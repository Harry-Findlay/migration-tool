"""
auth/google_oauth.py
====================
Google Workspace authentication (OAuth 2.0 / OpenID Connect, Auth Code + PKCE).

Drop-in replacement for auth/ms365.py — exposes the same public surface:
    auth_bp, require_auth, get_current_user
and the same routes:
    /auth/login  /auth/callback  /auth/logout  /auth/me

The user dict stored in the session has the same keys as before
(email, display_name, given_name, surname, id) so server.py, the audit log
and the frontend need no changes beyond the import.

No third-party OAuth library is used — the token exchange is a single
stdlib urllib POST, so there is nothing new for PyInstaller to collect.

Session storage
---------------
Unchanged from ms365.py: server-side dict keyed by a random session ID,
with only the ID in a small cookie. Avoids the 4KB cookie limit and the
SameSite=None/Secure requirement on plain http://localhost.

Domain restriction
------------------
Two layers:
  1. OAuth consent screen set to "Internal" — Google only lets accounts
     from the Workspace organisation through.
  2. Server-side check of the ID token `hd` claim and email domain against
     GOOGLE_ALLOWED_DOMAIN. This is the one that actually matters — the
     `hd` URL parameter sent on login is only a UI hint and can be removed
     by the user.

Setup (Google Cloud console)
----------------------------
1. Create (or select) a project under the IT INFINITY organisation.
2. APIs & Services → OAuth consent screen
   - User type: Internal
   - Scopes: openid, .../auth/userinfo.email, .../auth/userinfo.profile
3. APIs & Services → Credentials → Create credentials → OAuth client ID
   - Application type: Web application
   - Authorised redirect URI: http://localhost:5000/auth/callback
4. Copy Client ID     → GOOGLE_CLIENT_ID
        Client secret → GOOGLE_CLIENT_SECRET
5. GOOGLE_ALLOWED_DOMAIN=itinfinity.co.uk   (comma-separate for multiple)
"""

import os
import json
import uuid
import time
import base64
import hashlib
import secrets
import logging
import functools
import threading
import urllib.error
import urllib.parse
import urllib.request

from flask import (Blueprint, redirect, request, jsonify,
                   current_app, make_response)

logger  = logging.getLogger("auth.google")
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# ── Server-side session store ─────────────────────────────────────────────────
# Simple in-memory dict: { session_id -> { data dict } }
# Survives for the lifetime of the server process (8 hours by default).

_SESSION_STORE: dict = {}
_SESSION_LOCK         = threading.Lock()
_SESSION_TTL          = 8 * 60 * 60   # 8 hours in seconds
_COOKIE_NAME          = "itinfinity_sid"


def _new_sid() -> str:
    return str(uuid.uuid4())


def _get_store(sid: str) -> dict:
    """Return the session data dict for sid, or {} if missing/expired."""
    if not sid:
        return {}
    with _SESSION_LOCK:
        entry = _SESSION_STORE.get(sid)
        if not entry:
            return {}
        if time.time() > entry["expires"]:
            del _SESSION_STORE[sid]
            return {}
        return entry["data"]


def _set_store(sid: str, data: dict):
    with _SESSION_LOCK:
        _SESSION_STORE[sid] = {
            "data":    data,
            "expires": time.time() + _SESSION_TTL,
        }


def _del_store(sid: str):
    with _SESSION_LOCK:
        _SESSION_STORE.pop(sid, None)


def _purge_expired():
    """Remove expired sessions — called occasionally to prevent memory leak."""
    now = time.time()
    with _SESSION_LOCK:
        expired = [k for k, v in _SESSION_STORE.items() if now > v["expires"]]
        for k in expired:
            del _SESSION_STORE[k]


def get_current_user() -> dict | None:
    """Return the signed-in user dict, or None if not authenticated."""
    sid  = request.cookies.get(_COOKIE_NAME, "")
    data = _get_store(sid)
    return data.get("user")


def _sid_from_request() -> str:
    return request.cookies.get(_COOKIE_NAME, "")


# ── Google OAuth helpers ──────────────────────────────────────────────────────

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUERS   = ("https://accounts.google.com", "accounts.google.com")
_CLOCK_SKEW       = 120   # seconds of tolerance on exp / iat

SCOPES = "openid email profile"


def _allowed_domains() -> list[str]:
    raw = current_app.config.get("GOOGLE_ALLOWED_DOMAIN", "") or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _callback_uri() -> str:
    override = os.environ.get("GOOGLE_REDIRECT_URI", "")
    if override:
        return override
    port = current_app.config.get("SERVER_PORT", 5000)
    return f"http://localhost:{port}/auth/callback"


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier  = secrets.token_urlsafe(64)            # 86 chars, within 43–128
    digest    = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _exchange_code(code: str, verifier: str) -> dict:
    """POST the auth code to Google's token endpoint. Returns the JSON body."""
    cfg  = current_app.config
    body = urllib.parse.urlencode({
        "code":          code,
        "client_id":     cfg["GOOGLE_CLIENT_ID"],
        "client_secret": cfg["GOOGLE_CLIENT_SECRET"],
        "redirect_uri":  _callback_uri(),
        "grant_type":    "authorization_code",
        "code_verifier": verifier,
    }).encode("ascii")

    req = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept":       "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return json.loads(raw)
        except Exception:
            return {"error": f"http_{exc.code}",
                    "error_description": raw.decode("utf-8", "replace")[:300]}
    except Exception as exc:
        return {"error": "network_error", "error_description": str(exc)}


def _decode_id_token(id_token: str) -> dict:
    """
    Decode the ID token payload.

    The token is received directly from Google's token endpoint over TLS in
    exchange for our client secret, so per Google's OIDC guidance signature
    verification is not required. The claims are still validated below.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed ID token")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _validate_claims(claims: dict, expected_nonce: str) -> str | None:
    """Return an error string if the claims are unacceptable, else None."""
    cfg = current_app.config
    now = time.time()

    if claims.get("iss") not in _GOOGLE_ISSUERS:
        return f"Unexpected issuer: {claims.get('iss')!r}"
    if claims.get("aud") != cfg["GOOGLE_CLIENT_ID"]:
        return "ID token audience does not match this client."
    if float(claims.get("exp", 0)) < now - _CLOCK_SKEW:
        return "ID token has expired."
    if claims.get("nonce") != expected_nonce:
        return "Nonce mismatch."
    if not claims.get("email_verified"):
        return "Google account email is not verified."

    domains = _allowed_domains()
    if domains:
        email  = (claims.get("email") or "").lower()
        hd     = (claims.get("hd") or "").lower()
        e_dom  = email.rpartition("@")[2]
        if hd not in domains or e_dom not in domains:
            return f"Account {email or '(unknown)'} is not in an authorised domain."
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/login")
def login():
    """Redirect browser to Google sign-in."""
    _purge_expired()

    state             = secrets.token_urlsafe(32)
    nonce             = secrets.token_urlsafe(32)
    verifier, chall   = _pkce_pair()
    sid               = _new_sid()
    _set_store(sid, {
        "auth_state":    state,
        "auth_nonce":    nonce,
        "pkce_verifier": verifier,
    })

    params = {
        "client_id":             current_app.config["GOOGLE_CLIENT_ID"],
        "redirect_uri":          _callback_uri(),
        "response_type":         "code",
        "scope":                 SCOPES,
        "state":                 state,
        "nonce":                 nonce,
        "code_challenge":        chall,
        "code_challenge_method": "S256",
        "prompt":                "select_account",
    }
    # hd is a UI hint only (pre-filters the account chooser). Enforcement
    # happens in _validate_claims. Google only accepts a single value here.
    domains = _allowed_domains()
    if len(domains) == 1:
        params["hd"] = domains[0]

    auth_url = f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    # Lax cookie is sent on the top-level GET back from accounts.google.com.
    resp = make_response(redirect(auth_url))
    resp.set_cookie(
        _COOKIE_NAME, sid,
        httponly=True,
        samesite="Lax",
        max_age=600,       # 10 min — enough time to complete login
    )
    return resp


@auth_bp.route("/callback")
def callback():
    """Handle the redirect back from Google after login."""
    error = request.args.get("error")
    if error:
        desc = request.args.get("error_description", "")
        logger.error(f"Auth error: {error} — {desc}")
        return f"Authentication failed: {error} — {desc}", 401

    received_state = request.args.get("state", "")
    sid            = _sid_from_request()
    store_data     = _get_store(sid)
    stored_state   = store_data.get("auth_state", "")
    verifier       = store_data.get("pkce_verifier", "")
    nonce          = store_data.get("auth_nonce", "")

    logger.debug(f"callback: sid={sid!r} received={received_state!r} stored={stored_state!r}")

    # Strict: the PKCE verifier and nonce live in this session, so without
    # it the exchange cannot complete anyway.
    if not stored_state or not verifier:
        logger.warning("No pending login for this session ID (cookie missing or expired).")
        return ("Sign-in session expired or cookie was blocked. "
                "<a href='/auth/login'>Try again</a>"), 400

    if not secrets.compare_digest(received_state, stored_state):
        logger.warning("State mismatch — possible CSRF.")
        return "State mismatch — possible CSRF attack.", 400

    # Single-use: burn the pending-login data before the network call.
    _del_store(sid)

    result = _exchange_code(request.args.get("code", ""), verifier)
    if "error" in result or "id_token" not in result:
        logger.error(f"Token exchange failed: {result}")
        return f"Token exchange failed: {result.get('error_description', result)}", 401

    try:
        claims = _decode_id_token(result["id_token"])
    except Exception as exc:
        logger.error(f"ID token decode failed: {exc}")
        return "Token exchange failed: invalid ID token.", 401

    problem = _validate_claims(claims, nonce)
    if problem:
        logger.warning(f"Sign-in rejected: {problem}")
        return f"Access denied: {problem}", 403

    email = claims.get("email", "")
    user = {
        "email":        email,
        "display_name": claims.get("name") or email or "User",
        "given_name":   claims.get("given_name", ""),
        "surname":      claims.get("family_name", ""),
        "id":           claims.get("sub", ""),
    }

    new_sid = _new_sid()
    _set_store(new_sid, {"user": user})

    logger.info(f"User signed in: {user['email']}")

    resp = make_response(redirect("/"))
    resp.set_cookie(
        _COOKIE_NAME, new_sid,
        httponly=True,
        samesite="Lax",
        max_age=_SESSION_TTL,
    )
    return resp


@auth_bp.route("/logout")
def logout():
    """
    Clear the local session. The user stays signed in to Google in the
    browser (same as any Workspace app); prompt=select_account on the next
    login means they still get to choose the account.
    """
    sid = _sid_from_request()
    user_email = (_get_store(sid).get("user") or {}).get("email", "unknown")
    _del_store(sid)
    logger.info(f"User signed out: {user_email}")

    resp = make_response(redirect("/"))
    resp.delete_cookie(_COOKIE_NAME)
    return resp


@auth_bp.route("/me")
def me():
    """Return the current user's profile — called by the frontend on every load."""
    user = get_current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": user})


# ── Decorator ─────────────────────────────────────────────────────────────────

def require_auth(f):
    """Protect API endpoints — returns 401 JSON if not signed in."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not get_current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect("/auth/login")
        return f(*args, **kwargs)
    return wrapper
