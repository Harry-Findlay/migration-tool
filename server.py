"""
server.py
=========
Flask web server that powers the IT INFINITY Migration Tool.

Run with:
    python server.py

The server binds to http://localhost:5000 by default.
The desktop shortcut created by the installer points to this address.

Architecture
------------
  server.py         — Flask app, API routes, migration state machine
  auth/ms365.py     — Microsoft 365 MSAL OAuth blueprint
  core/engine.py    — Migration orchestration (unchanged background thread logic)
  core/migration_store.py — SQLite persistence (unchanged)
  datasources/      — Source and target datasource classes (unchanged)

API surface
-----------
  GET  /                       Serve the frontend HTML
  GET  /auth/login             Redirect to Microsoft 365 sign-in
  GET  /auth/callback          Handle OAuth callback
  GET  /auth/logout            Sign out
  GET  /auth/me                Return current user

  GET  /api/status             Server + connection status
  GET  /api/datasources        List available source/target datasources
  GET  /api/config             Get current datasource configuration
  POST /api/config             Save datasource configuration
  POST /api/test/source        Test source connection
  POST /api/test/target        Test target connection
  POST /api/validate           Validate both configurations
  POST /api/browse_folder      Open native folder browser (Windows only)

  POST /api/load               Load patients from source into memory
  GET  /api/patients           Return loaded patient list
  GET  /api/overview           Return overview stats

  POST /api/migrate/start      Start a migration
  POST /api/migrate/pause      Pause the running migration
  POST /api/migrate/resume     Resume a paused migration
  POST /api/migrate/cancel     Cancel the running migration
  GET  /api/migrate/status     Poll migration progress
  GET  /api/history            List past sessions
  GET  /api/history/<id>       Get a specific session
  DELETE /api/history/<id>     Delete a session

  POST /api/pms/compare        Compare PMS CSV data against loaded source patients
  POST /api/pms/confirm        Confirm PMS settings for the upcoming migration

  POST /api/dicom/export       Start a DICOM export job
  GET  /api/dicom/status       Poll DICOM export progress

  POST /api/clear_migrated     Clear SOURCE=1 patients from VistaSoft DB

  GET  /api/audit              Return audit log entries
"""

import os
import sys
import json
import logging
import threading
import datetime
from typing import Optional

from flask import (Flask, jsonify, request, send_from_directory,
                   redirect, url_for)
from flask_cors import CORS

# ── Path setup ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Load .env from the project root before anything reads os.environ.
# python-dotenv is in requirements.txt; this is a no-op if the file doesn't exist.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(BASE_DIR, ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass  # dotenv not installed yet — values must come from the environment directly

from core.engine          import MigrationEngine
from core.migration_store import MigrationStore
from core.models          import MigrationStatus
from auth.ms365           import auth_bp, require_auth, get_current_user

# ── Datasource registry ────────────────────────────────────────────────────────
from datasources.vistasoft_source  import VistaSoftSource
from datasources.sopro_source      import SOPROSource
from datasources.dtxstudio_source  import DTXStudioSource
from datasources.dtxstudio_target  import DTXStudioTarget
from datasources.vistasoft_target  import VistaSoftTarget

SOURCE_REGISTRY = {
    "VistaSoft":  VistaSoftSource,
    "DTX Studio": DTXStudioSource,
    "SOPRO":      SOPROSource,
}

TARGET_REGISTRY = {
    "DTX Studio": DTXStudioTarget,
    "VistaSoft":  VistaSoftTarget,
}

# ── Logging ────────────────────────────────────────────────────────────────────
# Create the log directory before the FileHandler tries to open the file.
_LOG_DIR = os.path.join(os.path.expanduser("~"), ".config", "ITInfinityMigrator")
os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(_LOG_DIR, "server.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("server")


# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
app.permanent_session_lifetime = datetime.timedelta(hours=8)

# CORS — must explicitly list the origin (not wildcard) when credentials are included
CORS(app,
     supports_credentials=True,
     origins=["http://localhost:5000"],
     allow_headers=["Content-Type"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

# ── Azure AD config  (set in .env or environment before starting the server) ──
app.config["AZURE_TENANT_ID"]     = os.environ.get("AZURE_TENANT_ID",     "")
app.config["AZURE_CLIENT_ID"]     = os.environ.get("AZURE_CLIENT_ID",     "")
app.config["AZURE_CLIENT_SECRET"] = os.environ.get("AZURE_CLIENT_SECRET", "")

# Warn at startup if Azure credentials are missing rather than crashing later
_missing = [k for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
            if not app.config[k]]
if _missing:
    logger.warning(
        "Azure AD credentials not set: %s. "
        "Sign-in will fail. Check your .env file at: %s",
        ", ".join(_missing), os.path.join(BASE_DIR, ".env")
    )

app.register_blueprint(auth_bp)


# ═══════════════════════════════════════════════════════════════════════════════
# In-memory application state
# All mutable state lives here so every request thread can access it.
# ═══════════════════════════════════════════════════════════════════════════════

_state_lock = threading.Lock()

class AppState:
    """Single global mutable state object, protected by _state_lock."""

    def __init__(self):
        self.store   = MigrationStore()
        self.engine  = MigrationEngine(store=self.store)

        # Currently selected datasource names
        self.source_name: str = "VistaSoft"
        self.target_name: str = "DTX Studio"

        # Live datasource instances (re-created when config changes)
        self.source: Optional[object] = SOURCE_REGISTRY["VistaSoft"]()
        self.target: Optional[object] = TARGET_REGISTRY["DTX Studio"]()

        # Patients loaded from source
        self.loaded_patients: list = []
        self.load_meta: dict = {}

        # Live migration progress (updated by engine callbacks)
        self.migration_status: str  = "idle"   # idle|running|paused|completed|failed|cancelled
        self.migration_pct: int     = 0
        self.migration_message: str = ""
        self.migration_counters: dict = {}
        self.migration_log: list    = []        # [{ts, msg, level}]
        self.session_id: Optional[str] = None

        # DICOM export state
        self.dicom_status: str  = "idle"
        self.dicom_pct: int     = 0
        self.dicom_message: str = ""

        # PMS CSV state (set by /api/pms/compare, consumed at migration start)
        self.pms_patients: list  = []
        self.pms_data_source: str = "source"  # "pms" | "source" | "merged"
        self.pms_confirmed: bool  = False

_app = AppState()


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _current_user() -> dict:
    return get_current_user() or {}

def _ok(data: dict = None, **kwargs) -> tuple:
    payload = {"ok": True}
    if data:
        payload.update(data)
    payload.update(kwargs)
    return jsonify(payload), 200

def _err(message: str, code: int = 400) -> tuple:
    return jsonify({"ok": False, "error": message}), code

def _get_or_build_source(name: str) -> object:
    cls = SOURCE_REGISTRY.get(name)
    if not cls:
        raise ValueError(f"Unknown source: {name}")
    return cls()

def _get_or_build_target(name: str) -> object:
    cls = TARGET_REGISTRY.get(name)
    if not cls:
        raise ValueError(f"Unknown target: {name}")
    return cls()

def _push_log(msg: str, level: str = "info"):
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")

def _local_db_path(remote_path: str):
        """
        Context manager. If remote_path is a UNC/network path, copies the .fdb
        to a local temp file, yields the local path, then copies it back.
        If it's already local, yields it unchanged.
 
        Usage:
            with _local_db_path(db_path) as local:
                _run("execute", local, sql)
        """
        import contextlib, shutil, tempfile
 
        @contextlib.contextmanager
        def _ctx():
            is_network = remote_path.startswith("\\\\") or (
                len(remote_path) > 1 and remote_path[1] != ":"
            )
            if not is_network:
                yield remote_path
                return
 
            from datasources.fb_client import _get_db_copy
            tmp = _get_db_copy(remote_path)
            try:
                yield tmp
                # Copy back so writes are persisted
                shutil.copy2(tmp, remote_path)
                logger.debug(f"_local_db_path: copied back to {remote_path}")
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
 
        return _ctx()

# ═══════════════════════════════════════════════════════════════════════════════
# Frontend
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def serve_frontend():
    """Serve the single-page frontend. No auth required - the JS handles sign-in."""
    index = os.path.join(app.static_folder, "index.html")
    if not os.path.isfile(index):
        return (
            "<h2>Setup required</h2>"
            f"<p>Place <code>index.html</code> in:<br><code>{app.static_folder}</code></p>"
            "<p>Copy <code>migration-tool.html</code> there and rename it to"
            " <code>index.html</code>.</p>"
        ), 404
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    """Simple health check - no auth required."""
    return jsonify({
        "ok": True,
        "static_folder": app.static_folder,
        "index_exists": os.path.isfile(
            os.path.join(app.static_folder, "index.html")
        ),
    })


@app.route("/api/diagnostics/source")
@require_auth
def api_diagnostics_source():
    """
    Walk the configured source MediaPath and return what we find.
    Helps diagnose why patients aren't being discovered.
    """
    with _state_lock:
        source = _app.source

    media_path = source.get_config_value("MediaPath") if source else ""
    if not media_path:
        return _ok(error="MediaPath not set", tree=[])

    if not os.path.isdir(media_path):
        return _ok(error=f"MediaPath does not exist: {media_path}", tree=[])

    tree = []
    patient_dat_found = []
    try:
        for root, dirs, files in os.walk(media_path):
            depth = root.replace(media_path, "").count(os.sep)
            if depth > 4:
                dirs.clear()  # don't go deeper than 4 levels
                continue
            rel = os.path.relpath(root, media_path)
            has_patient = any(f.lower() in ("patient.dat","patient.dax") for f in files)
            has_images  = any(f.lower() == "image.dat" for f in files)
            if has_patient or has_images or depth <= 1:
                entry = {
                    "path":        rel,
                    "depth":       depth,
                    "subdirs":     [d for d in dirs[:10]],
                    "files":       [f for f in files[:20]],
                    "has_patient": has_patient,
                    "has_images":  has_images,
                }
                tree.append(entry)
                if has_patient:
                    patient_dat_found.append(rel)
    except Exception as e:
        return _ok(error=str(e), tree=tree)

    return _ok(
        media_path=media_path,
        tree=tree,
        patient_folders_found=len(patient_dat_found),
        patient_folder_paths=patient_dat_found[:20],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# API — Status
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    with _state_lock:
        src_name = _app.source_name
        tgt_name = _app.target_name
        mig_status = _app.migration_status
    return _ok(
        connected=True,
        status="Server running",
        source=src_name,
        target=tgt_name,
        migration_status=mig_status,
        user=_current_user(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# API — Datasources
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/datasources")
@require_auth
def api_datasources():
    sources = [{"name": k, "display_name": k} for k in SOURCE_REGISTRY]
    targets = [{"name": k, "display_name": k} for k in TARGET_REGISTRY]
    return _ok(sources=sources, targets=targets)


@app.route("/api/config", methods=["GET"])
@require_auth
def api_config_get():
    with _state_lock:
        source = _app.source
        target = _app.target
    return _ok(
        source_name=_app.source_name,
        target_name=_app.target_name,
        source_config=source.config_to_dict() if source else [],
        target_config=target.config_to_dict() if target else [],
    )

@app.route("/api/config/schema")
@require_auth
def api_config_schema():
    """
    Return the configuration field schema for any source/target combination
    WITHOUT changing app state.  Used by the frontend dynamic config renderer.
 
    Query params:
        source  — e.g. "VistaSoft", "DTX Studio", "SOPRO"
        target  — e.g. "DTX Studio", "VistaSoft"
    """
    src_name = request.args.get("source", "VistaSoft")
    tgt_name = request.args.get("target", "DTX Studio")
 
    try:
        source = _get_or_build_source(src_name)
    except ValueError as e:
        return _err(str(e))
 
    try:
        target = _get_or_build_target(tgt_name)
    except ValueError as e:
        return _err(str(e))
 
    # Pre-fill with currently saved values so the UI restores previous entries
    with _state_lock:
        if _app.source_name == src_name and _app.source:
            for item in _app.source.configuration:
                source.set_config_value(item.key, item.value)
        if _app.target_name == tgt_name and _app.target:
            for item in _app.target.configuration:
                target.set_config_value(item.key, item.value)
 
    return _ok(
        source_name=src_name,
        target_name=tgt_name,
        source_config=source.config_to_dict(show_advanced=True),
        target_config=target.config_to_dict(show_advanced=True),
    )

@app.route("/api/config", methods=["POST"])
@require_auth
def api_config_save():
    """
    Save datasource configuration sent from the frontend.
    Expected body:
    {
        "source":        "VistaSoft",
        "target":        "DTX Studio",
        "source_config": {"MediaPath": "C:\\VistaSoftData", ...},
        "target_config": {"CoreUrl": "https://...", "Username": "...", ...}
    }
    """
    body = request.get_json(force=True) or {}
    src_name = body.get("source", "VistaSoft")
    tgt_name = body.get("target", "DTX Studio")

    try:
        source = _get_or_build_source(src_name)
        target = _get_or_build_target(tgt_name)
    except ValueError as e:
        return _err(str(e))

    source.apply_config_dict(body.get("source_config", {}))
    target.apply_config_dict(body.get("target_config", {}))

    with _state_lock:
        _app.source_name = src_name
        _app.target_name = tgt_name
        _app.source = source
        _app.target = target

    _app.store.audit("config_saved",
                     f"{src_name} → {tgt_name}",
                     user_email=_current_user().get("email"))
    logger.info(f"Config saved: {src_name} → {tgt_name}")
    return _ok(message="Configuration saved.")


# ═══════════════════════════════════════════════════════════════════════════════
# API — Connection tests & validation
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/test/source", methods=["POST"])
@require_auth
def api_test_source():
    body = request.get_json(force=True) or {}
    cfg  = body.get("config", {})
    src_name = body.get("source", _app.source_name)

    try:
        source = _get_or_build_source(src_name)
    except ValueError as e:
        return _err(str(e))

    source.apply_config_dict(cfg)

    # Persist so subsequent calls (load, migrate) use the same config
    with _state_lock:
        _app.source      = source
        _app.source_name = src_name

    try:
        if hasattr(source, "test_db_connection"):
            ok, msg = source.test_db_connection()
        else:
            ok, msg = source.validate()
    except Exception as e:
        logger.exception("Source test failed")
        return _err(str(e))

    if ok:
        return _ok(message=msg)
    return _err(msg)


@app.route("/api/test/target", methods=["POST"])
@require_auth
def api_test_target():
    body = request.get_json(force=True) or {}
    cfg  = body.get("config", {})
    tgt_name = body.get("target", _app.target_name)

    try:
        target = _get_or_build_target(tgt_name)
    except ValueError as e:
        return _err(str(e))

    target.apply_config_dict(cfg)

    with _state_lock:
        _app.target      = target
        _app.target_name = tgt_name

    try:
        ok, msg = target.test_connection()
    except Exception as e:
        logger.exception("Target test failed")
        return _err(str(e))
 
    if not ok:
        return _err(msg)
 
    # For DTX Studio targets, fetch extra info for the integration page
    extra = {}
    from datasources.dtxstudio_target import DTXStudioTarget
    from datasources.dtxstudio_source import DTXStudioSource
    if isinstance(target, (DTXStudioTarget, DTXStudioSource)):
        try:
            client = target._make_client()
            info = client.get_core_info()
            extra["core_version"] = (
                info.get("version") or info.get("coreVersion") or
                info.get("buildVersion") or info.get("Version") or "—"
            )
            extra["api_version"] = (
                info.get("apiVersion") or info.get("api_version") or
                str(info.get("apiResourceVersion", "")) or "—"
            )
            # Patient count — catch failure gracefully
            try:
                patients = client.get_patients()
                extra["patient_count"] = len(patients)
            except Exception:
                extra["patient_count"] = None
        except Exception:
            pass
 
    return _ok(message=msg, **extra)


@app.route("/api/validate", methods=["POST"])
@require_auth
def api_validate():
    body = request.get_json(force=True) or {}

    with _state_lock:
        source = _app.source
        target = _app.target

    if not source or not target:
        return _err("Source or target not configured.")

    # Apply any config overrides sent with the request
    if body.get("source_config"):
        source.apply_config_dict(body["source_config"])
    if body.get("target_config"):
        target.apply_config_dict(body["target_config"])

    ok1, msg1 = source.validate()
    ok2, msg2 = target.validate()
    if ok1 and ok2:
        return _ok(message="Both configurations are valid.")
    errors = []
    if not ok1:
        errors.append(f"Source: {msg1}")
    if not ok2:
        errors.append(f"Target: {msg2}")
    return _err(" | ".join(errors))


# ═══════════════════════════════════════════════════════════════════════════════
# API — Folder browser (Windows only, graceful fallback elsewhere)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/browse_folder", methods=["POST"])
@require_auth
def api_browse_folder():
    """
    Open a native folder selection dialog on the server machine.
    Only works when the server is running on Windows with a display.
    """
    if os.name != "nt":
        return _err("Folder browser only available on Windows.")
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askdirectory(title="Select Folder")
        root.destroy()
        if path:
            return _ok(path=os.path.normpath(path))
        return _ok(path="")
    except Exception as e:
        return _err(f"Folder browser failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# API — Load data
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/load", methods=["POST"])
@require_auth
def api_load():
    """
    Load all patients from the configured source.
    Accepts source name + config overrides in the request body, or uses
    whatever was last saved via /api/config.
    """
    body = request.get_json(force=True) or {}

    # If a source name is in the request, rebuild from scratch with that config.
    # Otherwise use whatever is already in _app.source (saved by /api/config).
    src_name   = body.get("source")
    src_config = body.get("config", {})

    with _state_lock:
        source = _app.source

    if not source:
        return _err("No source configured. Save configuration first.")

    if src_name and src_name != _app.source_name:
        try:
            source = _get_or_build_source(src_name)
        except ValueError as e:
            return _err(str(e))

    if src_config:
        source.apply_config_dict(src_config)
        with _state_lock:
            _app.source = source

    # Log exactly what config we're about to use — helps diagnose path issues
    media_path = source.get_config_value("MediaPath") or source.get_config_value("media_path") or ""
    logger.info(f"api_load: source={source.name!r} MediaPath={media_path!r}")

    ok, msg = source.validate()
    if not ok:
        logger.warning(f"api_load: validation failed: {msg}")
        return _err(f"Source validation failed: {msg}")

    import time
    t0 = time.time()
    try:
        patients = source.load(
            cancel_flag=lambda: False,
            max_parallelism=int(body.get("max_parallelism", 4)),
        )
    except Exception as e:
        logger.exception("Load failed")
        return _err(f"Failed to load from source: {e}")

    elapsed = round(time.time() - t0, 2)
 
    def _count_media(patient_list: list) -> int:
        """Count media by walking studies→series→media. Works for all datasources."""
        total = 0
        for p in patient_list:
            for study in p.get("studies", {}).values():
                for series in study.get("series", {}).values():
                    total += len(series.get("media", []))
        return total
 
    media_count = _count_media(patients)

    # Compute size estimate: walk the images path if accessible
    size_gb = 0.0
    try:
        if hasattr(source, "_get_images_path"):
            images_path = source._get_images_path()
            if os.path.isdir(images_path):
                total_bytes = sum(
                    f.stat().st_size
                    for f in os.scandir(images_path)
                    if f.is_file()
                )
                size_gb = round(total_bytes / (1024 ** 3), 2)
    except Exception:
        pass

    # Build media type breakdown
    modality_counts: dict = {}
    for p in patients:
        for study in p.get("studies", {}).values():
            for series in study.get("series", {}).values():
                for media in series.get("media", []):
                    mod = media.get("modality") or media.get("image_class") or "Unknown"
                    modality_counts[mod] = modality_counts.get(mod, 0) + 1

    with _state_lock:
        _app.loaded_patients = patients
        _app.load_meta = {
            "patients_count": len(patients),
            "media":          media_count,
            "size_gb":        size_gb,
            "load_time":      f"{elapsed}s",
            "modalities":     modality_counts,
        }

    _app.store.audit("source_loaded",
                     f"{len(patients)} patients from {source.name}",
                     user_email=_current_user().get("email"))
    logger.info(f"Loaded {len(patients)} patients from {source.name} in {elapsed}s")

    # Include a lightweight patient list (uid + name only) for the raw cache
    raw_list = [
        {
            "uid":         p.get("uid", ""),
            "given_names": p.get("given_names", ""),
            "family_name": p.get("family_name", ""),
            "studies":     p.get("studies", {}),
        }
        for p in patients
    ]
    return _ok(patients=raw_list, **_app.load_meta)

@app.route("/api/patients")
@require_auth
def api_patients():
    """Return the in-memory patient list. Accepts ?search= and ?status= filters."""
    search = (request.args.get("search") or "").lower()
    status_filter = request.args.get("status", "")

    with _state_lock:
        patients = _app.loaded_patients

    def _safe(p: dict) -> dict:
        """Strip non-serialisable keys (lambdas etc.) for JSON output."""
        # Count media by walking studies→series→media — works for all datasources.
        media_count = sum(
            len(series.get("media", []))
            for study in p.get("studies", {}).values()
            for series in study.get("series", {}).values()
        )
        # Collect unique modalities for the media breakdown chart.
        modalities = list({
            m.get("modality") or m.get("image_class") or "Unknown"
            for study in p.get("studies", {}).values()
            for series in study.get("series", {}).values()
            for m in series.get("media", [])
        })
        return {
            "uid":          p.get("uid", ""),
            "family_name":  p.get("family_name", ""),
            "given_names":  p.get("given_names", ""),
            "dob":          p.get("dob", "") or p.get("birth_date", ""),
            "nhs_number":   p.get("nhs_number", ""),
            "media_count":  media_count,
            "modality":     modalities[0] if len(modalities) == 1 else (
                            "Mixed" if modalities else "Unknown"),
            "modalities":   modalities,
            "status":       p.get("status", "pending"),
        }

    result = [_safe(p) for p in patients]

    if search:
        result = [
            p for p in result
            if search in p["family_name"].lower()
            or search in p["given_names"].lower()
            or search in p["uid"].lower()
        ]
    if status_filter:
        result = [p for p in result if p["status"] == status_filter]

    return _ok(patients=result, total=len(result))


@app.route("/api/overview")
@require_auth
def api_overview():
    with _state_lock:
        meta     = _app.load_meta
        patients = _app.loaded_patients
        src_name = _app.source_name
        tgt_name = _app.target_name

    migrated_count = _app.store.count_migrated(
        src_name.lower().replace(" ", "_"),
        tgt_name.lower().replace(" ", "_"),
    )

    return _ok(
        patients_count=meta.get("patients_count", 0),
        media_count=meta.get("media", 0),
        size_gb=meta.get("size_gb", 0),
        migrated=migrated_count,
        modalities=meta.get("modalities", {}),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# API — Migration control
# ═══════════════════════════════════════════════════════════════════════════════

def _on_progress(current, total, message, counters=None):
    pct = int((current / max(total, 1)) * 100)
    with _state_lock:
        _app.migration_pct     = pct
        _app.migration_message = message
        if counters:
            _app.migration_counters = counters
        if _app.migration_status not in ("paused", "cancelled"):
            _app.migration_status = "running"
    _push_log(message, "info")


def _make_migration_callbacks(user_email: str):
        """Create on_complete/on_error callbacks that don't need request context."""
 
        def _on_complete(result):
            with _state_lock:
                _app.migration_status  = result.status.value
                _app.migration_pct     = 100 if result.status == MigrationStatus.COMPLETED else _app.migration_pct
                _app.migration_message = result.message
                _app.session_id        = result.session_id
            level = "success" if result.status == MigrationStatus.COMPLETED else "warning"
            _push_log(result.message, level)
            _app.store.audit(
                f"migration_{result.status.value}",
                result.message,
                session_id=result.session_id,
                user_email=user_email,
            )
 
        def _on_error(error_msg):
            with _state_lock:
                _app.migration_status  = "failed"
                _app.migration_message = error_msg
            _push_log(error_msg, "error")
 
        return _on_complete, _on_error

@app.route("/api/migrate/start", methods=["POST"])
@require_auth
def api_migrate_start():
    body = request.get_json(force=True) or {}

    with _state_lock:
        if _app.migration_status == "running":
            return _err("A migration is already running.")
        source = _app.source
        target = _app.target

    if not source or not target:
        return _err("Source or target not configured.")

    # Apply any config overrides from the request
    source.apply_config_dict(body.get("source_config", {}))
    target.apply_config_dict(body.get("target_config", {}))

    incremental = bool(body.get("incremental", False))
    max_par     = int(body.get("max_parallelism", 4))

    with _state_lock:
        _app.migration_status   = "running"
        _app.migration_pct      = 0
        _app.migration_message  = "Starting…"
        _app.migration_counters = {}
        _app.migration_log      = []

    _user_email = _current_user().get("email", "")
    _on_complete, _on_error = _make_migration_callbacks(_user_email)
 
    _app.engine.run(
        source=source, target=target,
        max_parallelism=max_par,
        incremental=incremental,
        on_progress=_on_progress,
        on_complete=_on_complete,
        on_error=_on_error,
    )

    _app.store.audit("migration_started",
                     f"incremental={incremental}",
                     user_email=_current_user().get("email"))
    logger.info(f"Migration started: {source.name} → {target.name}, incremental={incremental}")
    return _ok(message="Migration started.")


@app.route("/api/migrate/pause", methods=["POST"])
@require_auth
def api_migrate_pause():
    with _state_lock:
        if _app.migration_status != "running":
            return _err("No running migration to pause.")
        _app.migration_status = "paused"
    _app.engine.pause()
    _push_log("Migration paused.", "warning")
    _app.store.audit("migration_paused", user_email=_current_user().get("email"))
    return _ok(message="Migration paused.")


@app.route("/api/migrate/resume", methods=["POST"])
@require_auth
def api_migrate_resume():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id")

    with _state_lock:
        source = _app.source
        target = _app.target
        current_status = _app.migration_status

    if current_status == "paused" and not session_id:
        # Resume in-process paused migration
        _app.engine.resume()
        with _state_lock:
            _app.migration_status = "running"
        _push_log("Migration resumed.", "info")
        return _ok(message="Migration resumed.")

    if session_id:
        # Resume a previously paused session from history
        with _state_lock:
            _app.migration_status   = "running"
            _app.migration_pct      = 0
            _app.migration_message  = f"Resuming session {session_id[:8]}…"
            _app.migration_log      = []

        _app.engine = MigrationEngine(store=_app.store)
        _user_email = _current_user().get("email", "")
        _on_complete, _on_error = _make_migration_callbacks(_user_email)
 
        _app.engine.run(
            source=source, target=target,
            resume_session_id=session_id,
            on_progress=_on_progress,
            on_complete=_on_complete,
            on_error=_on_error,
        )
        _app.store.audit("migration_resumed", f"session={session_id}",
                         user_email=_current_user().get("email"))
        return _ok(message=f"Resuming session {session_id}.")

    return _err("No paused migration to resume.")


@app.route("/api/migrate/cancel", methods=["POST"])
@require_auth
def api_migrate_cancel():
    _app.engine.cancel()
    with _state_lock:
        _app.migration_status  = "cancelled"
        _app.migration_message = "Migration cancelled."
    _push_log("Migration cancelled.", "error")
    _app.store.audit("migration_cancelled", user_email=_current_user().get("email"))
    return _ok(message="Migration cancelled.")


@app.route("/api/migrate/status")
@require_auth
def api_migrate_status():
    with _state_lock:
        status   = _app.migration_status
        pct      = _app.migration_pct
        message  = _app.migration_message
        counters = _app.migration_counters
        log      = _app.migration_log[-50:]   # last 50 lines

    # Fetch live session row if we have one
    session_row = None
    if _app.session_id:
        session_row = _app.store.get_session(_app.session_id)

    status_labels = {
        "idle":      "Idle",
        "running":   "Running",
        "paused":    "Paused",
        "completed": "Completed",
        "failed":    "Failed",
        "cancelled": "Cancelled",
    }

    return _ok(
        status=status,
        status_label=status_labels.get(status, status.title()),
        current=pct,
        total=100,
        message=message,
        counters=counters,
        log=log,
        session=session_row,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# API — History
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/history")
@require_auth
def api_history():
    sessions = _app.store.list_sessions(limit=100)
    return _ok(sessions=sessions)


@app.route("/api/history/<session_id>")
@require_auth
def api_history_detail(session_id):
    sess = _app.store.get_session(session_id)
    if not sess:
        return _err("Session not found.", 404)
    patients = _app.store.get_all_patient_states(session_id)
    return _ok(session=sess, patients=patients)


@app.route("/api/history/<session_id>", methods=["DELETE"])
@require_auth
def api_history_delete(session_id):
    _app.store.delete_session(session_id)
    _app.store.audit("session_deleted", f"session={session_id}",
                     user_email=_current_user().get("email"))
    return _ok(message="Session deleted.")


# ═══════════════════════════════════════════════════════════════════════════════
# API — PMS CSV
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pms/compare", methods=["POST"])
@require_auth
def api_pms_compare():
    """
    Compare PMS patients (already parsed/normalised by the frontend)
    against the loaded source patients.

    Body: { "pms_patients": [{surname, first_name, dob, nhs_number, ...}] }
    Returns match results for each PMS patient.
    """
    body = request.get_json(force=True) or {}
    pms_patients = body.get("pms_patients", [])

    with _state_lock:
        source_patients = _app.loaded_patients

    def _norm_dob(dob: str) -> str:
        if not dob:
            return ""
        clean = "".join(c for c in dob if c.isdigit())
        if len(clean) == 8:
            if clean[:4] > "1900":
                return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
            return f"{clean[4:8]}-{clean[2:4]}-{clean[:2]}"
        return dob.strip()

    def _find_source(pms: dict):
        surname    = (pms.get("surname") or "").lower().strip()
        first_name = (pms.get("first_name") or "").lower().strip()
        dob        = _norm_dob(pms.get("dob", ""))
        nhs        = (pms.get("nhs_number") or "").strip()

        # 1. NHS number match
        if nhs:
            for sp in source_patients:
                if (sp.get("nhs_number") or "").strip() == nhs:
                    return sp

        # 2. DOB + surname
        for sp in source_patients:
            if (_norm_dob(sp.get("dob", "")) == dob
                    and (sp.get("family_name") or "").lower().strip() == surname):
                return sp

        # 3. Fuzzy surname + first 3 chars of first name
        for sp in source_patients:
            if ((sp.get("family_name") or "").lower().strip() == surname
                    and (sp.get("given_names") or "").lower().strip()
                    .startswith(first_name[:3])):
                return sp

        return None

    results = []
    for pms in pms_patients:
        sp = _find_source(pms)
        conflicts = []
        match_status = "matched" if sp else "unmatched"

        if sp:
            src_dob = _norm_dob(sp.get("dob", ""))
            pms_dob = _norm_dob(pms.get("dob", ""))
            if src_dob and pms_dob and src_dob != pms_dob:
                conflicts.append({
                    "field": "DOB",
                    "pms": pms.get("dob"),
                    "source": sp.get("dob"),
                })
            src_surname = (sp.get("family_name") or "").lower().strip()
            pms_surname = (pms.get("surname") or "").lower().strip()
            if src_surname and pms_surname and src_surname != pms_surname:
                conflicts.append({
                    "field": "Surname",
                    "pms": pms.get("surname"),
                    "source": sp.get("family_name"),
                })
            if conflicts:
                match_status = "conflict"

        results.append({
            "pms":         pms,
            "source_uid":  sp.get("uid") if sp else None,
            "source":      {
                "family_name": sp.get("family_name", "") if sp else "",
                "given_names": sp.get("given_names", "") if sp else "",
                "dob":         sp.get("dob", "") if sp else "",
            },
            "match_status": match_status,
            "conflicts":   conflicts,
        })

    matched   = sum(1 for r in results if r["match_status"] != "unmatched")
    conflicts = sum(1 for r in results if r["match_status"] == "conflict")
    unmatched = sum(1 for r in results if r["match_status"] == "unmatched")

    return _ok(
        results=results,
        summary={
            "total":    len(results),
            "matched":  matched,
            "conflicts": conflicts,
            "unmatched": unmatched,
        }
    )


@app.route("/api/pms/confirm", methods=["POST"])
@require_auth
def api_pms_confirm():
    """
    Confirm PMS settings to be applied to the next migration run.
    Body: { "pms_patients": [...], "data_source": "pms"|"source"|"merged",
            "resolutions": [{patient_ref, field, value}] }
    """
    body = request.get_json(force=True) or {}
    with _state_lock:
        _app.pms_patients    = body.get("pms_patients", [])
        _app.pms_data_source = body.get("data_source", "source")
        _app.pms_confirmed   = True

    _app.store.audit(
        "pms_confirmed",
        f"data_source={_app.pms_data_source}, {len(_app.pms_patients)} patients",
        user_email=_current_user().get("email"),
    )
    logger.info(f"PMS confirmed: {_app.pms_data_source}, {len(_app.pms_patients)} patients")
    return _ok(message="PMS settings confirmed.")


# ═══════════════════════════════════════════════════════════════════════════════
# API — DICOM Export
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/dicom/load_patients", methods=["POST"])
@require_auth
def api_dicom_load_patients():
    """
    Load patient list from DTX Studio for the DICOM export page.
    Uses the current target config if it's DTX Studio, otherwise
    accepts config override in the request body.
    """
    body = request.get_json(force=True) or {}
    cfg  = body.get("config", {})
 
    with _state_lock:
        target = _app.target
 
    # Use DTX Studio target — either the current one or build a fresh one
    from datasources.dtxstudio_target import DTXStudioTarget
    if isinstance(target, DTXStudioTarget):
        dtx = target
    else:
        # Try to find DTX Studio in registry
        dtx_cls = TARGET_REGISTRY.get("DTX Studio")
        if not dtx_cls:
            return _err("DTX Studio target is not available.")
        dtx = dtx_cls()
 
    if cfg:
        dtx.apply_config_dict(cfg)
 
    ok, msg = dtx.test_connection()
    if not ok:
        return _err(f"Cannot connect to DTX Studio: {msg}")
 
    try:
        client = dtx._make_client()
        raw_patients = client.get_patients()
        import concurrent.futures as _cf
 
        def _load_one(rp):
            uid  = rp.get("id", "")
            name = rp.get("fullName") or rp.get("name") or ""
            fn   = (rp.get("firstname") or rp.get("firstName") or
                    rp.get("givenName") or "")
            ln   = (rp.get("lastname") or rp.get("lastName") or
                    rp.get("familyName") or "")
            dob_raw = (rp.get("dateOfBirth") or rp.get("birthDate") or
                       rp.get("dob") or "")
            dob = dob_raw[:10] if dob_raw else ""
            if not fn and not ln and name:
                parts = name.strip().split()
                fn = parts[0] if parts else ""
                ln = parts[-1] if len(parts) > 1 else ""
            pid = (rp.get("dicomId") or rp.get("pmsId") or
                   rp.get("referenceId") or rp.get("patientReferenceId") or uid)
            media_count = None
            try:
                media_list = client.get_patient_media(uid)
                media_count = len([m for m in (media_list or [])
                                   if m.get("mediaType") in ("IMAGE", "VOLUME", None)])
            except Exception:
                pass
            return {
                "uid":         uid,
                "id":          pid,
                "given_names": fn,
                "family_name": ln,
                "birth_date":  dob,
                "media_count": media_count,
            }
 
        with _cf.ThreadPoolExecutor(max_workers=8) as pool:
            patients = list(pool.map(_load_one, raw_patients))
 
        return _ok(patients=patients, total=len(patients))
    except Exception as e:
        logger.exception("dicom_load_patients failed")
        return _err(str(e))

@app.route("/api/dicom/export", methods=["POST"])
@require_auth
def api_dicom_export():
    """
    Export DICOM files from DTX Studio for selected patients.
    Downloads actual DICOM binary data via the DTX Core API.
    """
    body           = request.get_json(force=True) or {}
    output_folder  = body.get("output_folder", "")
    modality_filter = (body.get("modality_filter") or "").upper()
    anonymise      = bool(body.get("anonymise", False))
    folder_naming  = body.get("folder_naming", "id")   # id | name | uid
    patient_uids   = body.get("patient_uids", [])       # empty = all
 
    if not output_folder:
        return _err("Output folder is required.")
    try:
        os.makedirs(output_folder, exist_ok=True)
    except Exception as e:
        return _err(f"Cannot create output folder: {e}")
 
    with _state_lock:
        target = _app.target
 
    from datasources.dtxstudio_target import DTXStudioTarget
    if isinstance(target, DTXStudioTarget):
        dtx = target
    else:
        dtx_cls = TARGET_REGISTRY.get("DTX Studio")
        if not dtx_cls:
            return _err("DTX Studio target is not available.")
        dtx = dtx_cls()
 
    ok, msg = dtx.test_connection()
    if not ok:
        return _err(f"Cannot connect to DTX Studio: {msg}")
 
    with _state_lock:
        _app.dicom_status  = "running"
        _app.dicom_pct     = 0
        _app.dicom_message = "Connecting to DTX Studio…"
 
    def _export_worker():
        try:
            client = dtx._make_client()
            all_patients = client.get_patients()
 
            # Filter to selected patients if specified
            if patient_uids:
                uid_set = set(patient_uids)
                all_patients = [p for p in all_patients if p.get("id") in uid_set]
 
            total = len(all_patients)
            done  = failed = 0
 
            with _state_lock:
                _app.dicom_message = f"Exporting {total} patient(s)…"
 
            for rp in all_patients:
                if _app.dicom_status == "cancelled":
                    break
 
                pat_uid = rp.get("id", "")
                fn = rp.get("firstname") or rp.get("firstName") or ""
                ln = rp.get("lastname")  or rp.get("lastName")  or ""
                pid = rp.get("dicomId") or rp.get("pmsId") or pat_uid
 
                # Determine folder name
                if folder_naming == "name":
                    folder_name = f"{ln}_{fn}".strip("_") or pat_uid[:12]
                elif folder_naming == "uid":
                    folder_name = pat_uid[:16] or pid
                else:
                    folder_name = pid or pat_uid[:12]
                # Sanitise folder name
                folder_name = "".join(c for c in folder_name
                                      if c.isalnum() or c in "-_ .")[:60]
                patient_dir = os.path.join(output_folder, folder_name or "unknown")
                os.makedirs(patient_dir, exist_ok=True)
 
                try:
                    # Get all media for this patient
                    media_list = client.get_patient_media(pat_uid)
                    exported = 0
                    for media in media_list:
                        mid      = media.get("id", "")
                        modality = (media.get("modality") or "").upper()
                        if modality_filter and modality != modality_filter:
                            continue
                        if media.get("mediaType") not in ("IMAGE", "VOLUME", None):
                            continue
                        try:
                            dcm_data = client.get_media_data(mid)
                            if not dcm_data:
                                continue
                            # Fetch DICOM tags from DTX and inject patient demographics
                            try:
                                dtags = client.get_media_dicom_tags(mid) or {}
                                dcm_data = _inject_dicom_tags(
                                    dcm_data, dtags,
                                    patient_name=f"{ln}^{fn}",
                                    patient_id=pid,
                                    patient_dob=rp.get("dateOfBirth", "")[:10].replace("-", ""),
                                    patient_sex=rp.get("gender", ""),
                                )
                            except Exception as te:
                                logger.debug(f"  Tag inject failed for {mid}: {te}")
                            if anonymise:
                                dcm_data = _anonymise_dicom(dcm_data, pat_uid)
                            sop = (media.get("sopInstanceUid") or mid or
                                   str(__import__("uuid").uuid4()))
                            fname = sop.replace(".", "_") + ".dcm"
                            with open(os.path.join(patient_dir, fname), "wb") as f:
                                f.write(dcm_data)
                            exported += 1
                        except Exception as me:
                            logger.warning(f"  Media {mid} export failed: {me}")
 
                    done += 1
                    logger.info(f"  Exported {exported} files for {fn} {ln}")
 
                except Exception as pe:
                    failed += 1
                    logger.error(f"  Patient {pat_uid} export failed: {pe}")
 
                pct = int((done + failed) / max(total, 1) * 100)
                with _state_lock:
                    _app.dicom_pct     = pct
                    _app.dicom_message = f"Exported {done}/{total} patients…"
 
            with _state_lock:
                _app.dicom_status  = "completed"
                _app.dicom_pct     = 100
                _app.dicom_message = (f"Export complete. {done} patient(s) exported"
                                      + (f", {failed} failed." if failed else "."))
            _app.store.audit("dicom_export_complete",
                             f"{done} patients → {output_folder}",
                             user_email=None)
        except Exception as e:
            logger.exception("DICOM export worker error")
            with _state_lock:
                _app.dicom_status  = "failed"
                _app.dicom_message = f"Export failed: {e}"
 
    threading.Thread(target=_export_worker, daemon=True).start()
    return _ok(message="DICOM export started.")
 
def _inject_dicom_tags(data: bytes, dtags: dict,
                        patient_name: str = "", patient_id: str = "",
                        patient_dob: str = "", patient_sex: str = "") -> bytes:
    """
    Inject patient-level DICOM tags into a DICOM file.
    DTX get_media_data() returns the raw DICOM binary but patient demographic
    tags (PatientName, PatientID, DOB, Sex) may be empty — DTX stores them
    separately and returns them via get_media_dicom_tags(). This function
    writes them into the file only if the existing tag value is empty.
    """
    import struct as _st
 
    def _set_tag_if_empty(d: bytes, grp: int, elm: int, vr: str, value: bytes) -> bytes:
        if not value:
            return d
        # Pad to even length
        pad = b' ' if vr in ('LO','PN','SH','CS','DA','TM','LT','ST') else b'\x00'
        if len(value) % 2:
            value += pad
 
        tag_bytes = _st.pack('<HH', grp, elm)
        pos = d.find(tag_bytes)
        if pos >= 0:
            try:
                existing_vr = d[pos+4:pos+6].decode('ascii', errors='?')
                if existing_vr in ('OB','OW','SQ','UC','UR','UT','UN'):
                    old_len = _st.unpack('<I', d[pos+8:pos+12])[0]
                    vs = pos + 12
                else:
                    old_len = _st.unpack('<H', d[pos+6:pos+8])[0]
                    vs = pos + 8
                # Only overwrite if currently blank/empty
                existing_val = d[vs:vs+old_len].rstrip(b'\x00 ')
                if existing_val:
                    return d  # already populated — don't touch it
                # Overwrite with new value (same length padding)
                new_val = value[:old_len].ljust(old_len, pad)
                return d[:vs] + new_val + d[vs+old_len:]
            except Exception:
                return d
        else:
            # Tag not present — insert before the first tag with a higher address
            insert_pos = 132
            try:
                i = 132
                while i < len(d) - 8:
                    g = _st.unpack('<H', d[i:i+2])[0]
                    e = _st.unpack('<H', d[i+2:i+4])[0]
                    if (g > grp) or (g == grp and e > elm):
                        insert_pos = i
                        break
                    evr = d[i+4:i+6].decode('ascii', errors='?')
                    if evr in ('OB','OW','SQ','UC','UR','UT','UN'):
                        l = _st.unpack('<I', d[i+8:i+12])[0]; i += 12 + l
                    else:
                        l = _st.unpack('<H', d[i+6:i+8])[0]; i += 8 + l
                    if g > 0x0050:  # don't scan past group 0050
                        break
            except Exception:
                pass
            new_tag = tag_bytes + vr.encode('ascii') + _st.pack('<H', len(value)) + value
            return d[:insert_pos] + new_tag + d[insert_pos:]
 
    sex_map = {"MALE": "M", "FEMALE": "F", "OTHER": "O"}
    sex_byte = sex_map.get((patient_sex or "").upper(), "").encode('ascii')
 
    if patient_name:
        data = _set_tag_if_empty(data, 0x0010, 0x0010, 'PN',
                                  patient_name[:64].encode('ascii', errors='replace'))
    if patient_id:
        data = _set_tag_if_empty(data, 0x0010, 0x0020, 'LO',
                                  patient_id[:64].encode('ascii', errors='replace'))
    if patient_dob and patient_dob.isdigit() and len(patient_dob) == 8:
        data = _set_tag_if_empty(data, 0x0010, 0x0030, 'DA',
                                  patient_dob.encode('ascii'))
    if sex_byte:
        data = _set_tag_if_empty(data, 0x0010, 0x0040, 'CS', sex_byte)
 
    return data

def _inject_dicom_tags(data: bytes, dtags: dict,
                        patient_name: str = "", patient_id: str = "",
                        patient_dob: str = "", patient_sex: str = "") -> bytes:
    """
    Inject patient demographic tags into a DICOM file.
    Only writes a tag if it is currently blank/empty in the file.
    """
    import struct as _st
 
    def _set_if_empty(d: bytes, grp: int, elm: int, vr: str, value: bytes) -> bytes:
        if not value:
            return d
        pad = b' ' if vr in ('LO','PN','SH','CS','DA','TM','LT','ST','UI') else b'\x00'
        if len(value) % 2:
            value = value + pad
        tag_bytes = _st.pack('<HH', grp, elm)
        pos = d.find(tag_bytes)
        if pos >= 0:
            try:
                evr = d[pos+4:pos+6].decode('ascii', errors='?')
                if evr in ('OB','OW','SQ','UC','UR','UT','UN'):
                    old_len = _st.unpack('<I', d[pos+8:pos+12])[0]
                    vs = pos + 12
                else:
                    old_len = _st.unpack('<H', d[pos+6:pos+8])[0]
                    vs = pos + 8
                if d[vs:vs+old_len].rstrip(b'\x00 '):
                    return d  # already has content — don't overwrite
                new_val = (value[:old_len]).ljust(old_len, pad)
                return d[:vs] + new_val + d[vs+old_len:]
            except Exception:
                return d
        else:
            # Tag missing — insert before first tag with higher address
            insert_pos = 132
            try:
                i = 132
                while i < len(d) - 8:
                    g = _st.unpack('<H', d[i:i+2])[0]
                    e = _st.unpack('<H', d[i+2:i+4])[0]
                    if g > grp or (g == grp and e > elm):
                        insert_pos = i
                        break
                    evr = d[i+4:i+6].decode('ascii', errors='?')
                    if evr in ('OB','OW','SQ','UC','UR','UT','UN'):
                        l = _st.unpack('<I', d[i+8:i+12])[0]; i += 12 + l
                    else:
                        l = _st.unpack('<H', d[i+6:i+8])[0]; i += 8 + l
                    if g > 0x0050:
                        break
            except Exception:
                pass
            new_tag = tag_bytes + vr.encode('ascii') + _st.pack('<H', len(value)) + value
            return d[:insert_pos] + new_tag + d[insert_pos:]
 
    sex_map = {"MALE": "M", "FEMALE": "F", "OTHER": "O"}
    sex_byte = sex_map.get((patient_sex or "").upper(), "").encode('ascii')
 
    if patient_name:
        data = _set_if_empty(data, 0x0010, 0x0010, 'PN',
                             patient_name[:64].encode('ascii', errors='replace'))
    if patient_id:
        data = _set_if_empty(data, 0x0010, 0x0020, 'LO',
                             patient_id[:64].encode('ascii', errors='replace'))
    if patient_dob and patient_dob.isdigit() and len(patient_dob) == 8:
        data = _set_if_empty(data, 0x0010, 0x0030, 'DA',
                             patient_dob.encode('ascii'))
    if sex_byte:
        data = _set_if_empty(data, 0x0010, 0x0040, 'CS', sex_byte)
    return data

def _anonymise_dicom(data: bytes, anon_id: str) -> bytes:
    """
    Basic DICOM anonymisation — blanks patient name, DOB, and ID tags.
    Replaces values with the anonymised ID string.
    This is a best-effort approach; use a dedicated deid tool for full compliance.
    """
    import struct as _st
 
    def _blank_tag(d: bytes, grp: int, elm: int, replacement: bytes) -> bytes:
        """Find and blank a specific DICOM tag value in-place."""
        tag_bytes = _st.pack('<HH', grp, elm)
        pos = d.find(tag_bytes)
        if pos < 0:
            return d
        try:
            vr = d[pos+4:pos+6].decode('ascii', errors='?')
            if vr in ('OB','OW','SQ','UC','UR','UT','UN'):
                length = _st.unpack('<I', d[pos+8:pos+12])[0]
                vs = pos + 12
            else:
                length = _st.unpack('<H', d[pos+6:pos+8])[0]
                vs = pos + 8
            # Pad replacement to same length
            rep = replacement[:length].ljust(length, b' ')
            return d[:vs] + rep + d[vs+length:]
        except Exception:
            return d
 
    anon_bytes = anon_id[:64].encode('ascii', errors='replace')
    data = _blank_tag(data, 0x0010, 0x0010, anon_bytes)  # PatientName
    data = _blank_tag(data, 0x0010, 0x0020, anon_bytes)  # PatientID
    data = _blank_tag(data, 0x0010, 0x0030, b'19000101')  # PatientBirthDate
    return data

@app.route("/api/dicom/status")
@require_auth
def api_dicom_status():
    with _state_lock:
        return _ok(
            status=_app.dicom_status,
            pct=_app.dicom_pct,
            message=_app.dicom_message,
        )

@app.route("/api/media/preview")
@require_auth
def api_media_preview():
    """
    Serve a thumbnail/preview for a media item by patient UID and media UID.
    Reads preview_path or file_path from the in-memory patient list.
    Returns the image bytes with the correct content-type.
    Query params: patient_uid, media_uid
    """
    from flask import Response
    patient_uid = request.args.get("patient_uid", "")
    media_uid   = request.args.get("media_uid", "")

    if not patient_uid or not media_uid:
        return _err("patient_uid and media_uid are required", 400)

    with _state_lock:
        patients = _app.loaded_patients

    # Find the patient
    patient = next((p for p in patients if p.get("uid") == patient_uid), None)
    if not patient:
        return _err("Patient not found", 404)

    # Find the media item
    media_item = None
    for study in patient.get("studies", {}).values():
        for series in study.get("series", {}).values():
            for m in series.get("media", []):
                if m.get("uid") == media_uid:
                    media_item = m
                    break

    if not media_item:
        return _err("Media not found", 404)

    # Try preview_path first (JPEG thumbnail), then file_path
    for path_key in ("preview_path", "file_path"):
        path = media_item.get(path_key)
        if path and os.path.isfile(path):
            try:
                import mimetypes
                ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
                # For DICOM files, we can't display directly — skip to next
                if "dicom" in ct or path.lower().endswith(".dcm"):
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                return Response(data, mimetype=ct,
                                headers={"Cache-Control": "max-age=300"})
            except Exception as e:
                logger.warning(f"Preview read failed for {path}: {e}")
                continue

    # No previewable file found — return a 1x1 transparent PNG placeholder
    import base64
    placeholder = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    return Response(placeholder, mimetype="image/png",
                    headers={"Cache-Control": "max-age=60"})

# ═══════════════════════════════════════════════════════════════════════════════
# API — VistaSoft: clear migrated data
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/clear_migrated", methods=["POST"])
@require_auth
def api_clear_migrated():
    """
    Delete SOURCE=1 patients (and all their studies/images/disk files)
    from the VistaSoft Firebird DB. Works for both local and UNC paths.
    """
    with _state_lock:
        target = _app.target

    if not hasattr(target, "_get_db_path"):
        return _err("Clear Migrated Data is only available for VistaSoft targets.")

    try:
        from datasources.fb_client import _run
        db_path_remote = target._get_db_path()
        images_path    = target._get_images_path()

        p_where = "(SELECT UID FROM PATIENT WHERE SOURCE=1)"
        s_where = ("(SELECT STUDY.UID FROM STUDY "
                    "JOIN PATIENT ON STUDY.PATIENTUID=PATIENT.UID "
                    "WHERE PATIENT.SOURCE=1)")
        i_where = ("(SELECT IMAGE.UID FROM IMAGE "
                    "JOIN STUDY ON IMAGE.STUDYUID=STUDY.UID "
                    "JOIN PATIENT ON STUDY.PATIENTUID=PATIENT.UID "
                    "WHERE PATIENT.SOURCE=1)")

        stmts = [
            f"DELETE FROM PRESENTATIONSTATE WHERE IMAGEUID IN {i_where}",
            f"DELETE FROM IMAGE WHERE STUDYUID IN {s_where}",
            f"DELETE FROM STUDY WHERE PATIENTUID IN {p_where}",
            f"DELETE FROM PATIENTREGISTRATION WHERE PATIENTUID IN {p_where}",
            f"DELETE FROM WORKITEM WHERE PATIENTUID IN {p_where}",
            f"DELETE FROM IMPLANT WHERE PATIENTUID IN {p_where}",
            f"DELETE FROM PATIENTTRANSFER WHERE PATIENTUID IN {p_where}",
            f"DELETE FROM PATIENTCONSENT WHERE PATIENTUID IN {p_where}",
            "DELETE FROM PATIENT WHERE SOURCE=1",
        ]

        deleted = 0
        errors_list = []
        with _local_db_path(db_path_remote) as db_path:
            for sql in stmts:
                try:
                    _run("execute", db_path, sql)
                    deleted += 1
                except Exception as e:
                    errors_list.append(str(e))
                    logger.warning(f"clear_migrated stmt failed: {e}")

        # Delete disk folders for SOURCE=1 patients.
        # Each patient folder is named by their UID — collect them first,
        # then remove after the DB is cleaned.
        if os.path.isdir(images_path):
            removed_dirs = 0
            for entry in os.scandir(images_path):
                if entry.is_dir():
                    # Check for Patient.json with PatientSource=Import
                    pj = os.path.join(entry.path, "Patient.json")
                    try:
                        import json as _j
                        with open(pj, encoding="utf-8-sig") as f:
                            pdata = _j.load(f)
                        if pdata.get("PatientSource") == "Import":
                            import shutil
                            shutil.rmtree(entry.path, ignore_errors=True)
                            removed_dirs += 1
                    except Exception:
                        pass
            logger.info(f"Removed {removed_dirs} patient image folder(s).")

        _app.store.audit("clear_migrated_data",
                            f"db={db_path_remote}",
                            user_email=_current_user().get("email"))
        logger.info(f"Cleared migrated data: {deleted} SQL statements executed.")
        msg = "Migrated data cleared from VistaSoft DB."
        if errors_list:
            msg += f" ({len(errors_list)} minor errors — some tables may not exist)"
        return _ok(message=msg)

    except Exception as e:
        logger.exception("Clear migrated data failed")
        return _err(str(e))

# ═══════════════════════════════════════════════════════════════════════════════
# API — Audit log
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/audit")
@require_auth
def api_audit():
    session_id = request.args.get("session_id")
    entries = _app.store.get_audit_log(session_id=session_id, limit=500)
    return _ok(entries=entries)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    logger.info(f"IT INFINITY Migration Tool server starting on port {port}")
    app.config["SERVER_PORT"] = port
    app.run(host="localhost", port=port, debug=debug, use_reloader=False)