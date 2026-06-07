"""
core/migration_store.py
=======================
SQLite-backed store — unchanged from original.
Tracks sessions, per-patient state, and incremental migration history.
"""

import os
import sqlite3
import json
import uuid
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("MigrationStore")


def _get_db_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "ITInfinityMigrator")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "migration_state.db")


class MigrationStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _get_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._ensure_schema()

    def _connect(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _ensure_schema(self):
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id      TEXT PRIMARY KEY,
                source_key      TEXT NOT NULL,
                target_key      TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'running',
                total_patients  INTEGER DEFAULT 0,
                done_patients   INTEGER DEFAULT 0,
                failed_patients INTEGER DEFAULT 0,
                media_uploaded  INTEGER DEFAULT 0,
                media_missing   INTEGER DEFAULT 0,
                message         TEXT DEFAULT '',
                is_incremental  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS patient_state (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                patient_uid     TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                patient_json    TEXT,
                error_msg       TEXT DEFAULT '',
                updated_at      TEXT NOT NULL,
                UNIQUE(session_id, patient_uid)
            );

            CREATE INDEX IF NOT EXISTS idx_ps_session  ON patient_state(session_id);
            CREATE INDEX IF NOT EXISTS idx_ps_status   ON patient_state(session_id, status);

            CREATE TABLE IF NOT EXISTS migrated_ids (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key      TEXT NOT NULL,
                target_key      TEXT NOT NULL,
                patient_uid     TEXT NOT NULL,
                session_id      TEXT NOT NULL,
                migrated_at     TEXT NOT NULL,
                UNIQUE(source_key, target_key, patient_uid)
            );

            CREATE INDEX IF NOT EXISTS idx_mid_pair ON migrated_ids(source_key, target_key);

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                user_email  TEXT,
                action      TEXT NOT NULL,
                detail      TEXT DEFAULT '',
                logged_at   TEXT NOT NULL
            );
        """)
        c.commit()

    # ── Sessions ───────────────────────────────────────────────────────────────

    def create_session(self, source_key: str, target_key: str,
                       total_patients: int, is_incremental: bool = False) -> str:
        sid = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            """INSERT INTO sessions
               (session_id, source_key, target_key, created_at, updated_at,
                status, total_patients, is_incremental)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sid, source_key, target_key, now, now, "running",
             total_patients, int(is_incremental)),
        )
        self._conn.commit()
        return sid

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, source_key: str = None, target_key: str = None,
                      limit: int = 50) -> list:
        q = "SELECT * FROM sessions WHERE 1=1"
        params = []
        if source_key:
            q += " AND source_key=?"; params.append(source_key)
        if target_key:
            q += " AND target_key=?"; params.append(target_key)
        q += f" ORDER BY created_at DESC LIMIT {limit}"
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def update_session_status(self, session_id: str, status: str,
                              message: str = "", **counters):
        sets = ["status=?", "updated_at=?", "message=?"]
        vals = [status, _now(), message]
        for col in ("done_patients", "failed_patients", "media_uploaded", "media_missing"):
            if col in counters:
                sets.append(f"{col}=?")
                vals.append(counters[col])
        vals.append(session_id)
        self._conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id=?", vals
        )
        self._conn.commit()

    def delete_session(self, session_id: str):
        self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        self._conn.commit()

    # ── Patient state ──────────────────────────────────────────────────────────

    @staticmethod
    def _strip_non_serialisable(patient: dict) -> dict:
        import copy
        p = copy.deepcopy(patient)
        for study in p.get("studies", {}).values():
            for series in study.get("series", {}).values():
                for media in series.get("media", []):
                    for key in list(media.keys()):
                        if key.startswith("_") or callable(media.get(key)):
                            del media[key]
        return p

    def bulk_insert_patients(self, session_id: str, patients: list):
        now = _now()
        rows = [
            (session_id, p["uid"], "pending",
             json.dumps(self._strip_non_serialisable(p)), "", now)
            for p in patients
        ]
        self._conn.executemany(
            """INSERT OR IGNORE INTO patient_state
               (session_id, patient_uid, status, patient_json, error_msg, updated_at)
               VALUES (?,?,?,?,?,?)""",
            rows,
        )
        self._conn.commit()

    def set_patient_status(self, session_id: str, patient_uid: str,
                           status: str, error_msg: str = ""):
        self._conn.execute(
            """UPDATE patient_state
               SET status=?, error_msg=?, updated_at=?
               WHERE session_id=? AND patient_uid=?""",
            (status, error_msg, _now(), session_id, patient_uid),
        )
        self._conn.commit()

    def get_pending_patients(self, session_id: str) -> list:
        rows = self._conn.execute(
            """SELECT patient_json FROM patient_state
               WHERE session_id=? AND status IN ('pending','in_progress')
               ORDER BY id""",
            (session_id,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                result.append(json.loads(r["patient_json"]))
            except Exception:
                pass
        return result

    def get_all_patient_states(self, session_id: str) -> list:
        rows = self._conn.execute(
            """SELECT patient_uid, status, error_msg, updated_at
               FROM patient_state WHERE session_id=? ORDER BY id""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_by_status(self, session_id: str) -> dict:
        rows = self._conn.execute(
            """SELECT status, COUNT(*) as n FROM patient_state
               WHERE session_id=? GROUP BY status""",
            (session_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ── Incremental / diff tracking ────────────────────────────────────────────

    def mark_migrated(self, source_key: str, target_key: str,
                      patient_uid: str, session_id: str):
        self._conn.execute(
            """INSERT OR REPLACE INTO migrated_ids
               (source_key, target_key, patient_uid, session_id, migrated_at)
               VALUES (?,?,?,?,?)""",
            (source_key, target_key, patient_uid, session_id, _now()),
        )
        self._conn.commit()

    def get_migrated_uids(self, source_key: str, target_key: str) -> set:
        rows = self._conn.execute(
            "SELECT patient_uid FROM migrated_ids WHERE source_key=? AND target_key=?",
            (source_key, target_key),
        ).fetchall()
        return {r["patient_uid"] for r in rows}

    def clear_migrated_ids(self, source_key: str, target_key: str):
        self._conn.execute(
            "DELETE FROM migrated_ids WHERE source_key=? AND target_key=?",
            (source_key, target_key),
        )
        self._conn.execute(
            "DELETE FROM sessions WHERE source_key=? AND target_key=?",
            (source_key, target_key),
        )
        self._conn.commit()

    def count_migrated(self, source_key: str, target_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as n FROM migrated_ids WHERE source_key=? AND target_key=?",
            (source_key, target_key),
        ).fetchone()
        return row["n"] if row else 0

    def get_paused_session(self, source_key: str, target_key: str) -> Optional[dict]:
        row = self._conn.execute(
            """SELECT * FROM sessions
               WHERE source_key=? AND target_key=? AND status='paused'
               ORDER BY updated_at DESC LIMIT 1""",
            (source_key, target_key),
        ).fetchone()
        return dict(row) if row else None

    # ── Audit log ──────────────────────────────────────────────────────────────

    def audit(self, action: str, detail: str = "",
              session_id: str = None, user_email: str = None):
        self._conn.execute(
            """INSERT INTO audit_log (session_id, user_email, action, detail, logged_at)
               VALUES (?,?,?,?,?)""",
            (session_id, user_email, action, detail, _now()),
        )
        self._conn.commit()

    def get_audit_log(self, session_id: str = None, limit: int = 200) -> list:
        q = "SELECT * FROM audit_log"
        params = []
        if session_id:
            q += " WHERE session_id=?"; params.append(session_id)
        q += f" ORDER BY logged_at DESC LIMIT {limit}"
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
