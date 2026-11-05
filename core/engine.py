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

# Set on the worker thread while a patient is being written, so log records
# from sources / targets can be tied to the patient in the in-app log.
migration_context = threading.local()


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
            on_error: Optional[Callable] = None,
            filters: Optional[dict] = None,
            preloaded: Optional[list] = None):

        if self.is_running():
            raise RuntimeError("A migration is already running or paused — resume or cancel it first.")

        self._cancelled = False
        self._paused    = False
        self._pause_event.set()
        self._filters   = filters or {}
        # Patients already loaded for the UI (same source + settings) — saves
        # reading the whole source a second time at the start of the run.
        self._preloaded = preloaded

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

    # Filters page → image classes (image_class values used by every source)
    _CLASS_FILTER_KEYS = {
        "Pano": "pano", "Ceph": "ceph",
        "Intra": "intra", "Xray": "intra",
        "Dvt": "dvt",
        "Snapshot": "photo", "Proof": "photo",
        "Video": "video",
    }

    @classmethod
    def _apply_filters(cls, patients: list, filters: dict) -> tuple:
        """
        Apply the Filters page: surname prefix, included image types, and
        skipping patients left with no media. Returns (patients, summary).
        Builds new patient / study / series dicts so the loaded data the
        server keeps for the UI is not modified.
        """
        if not filters:
            return patients, ""
        prefix  = (filters.get("surname_prefix") or "").strip().lower()
        types   = filters.get("media_types")          # {"pano": True, ...} or None
        skip_no = bool(filters.get("skip_no_media"))

        out, dropped_media, dropped_pat = [], 0, 0
        for p in patients:
            if prefix and not (p.get("family_name") or "").strip().lower().startswith(prefix):
                continue
            if types:
                studies = {}
                for sk, st in (p.get("studies") or {}).items():
                    series = {}
                    for rk, se in (st.get("series") or {}).items():
                        keep = []
                        for m in se.get("media") or []:
                            key = cls._CLASS_FILTER_KEYS.get(m.get("image_class") or "")
                            if key is None or types.get(key, True):
                                keep.append(m)
                            else:
                                dropped_media += 1
                        if keep:
                            series[rk] = dict(se, media=keep)
                    if series:
                        studies[sk] = dict(st, series=series)
                removed = sum(len(se.get("media") or []) for st in (p.get("studies") or {}).values()
                              for se in (st.get("series") or {}).values()) - \
                          sum(len(se.get("media") or []) for st in studies.values()
                              for se in st["series"].values())
                p = dict(p, studies=studies)
                if removed:
                    # Kept in the stored patient JSON (no leading "_"), so a
                    # resumed session still knows images were held back.
                    p["filtered_media"] = removed
            if skip_no and not any(se.get("media") for st in (p.get("studies") or {}).values()
                                   for se in (st.get("series") or {}).values()):
                dropped_pat += 1
                continue
            out.append(p)

        parts = []
        if prefix:
            parts.append(f"surname starts with '{filters.get('surname_prefix').strip()}'")
        if dropped_media:
            parts.append(f"{dropped_media} image(s) excluded by type")
        if dropped_pat:
            parts.append(f"{dropped_pat} patient(s) without images skipped")
        return out, "; ".join(parts)

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

                preloaded, self._preloaded = self._preloaded, None
                if preloaded:
                    all_patients = list(preloaded)
                    _progress(45, 100, f"Using the {len(all_patients)} patient(s) already loaded from the source.")
                else:
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

                before_filters = len(all_patients)
                all_patients, filter_note = self._apply_filters(all_patients, getattr(self, "_filters", {}))
                if filter_note:
                    _progress(46, 100, f"Filters: {len(all_patients)} of {before_filters} patient(s) selected — {filter_note}.")

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

            # ── Patient loop ───────────────────────────────────────────────
            # Targets that can take concurrent writes (each call is
            # self-contained, e.g. DTX Studio's REST API) set
            # supports_parallel_writes = True and get several patients in
            # flight at once. Others (VistaSoft: one Firebird embedded
            # process at a time) keep the original one-at-a-time behaviour.
            # All store / progress updates stay on this thread.
            import concurrent.futures as _cf

            parallel = bool(getattr(target, "supports_parallel_writes", False))
            workers  = max(1, min(int(max_parallelism or 1), 16)) if parallel else 1
            if workers > 1:
                self.logger.info(f"Writing up to {workers} patients in parallel.")

            def _name_of(p):
                return (f"{p.get('given_names', '')} {p.get('family_name', '')}".strip()
                        or p.get("uid", ""))

            def _write_one(p):
                migration_context.patient = _name_of(p)
                migration_context.uid     = p.get("uid", "")
                try:
                    if hasattr(source, "reattach_fetch"):
                        source.reattach_fetch([p])
                    return target.write_patients(
                        [p],
                        progress_callback=None,
                        cancel_flag=self._cancel_or_pause_flag,
                        incremental=incremental,
                    )
                finally:
                    migration_context.patient = None
                    migration_context.uid     = None

            def _counters(event=None):
                c = {
                    "processed":     done + failed,
                    "imported":      done,
                    "failed":        failed,
                    "images":        media_uploaded,
                    "media_missing": media_missing,
                    "skipped":       result.patients_skipped,
                }
                if event:
                    c["event"] = event    # consumed by the server's log, not stored
                return c

            pending   = iter(patients)
            inflight  = {}   # future -> patient
            exhausted = False
            last_saved = 0

            def _save_counters(status="running", message=""):
                self.store.update_session_status(
                    session_id, status, message=message,
                    done_patients=done, failed_patients=failed,
                    media_uploaded=media_uploaded, media_missing=media_missing,
                )

            with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
                while True:
                    # Top up the pool unless pausing / cancelling
                    while (len(inflight) < workers and not exhausted
                           and not self._paused and not self._cancelled):
                        patient = next(pending, None)
                        if patient is None:
                            exhausted = True
                            break
                        self.store.set_patient_status(session_id, patient.get("uid", ""), "in_progress")
                        inflight[pool.submit(_write_one, patient)] = patient

                    if not inflight:
                        if exhausted or self._cancelled:
                            break
                        if self._paused:
                            # Everything in flight has finished. Record the pause
                            # (so it can be resumed from history after a restart)
                            # and wait here for Resume or Cancel.
                            _save_counters("paused", f"Paused after {done}/{total} patients.")
                            _progress(50 + int(((done + failed) / max(total, 1)) * 46), 100,
                                      f"Paused after {done + failed} of {total} patient(s).", _counters())
                            self._pause_event.wait()
                            if self._cancelled:
                                break
                            _save_counters("running", "Resumed")
                            _progress(50 + int(((done + failed) / max(total, 1)) * 46), 100,
                                      "Resumed.", _counters())
                            continue
                        break

                    # Keep the session row current so history is right even after a crash
                    if done + failed - last_saved >= 25:
                        last_saved = done + failed
                        _save_counters()

                    finished, _ = _cf.wait(list(inflight), return_when=_cf.FIRST_COMPLETED)
                    for fut in finished:
                        patient = inflight.pop(fut)
                        uid  = patient.get("uid", "")
                        name = _name_of(patient)
                        try:
                            write_result = fut.result() or {}
                            # Targets report failure as an errors list and/or a failed
                            # count (VistaSoft returns failed=1 with an empty list)
                            if write_result.get("errors") or write_result.get("failed"):
                                detail = write_result.get("errors") or "see log"
                                if isinstance(detail, list) and len(detail) == 1:
                                    detail = detail[0]
                                raise RuntimeError(f"Write failed for {uid[:8]}… ({detail})")

                            self.store.set_patient_status(session_id, uid, "completed")

                            pu = write_result.get("media_uploaded", write_result.get("written", 0))
                            pm = write_result.get("media_missing",  write_result.get("skipped", 0))

                            # Only a patient with everything migrated is marked done.
                            # Images held back by the Filters page keep the patient
                            # eligible, so a later run without that filter adds them.
                            if pm == 0 and not patient.get("filtered_media"):
                                self.store.mark_migrated(source.name, target.name, uid, session_id)

                            media_uploaded += pu
                            media_missing  += pm
                            done += 1

                            pct = 50 + int((done / max(total, 1)) * 46)
                            note = f" — {pm} image(s) not migrated" if pm else ""
                            _progress(pct, 100, f"Imported: {name} ({done}/{total}){note}", _counters({
                                "type": "patient_done", "patient": name, "uid": uid,
                                "images": pu, "media_missing": pm,
                            }))

                        except Exception as exc:
                            failed += 1
                            err_str = str(exc)
                            errors.append(f"{name}: {err_str}")
                            self.store.set_patient_status(session_id, uid, "failed", err_str)
                            self.logger.error(f"Failed to migrate {name}: {exc}")
                            pct = 50 + int(((done + failed) / max(total, 1)) * 46)
                            _progress(pct, 100, f"Failed: {name} — {exc}", _counters({
                                "type": "patient_failed", "patient": name, "uid": uid,
                                "error": err_str,
                            }))

            if self._cancelled:
                self.store.update_session_status(
                    session_id, "cancelled",
                    message=f"Cancelled after {done}/{total} patients.",
                    done_patients=done, failed_patients=failed,
                    media_uploaded=media_uploaded, media_missing=media_missing,
                )
                result.status  = MigrationStatus.CANCELLED
                result.message = "Migration was cancelled."
                result.patients_failed = failed
                if on_complete:
                    on_complete(result)
                return

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
                f"{media_uploaded} image(s) uploaded, {media_missing} could not be migrated."
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
