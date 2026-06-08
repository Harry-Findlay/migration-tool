"""
core/engine.py
==============
Migration orchestration engine — unchanged from original.
Exposes run / pause / resume / cancel.
Called from the Flask server in a background thread; progress is
reported via callbacks that update the in-memory MigrationState object.
"""

import sys
import os
import threading
import logging
from typing import Callable, Optional

from .models import MigrationResult, MigrationStatus
from .migration_store import MigrationStore


class MigrationEngine:
    def __init__(self, store: Optional[MigrationStore] = None):
        self.logger = logging.getLogger("MigrationEngine")
        self._cancelled   = False
        self._paused      = False
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._thread: Optional[threading.Thread] = None
        self.store = store or MigrationStore()
        self.current_session_id: Optional[str] = None

    # ── Public control API ─────────────────────────────────────────────────────

    def run(self, source, target,
            max_parallelism: int = 4,
            incremental: bool = False,
            resume_session_id: Optional[str] = None,
            on_progress: Optional[Callable] = None,
            on_complete: Optional[Callable] = None,
            on_error: Optional[Callable] = None):

        self._cancelled = False
        self._paused    = False
        self._pause_event.set()

        self._thread = threading.Thread(
            target=self._execute,
            args=(source, target, max_parallelism, incremental,
                  resume_session_id, on_progress, on_complete, on_error),
            daemon=True,
        )
        self._thread.start()

    def pause(self):
        if not self._paused:
            self._paused = True
            self._pause_event.clear()
            self.logger.info("Migration pause requested.")

    def resume(self):
        if self._paused:
            self._paused = False
            self._pause_event.set()
            self.logger.info("Migration resumed.")

    def cancel(self):
        self._cancelled = True
        self._pause_event.set()
        self.logger.info("Migration cancellation requested.")

    def is_paused(self) -> bool:
        return self._paused

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Internal ───────────────────────────────────────────────────────────────
     
    @staticmethod
    def _apply_pms_overrides(patients: list, pms_patients: list, data_source: str) -> list:
        """Apply PMS demographic overrides to source patients before migration."""
        pms_by_uid = {p["source_uid"]: p for p in pms_patients if p.get("source_uid")}
        if not pms_by_uid:
            return patients

        def _resolve(field_key, pms_val, src_val, field_overrides):
            override = (field_overrides or {}).get(field_key)
            if override == "pms":    return pms_val or src_val
            if override == "source": return src_val or pms_val
            if data_source == "pms":    return pms_val or src_val
            if data_source == "source": return src_val or pms_val
            return src_val or pms_val  # merged

        result = []
        for sp in patients:
            pp = pms_by_uid.get(sp.get("uid", ""))
            if not pp:
                result.append(sp)
                continue
            fo  = pp.get("field_overrides", {})
            src_ref = sp.get("id") or sp.get("patient_ref") or ""
            merged = dict(sp)
            merged["family_name"] = _resolve("surname",    pp.get("surname",""),     sp.get("family_name",""), fo)
            merged["given_names"] = _resolve("first_name", pp.get("first_name",""),  sp.get("given_names",""),  fo)
            merged["birth_date"]  = _resolve("dob",        pp.get("dob",""),          sp.get("birth_date") or sp.get("dob",""), fo)
            merged["id"]          = _resolve("patient_ref",pp.get("patient_ref",""), src_ref, fo)
            merged["pms_id"]      = merged["id"]
            result.append(merged)
        return result

    def _cancel_flag(self) -> bool:
        return self._cancelled

    def _cancel_or_pause_flag(self) -> bool:
        return self._cancelled or self._paused

    def _check_pause_or_cancel(self) -> bool:
        self._pause_event.wait()
        return self._cancelled

    def _execute(self, source, target, max_parallelism, incremental,
                 resume_session_id, on_progress, on_complete, on_error):

        result = MigrationResult(status=MigrationStatus.RUNNING)

        def _progress(cur, tot, msg, counters=None):
            if on_progress:
                on_progress(cur, tot, msg, counters)

        try:
            _progress(0, 100, "Validating source configuration…")
            valid, msg = source.validate()
            if not valid:
                raise ValueError(f"Source validation failed: {msg}")

            _progress(2, 100, "Validating target configuration…")
            valid, msg = target.validate()
            if not valid:
                raise ValueError(f"Target validation failed: {msg}")

            _progress(5, 100, "Testing connection to target…")
            ok, msg = target.test_connection()
            if not ok:
                raise ConnectionError(msg)
            _progress(10, 100, f"Connected — {msg}")

            if resume_session_id:
                _progress(12, 100, "Resuming paused migration — loading remaining patients…")
                paused_session = self.store.get_session(resume_session_id)
                if not paused_session or paused_session["status"] != "paused":
                    raise ValueError(f"Session {resume_session_id} is not in a paused state.")
                patients = self.store.get_pending_patients(resume_session_id)
                session_id = resume_session_id
                self.store.update_session_status(session_id, "running", message="Resumed")
                _progress(15, 100, f"Resuming with {len(patients)} remaining patient(s)…")
            else:
                _progress(15, 100, "Loading patients from source…")

                def source_progress(current, total, message):
                    if self._cancelled or self._paused:
                        return
                    pct = 15 + int((current / max(total, 1)) * 30)
                    _progress(pct, 100, message)

                all_patients = source.load(
                    cancel_flag=self._cancel_or_pause_flag,
                    max_parallelism=max_parallelism,
                    progress_callback=source_progress,
                )

                # ── Apply PMS overrides if confirmed ──────────────────────
                if getattr(source, '_pms_patients', None):
                    all_patients = self._apply_pms_overrides(
                        all_patients,
                        source._pms_patients,
                        source._pms_data_source,
                    )

                if self._cancelled:
                    result.status  = MigrationStatus.CANCELLED
                    result.message = "Migration was cancelled during source load."
                    if on_complete:
                        on_complete(result)
                    return

                if incremental:
                    migrated = self.store.get_migrated_uids(source.name, target.name)
                    before   = len(all_patients)
                    patients = [p for p in all_patients if p["uid"] not in migrated]
                    skipped  = before - len(patients)
                    _progress(47, 100,
                              f"Incremental mode: {skipped} already migrated, "
                              f"{len(patients)} to process.")
                    result.patients_skipped = skipped
                else:
                    patients = all_patients

                if not patients:
                    result.status  = MigrationStatus.COMPLETED
                    result.message = (
                        "Nothing to migrate — all patients are already up to date."
                        if incremental else "No patients found in source."
                    )
                    if on_complete:
                        on_complete(result)
                    return

                session_id = self.store.create_session(
                    source_key=source.name,
                    target_key=target.name,
                    total_patients=len(patients),
                    is_incremental=incremental,
                )
                self.store.bulk_insert_patients(session_id, patients)
                _progress(50, 100, f"Read {len(patients)} patient(s). Starting import…")

            self.current_session_id = session_id
            result.session_id       = session_id
            result.patients_processed = len(patients)

            total  = len(patients)
            done   = 0
            failed = 0
            media_uploaded = 0
            media_missing  = 0
            errors = []

            for patient in patients:
                if self._paused:
                    self.store.update_session_status(
                        session_id, "paused",
                        message=f"Paused after {done}/{total} patients.",
                        done_patients=done, failed_patients=failed,
                        media_uploaded=media_uploaded, media_missing=media_missing,
                    )
                    result.status  = MigrationStatus.PAUSED
                    result.message = f"Migration paused after {done} of {total} patient(s)."
                    result.patients_remaining = total - done
                    result.patients_failed    = failed
                    result.media_uploaded     = media_uploaded
                    result.media_missing      = media_missing
                    if on_complete:
                        on_complete(result)
                    return

                if self._cancelled:
                    self.store.update_session_status(
                        session_id, "cancelled",
                        message=f"Cancelled after {done}/{total} patients.",
                        done_patients=done, failed_patients=failed,
                    )
                    result.status  = MigrationStatus.CANCELLED
                    result.message = "Migration was cancelled."
                    result.patients_failed = failed
                    if on_complete:
                        on_complete(result)
                    return

                uid  = patient.get("uid", "")
                name = (
                    f"{patient.get('given_names', '')} "
                    f"{patient.get('family_name', '')}".strip() or uid
                )

                self.store.set_patient_status(session_id, uid, "in_progress")

                try:
                    if hasattr(source, "reattach_fetch"):
                        source.reattach_fetch([patient])

                    write_result = target.write_patients(
                        [patient],
                        progress_callback=None,
                        cancel_flag=self._cancel_or_pause_flag,
                        incremental=incremental,
                    )
                    if write_result.get("errors", 0):
                        raise RuntimeError(
                            f"Write failed for {uid[:8]}… "
                            f"(errors={write_result['errors']})"
                        )

                    self.store.set_patient_status(session_id, uid, "completed")

                    pu = write_result.get("media_uploaded", write_result.get("written", 0))
                    pm = write_result.get("media_missing",  write_result.get("skipped", 0))

                    if pm == 0:
                        self.store.mark_migrated(source.name, target.name, uid, session_id)

                    media_uploaded += pu
                    media_missing  += pm
                    done += 1

                    pct = 50 + int((done / max(total, 1)) * 46)
                    _progress(pct, 100, f"Imported: {name} ({done}/{total})", {
                        "processed": done + failed,
                        "imported":  done,
                        "failed":    failed,
                        "images":    media_uploaded,
                        "skipped":   result.patients_skipped,
                    })

                except Exception as exc:
                    failed += 1
                    err_str = str(exc)
                    errors.append(f"{name}: {err_str}")
                    self.store.set_patient_status(session_id, uid, "failed", err_str)
                    self.logger.error(f"Failed to migrate {name}: {exc}")
                    pct = 50 + int(((done + failed) / max(total, 1)) * 46)
                    _progress(pct, 100, f"Failed: {name} — {exc}", {
                        "processed": done + failed,
                        "imported":  done,
                        "failed":    failed,
                        "images":    media_uploaded,
                        "skipped":   result.patients_skipped,
                    })

            self.store.update_session_status(
                session_id, "completed",
                message=f"{done} imported, {failed} failed.",
                done_patients=done, failed_patients=failed,
                media_uploaded=media_uploaded, media_missing=media_missing,
            )

            _progress(100, 100, "Migration complete.")
            result.status          = MigrationStatus.COMPLETED
            result.patients_failed = failed
            result.media_uploaded  = media_uploaded
            result.media_missing   = media_missing
            result.errors          = errors
            result.message = (
                f"Migration completed. {done} patient(s) imported, {failed} failed. "
                f"{media_uploaded} image(s) uploaded, {media_missing} not found on disk."
            )

        except Exception as exc:
            self.logger.exception("Migration error")
            if self.current_session_id:
                self.store.update_session_status(
                    self.current_session_id, "failed", message=str(exc)
                )
            result.status  = MigrationStatus.FAILED
            result.message = str(exc)
            result.errors.append(str(exc))
            if on_error:
                on_error(str(exc))
            return

        if on_complete:
            on_complete(result)
