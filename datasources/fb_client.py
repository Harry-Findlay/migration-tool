"""
datasources/fb_client.py
========================
Firebird connection via FbBridge.exe — a tiny self-contained .NET executable
that uses FirebirdSql.Data.FirebirdClient with ServerType=1 (embedded).

ServerType=1 opens the .fdb file directly — no TCP port or Firebird service needed.

In this project layout FbBridge.exe and all its companion DLLs live in:
    lib/fb_bridge/FbBridge.exe

BUILD ONCE (requires .NET 8 SDK):
    cd lib/fb_bridge
    dotnet publish -c Release -r win-x64 --self-contained true ^
        -o .
"""

import os
import json
import subprocess
import logging

logger  = logging.getLogger("fb_client")

# Resolve lib/fb_bridge relative to this file:
#   datasources/fb_client.py  →  project root  →  lib/fb_bridge/
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
_ROOT       = os.path.dirname(_THIS_DIR)          # project root
_LIB_DIR    = os.path.join(_ROOT, "lib", "fb_bridge")
_EXE        = os.path.join(_LIB_DIR, "FbBridge.exe")

_NEED_BUILD = (
    "FbBridge.exe not found at: {path}\n\n"
    "Build it once (requires .NET 8 SDK):\n"
    "  cd lib\\fb_bridge\n"
    "  dotnet publish -c Release -r win-x64 --self-contained true -o .\n\n"
    "Then restart the server."
)

# Firebird 2.5 embedded bridge — used for ODS 11.2 databases (e.g. DBSWin)
_LIB_DIR_V2 = os.path.join(_ROOT, "lib", "fb_bridge_v2")
_EXE_V2     = os.path.join(_LIB_DIR_V2, "FbBridge.exe")

def _run_v2(command: str, db_path: str, *extra) -> dict:
    """Same as _run() but uses the Firebird 2.5 embedded client in lib/fb_bridge_v2/."""
    if not os.path.isfile(_EXE_V2):
        raise FileNotFoundError(
            f"FbBridge.exe not found at: {_EXE_V2}\n\n"
            f"Copy FbBridge.exe and Firebird 2.5 embedded DLLs into lib\\fb_bridge_v2\\"
        )

    env = os.environ.copy()
    env["FIREBIRD"] = _LIB_DIR_V2
    env.setdefault("PATH", "")
    env["PATH"] = _LIB_DIR_V2 + os.pathsep + env["PATH"]

    stdin_data = None
    safe_extra = []
    for arg in extra:
        if arg and isinstance(arg, str) and arg.strip().startswith("{"):
            stdin_data = arg
            safe_extra.append("-")
        else:
            safe_extra.append(arg)

    cmd = [_EXE_V2, command, db_path] + safe_extra
    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=_LIB_DIR_V2,
        )
        out = result.stdout.strip()
        if not out:
            raise RuntimeError(
                f"FbBridge (v2) produced no output.\nstderr: {result.stderr[:400]}"
            )
        data = json.loads(out)
        if not data.get("ok"):
            err = data.get("error", "Unknown FbBridge error")
            if result.stderr.strip():
                err += f"\n[stderr]: {result.stderr.strip()[:300]}"
            raise ConnectionError(err)
        return data.get("data", data)
    except subprocess.TimeoutExpired:
        raise ConnectionError("FbBridge (v2) timed out after 60 seconds")
    except json.JSONDecodeError:
        raise RuntimeError(f"FbBridge (v2) returned invalid JSON: {out[:200]}")

def _run(command: str, db_path: str, *extra) -> dict:
    if not os.path.isfile(_EXE):
        raise FileNotFoundError(_NEED_BUILD.format(path=_EXE))

    env = os.environ.copy()
    # Tell Firebird embedded where its DLLs are
    env["FIREBIRD"] = _LIB_DIR
    env.setdefault("PATH", "")
    env["PATH"] = _LIB_DIR + os.pathsep + env["PATH"]

    # Pass JSON args via stdin to avoid Windows quoting issues
    stdin_data = None
    safe_extra = []
    for arg in extra:
        if arg and isinstance(arg, str) and arg.strip().startswith("{"):
            stdin_data = arg
            safe_extra.append("-")
        else:
            safe_extra.append(arg)

    cmd = [_EXE, command, db_path] + safe_extra
    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=_LIB_DIR,   # run from its own directory so relative DLL paths work
        )
        out = result.stdout.strip()
        if not out:
            raise RuntimeError(
                f"FbBridge produced no output.\nstderr: {result.stderr[:400]}"
            )
        data = json.loads(out)
        if not data.get("ok"):
            err = data.get("error", "Unknown FbBridge error")
            if result.stderr.strip():
                err += f"\n[stderr]: {result.stderr.strip()[:300]}"
            raise ConnectionError(err)
        return data.get("data", data)
    except subprocess.TimeoutExpired:
        raise ConnectionError("FbBridge timed out after 60 seconds")
    except json.JSONDecodeError:
        raise RuntimeError(f"FbBridge returned invalid JSON: {out[:200]}")


def run_diag() -> str:
    """Run FbBridge diag and return a formatted string for the log / API."""
    if not os.path.isfile(_EXE):
        return f"FbBridge.exe not found at: {_EXE}"
    env = os.environ.copy()
    env["FIREBIRD"] = _LIB_DIR
    try:
        result = subprocess.run(
            [_EXE, "diag", "."],
            capture_output=True, text=True, timeout=10,
            env=env, cwd=_LIB_DIR,
        )
        out = result.stdout.strip()
        if not out:
            return f"diag returned no output. stderr: {result.stderr[:200]}"
        data = json.loads(out)
        d = data.get("data", data)
        lines = [
            f"exe_dir      : {d.get('exe_dir')}",
            f"FIREBIRD env : {d.get('firebird_env')}",
            f"engine13.dll : {'YES' if d.get('engine13_exists') else 'MISSING'}",
            f"fbclient.dll : {'YES' if d.get('fbclient_exists') else 'MISSING'}",
            f"root DLLs    : {[os.path.basename(x) for x in d.get('dlls_in_exe_dir', [])]}",
            f"plugin DLLs  : {[os.path.basename(x) for x in d.get('dlls_in_plugins', [])]}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Diag error: {e}"


def _get_db_copy(db_path: str) -> str:
    """Make a temp copy of the .fdb even while VistaSoft has it open."""
    data = _run("copy", db_path)
    return data["dest"]


def connect_path(db_path: str):
    return _Connection(db_path, cleanup=False)


class _Connection:
    def __init__(self, db_path: str, cleanup: bool = False):
        self.db_path  = db_path
        self._cleanup = cleanup

    def cursor(self):
        return _Cursor(self.db_path)

    def close(self):
        if self._cleanup and os.path.isfile(self.db_path):
            try:
                os.remove(self.db_path)
                logger.info(f"Removed temp db copy: {self.db_path}")
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _Cursor:
    def __init__(self, db_path: str):
        self.db_path     = db_path
        self._images_dir = ""
        self._rows       = []
        self._file_paths = []
        self._pos        = 0

    def execute(self, sql: str, params: tuple = ()):
        sql_norm = " ".join(sql.split()).upper()

        if "COUNT(*)" in sql_norm and "PATIENT" in sql_norm:
            data = _run("test", self.db_path)
            self._rows = [(data["count"],)]

        elif sql_norm.startswith("SELECT UID FROM PATIENT"):
            data = _run("patients", self.db_path)
            self._rows = [(uid,) for uid in data["patients"]]

        elif "FROM PATIENT WHERE UID" in sql_norm:
            import re
            m = re.search(r"X'([0-9A-Fa-f]+)'", sql)
            uid_hex = (
                m.group(1) if m
                else (params[0].replace("-", "") if params else "")
            )
            data = _run("patient", self.db_path, uid_hex)
            p = data
            self._rows = [(
                p.get("uid"), p.get("id"), p.get("birth_date"),
                p.get("given_names"), p.get("family_name"), p.get("sex"),
            )]

        elif "FROM IMAGE" in sql_norm and "STUDY" in sql_norm:
            import re
            m = re.search(r"X'([0-9A-Fa-f]+)'", sql)
            uid_hex = (
                m.group(1) if m
                else (params[0].replace("-", "") if params else "")
            )
            data = _run("images", self.db_path, uid_hex, self._images_dir)
            imgs = data.get("images", [])
            self._rows = [
                (
                    i.get("image_uid"), i.get("study_uid"), i.get("sop_instance"),
                    i.get("image_class"), i.get("comments"), i.get("acq_datetime"),
                    i.get("study_instance"), i.get("study_datetime"),
                    i.get("accession"), i.get("description"), i.get("ref_physician"),
                )
                for i in imgs
            ]
            self._file_paths = [i.get("file_path", "") for i in imgs]

        else:
            raise NotImplementedError(f"Unsupported SQL: {sql[:100]}")

        self._pos = 0

    def fetchone(self):
        if self._pos < len(self._rows):
            r = self._rows[self._pos]
            self._pos += 1
            return r
        return None

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows
