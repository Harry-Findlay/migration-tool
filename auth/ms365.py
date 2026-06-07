"""
auth/ms365.py
=============
Microsoft 365 / Azure AD authentication via MSAL (OAuth 2.0 Auth Code flow).

Session storage
---------------
Flask's default cookie-based sessions fail here because:
  - SameSite=None requires Secure=True, which requires HTTPS
  - We're on plain http://localhost

So we use a simple server-side session store: a dict keyed by a random
session ID, with the ID stored in a small cookie. This keeps the cookie
under 50 bytes and sidesteps every SameSite/Secure/size issue.

Setup (Azure portal)
--------------------
1. App registrations → New registration
   - Name: IT INFINITY Migration Tool
   - Supported account types: Single tenant
   - Redirect URI: Web → http://localhost:5000/auth/callback
2. Note: Application (client) ID → AZURE_CLIENT_ID
         Directory (tenant) ID   → AZURE_TENANT_ID
3. Certificates & secrets → New client secret → AZURE_CLIENT_SECRET
4. API permissions → Microsoft Graph → Delegated → User.Read → Grant admin consent
"""

import os
import json
import uuid
import time
import logging
import functools
import threading

import msal
from flask import (Blueprint, redirect, request, jsonify,
                   current_app, make_response)

logger  = logging.getLogger("auth.ms365")
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# ── Server-side session store ─────────────────────────────────────────────────
# Simple in-memory dict: { session_id -> { data dict } }
# Survives for the lifetime of the server process (8 hours by default).
# For multi-process deployments replace with Redis or a DB-backed store.

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


# ── MSAL helpers ──────────────────────────────────────────────────────────────

def _build_msal_app():
    cfg = current_app.config
    return msal.ConfidentialClientApplication(
        cfg["AZURE_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{cfg['AZURE_TENANT_ID']}",
        client_credential=cfg["AZURE_CLIENT_SECRET"],
    )


def _callback_uri() -> str:
    override = os.environ.get("AZURE_REDIRECT_URI", "")
    if override:
        return override
    port = current_app.config.get("SERVER_PORT", 5000)
    return f"http://localhost:{port}/auth/callback"


def _fetch_graph_profile(token: str) -> dict:
    import urllib.request as _ur
    req = _ur.Request(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning(f"Graph /me failed: {exc}")
        return {}


# ── Routes ────────────────────────────────────────────────────────────────────

SCOPES = ["User.Read"]


@auth_bp.route("/login")
def login():
    """Redirect browser to Microsoft 365 sign-in."""
    _purge_expired()

    # Generate a state value and store it server-side against this session ID
    state = str(uuid.uuid4())
    sid   = _new_sid()
    _set_store(sid, {"auth_state": state})

    cca      = _build_msal_app()
    auth_url = cca.get_authorization_request_url(
        SCOPES,
        state=state,
        redirect_uri=_callback_uri(),
    )

    # Set the session cookie BEFORE redirecting to Microsoft.
    # httponly=True, samesite="Lax" is fine here because the browser is
    # navigating away FROM us — the cookie is set in the response headers.
    resp = make_response(redirect(auth_url))
    resp.set_cookie(
        _COOKIE_NAME, sid,
        httponly=True,
        samesite="Lax",   # Lax is safe for the outgoing leg
        max_age=600,       # 10 min — enough time to complete login
    )
    return resp


@auth_bp.route("/callback")
def callback():
    """Handle the redirect back from Microsoft after login."""
    error = request.args.get("error")
    if error:
        desc = request.args.get("error_description", "")
        logger.error(f"Auth error: {error} — {desc}")
        return f"Authentication failed: {error} — {desc}", 401

    received_state = request.args.get("state", "")
    sid            = _sid_from_request()
    store_data     = _get_store(sid)
    stored_state   = store_data.get("auth_state", "")

    logger.debug(f"callback: sid={sid!r} received={received_state!r} stored={stored_state!r}")

    if stored_state and received_state != stored_state:
        logger.warning("State mismatch — possible CSRF.")
        return "State mismatch — possible CSRF attack.", 400

    if not stored_state:
        logger.warning("No stored auth_state found for this session ID — proceeding anyway.")

    cca    = _build_msal_app()
    result = cca.acquire_token_by_authorization_code(
        request.args.get("code", ""),
        scopes=SCOPES,
        redirect_uri=_callback_uri(),
    )

    if "error" in result:
        logger.error(f"Token exchange failed: {result}")
        return f"Token exchange failed: {result.get('error_description', result)}", 401

    profile = _fetch_graph_profile(result["access_token"])

    user = {
        "email":        profile.get("mail") or profile.get("userPrincipalName", ""),
        "display_name": profile.get("displayName", "User"),
        "given_name":   profile.get("givenName", ""),
        "surname":      profile.get("surname", ""),
        "id":           profile.get("id", ""),
    }

    # Reuse the same sid or issue a new one (new one is safer post-auth)
    new_sid = _new_sid()
    _del_store(sid)
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
    sid = _sid_from_request()
    user_email = (_get_store(sid).get("user") or {}).get("email", "unknown")
    _del_store(sid)
    logger.info(f"User signed out: {user_email}")

    cfg            = current_app.config
    ms_logout_url  = (
        f"https://login.microsoftonline.com/{cfg['AZURE_TENANT_ID']}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri=http://localhost:{cfg.get('SERVER_PORT', 5000)}/"
    )
    resp = make_response(redirect(ms_logout_url))
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