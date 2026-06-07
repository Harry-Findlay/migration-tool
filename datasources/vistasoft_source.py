"""
VistaSoft — SOURCE datasource.
Reads patients and imaging data from VistaSoft (Firebird DB + disk files).
"""
import os
import sys
import concurrent.futures
import logging
from typing import Callable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from core.base_datasource import BaseDatasource
from core.models import ConfigurationItem, ConfigurationType

COMMON_ENCODINGS = {
    "utf-8":   "UTF-8, Codepage:65001",
    "utf-16":  "UTF-16, Codepage:1200",
    "latin-1": "Latin-1 / ISO-8859-1, Codepage:28591",
    "cp1252":  "Windows-1252, Codepage:1252",
    "ascii":   "ASCII, Codepage:20127",
}


# ImageClass → DTX Studio modality enum
# From VistaSoft C# ToMediaType() - maps ImageClass to content type
_CLASS_TO_CONTENT_TYPE = {
    "Pano":     "image/pano",
    "Ceph":     "image/ceph",
    "Intra":    "image/intraoral_xray",
    "Video":    "unsupported/Video",     # VistaSoft doesn't support video export
    "Snapshot": "image/screen_capture",
    "Dvt":      "volume/multi_frame",
    "Proof":    "image/scout",
    "Xray":     "image/intraoral_xray",
}

_CLASS_TO_DTX_MODALITY = {
    "Pano":     "PANORAMIC",
    "Ceph":     "CEPHALOGRAM",
    "Intra":    "INTRAORAL",
    "Xray":     "INTRAORAL",
    "Dvt":      "VOLUME",
    "Video":    "PICTURE",  # DTX has no MOVIE bucket — videos display as PICTURE
    "Snapshot": "PICTURE",
    "Proof":    "PICTURE",
}


def _partial_reverse_uuid(uid: str) -> str:
    parts = uid.lower().strip().split("-")
    def rev(h: str) -> str:
        b = bytes.fromhex(h)
        return b[::-1].hex()
    return f"{rev(parts[0])}-{rev(parts[1])}-{rev(parts[2])}-{parts[3]}-{parts[4]}"


def _build_folder_map(parent: str) -> dict:
    result = {}
    try:
        for name in os.listdir(parent):
            full = os.path.join(parent, name)
            if os.path.isdir(full):
                result[name.lower()] = full
    except OSError:
        pass
    return result


def _build_file_map(folder: str) -> dict:
    """Map lowercase stem -> full path, skipping sidecar/preview files."""
    _SKIP = {".json", ".jpg", ".jpeg", ".spv"}
    result = {}
    try:
        for name in os.listdir(folder):
            if os.path.splitext(name)[1].lower() in _SKIP:
                continue
            stem = os.path.splitext(name)[0].lower()
            result[stem] = os.path.join(folder, name)
    except OSError:
        pass
    return result


def _read_sidecar(dcm_path: str) -> dict:
    import json
    if not dcm_path:
        return {}
    json_path = os.path.splitext(dcm_path)[0] + ".json"
    if not os.path.isfile(json_path):
        return {}
    try:
        with open(json_path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


class VistaSoftSource(BaseDatasource):

    @property
    def name(self) -> str:
        return "vistasoft"

    @property
    def display_name(self) -> str:
        return "VistaSoft"

    @property
    def role(self) -> str:
        return "source"

    def _setup_configuration(self):
        self.configuration = [
            ConfigurationItem(
                key="MediaPath",
                name="VistaSoftData Folder",
                description="Location of the VistaSoftData folder",
                value="C:\\VistaSoftData",
                placeholder="C:\\VistaSoftData",
                advanced=False,
                config_type=ConfigurationType.PATH,
                group="Connection",
            ),
            ConfigurationItem(
                key="ConnectionString",
                name="Connection String",
                description="Auto-built from MediaPath. Override only if needed.",
                value="",
                advanced=True,
                hidden=False,
                placeholder="user=SYSDBA;password=masterkey;database=localhost:C:\\VistaSoftData\\P1\\Institution.fdb;",
                config_type=ConfigurationType.CONNECTION_STRING,
                group="Connection",
            ),
            ConfigurationItem(
                key="Encoding",
                name="Text Encoding",
                value="utf-8",
                advanced=True,
                options=COMMON_ENCODINGS,
                config_type=ConfigurationType.SELECT,
                group="Advanced",
            ),
            ConfigurationItem(
                key="SkipEmptyPatients",
                name="Skip Patients Without Media",
                value="On",
                advanced=True,
                config_type=ConfigurationType.SWITCH,
                group="Advanced",
            ),
            ConfigurationItem(
                key="NameOrder",
                name="Name Order",
                description="How to map patient names to given/family name fields",
                value="given_family",
                advanced=False,
                options={
                    "given_family": "Given name first (John Smith)",
                    "family_given": "Family name first (Smith John)",
                },
                config_type=ConfigurationType.SELECT,
                group="Advanced",
            ),
        ]

    def _get_db_path(self) -> str:
        return os.path.normpath(
            os.path.join(self.get_config_value("MediaPath") or "", "P1", "Institution.fdb"))

    def _get_images_path(self) -> str:
        return os.path.normpath(
            os.path.join(self.get_config_value("MediaPath") or "", "P1", "Images"))

    def validate(self) -> tuple[bool, str]:
        media_path = self.get_config_value("MediaPath")
        if not media_path:
            return False, "VistaSoftData folder is not set."
        if not os.path.isdir(media_path):
            return False, f"Folder not found: {media_path}"
        if not os.path.isfile(self._get_db_path()):
            return False, f"Database not found at: {self._get_db_path()}"
        if not os.path.isdir(self._get_images_path()):
            return False, f"Images folder not found at: {self._get_images_path()}"
        return True, "VistaSoft configuration is valid."

    def test_connection(self) -> tuple[bool, str]:
        return self.test_db_connection()

    def test_db_connection(self) -> tuple[bool, str]:
        try:
            from datasources.fb_client import _run, _get_db_copy
            temp_db = _get_db_copy(self._get_db_path())
            try:
                data = _run("test", temp_db)
                return True, f"Connected. Found {data['count']} visible patient(s)."
            finally:
                try:
                    os.remove(temp_db)
                except Exception:
                    pass
        except Exception as exc:
            from datasources.fb_client import run_diag
            return False, f"{exc}\n\n--- FbBridge diagnostics ---\n{run_diag()}"

    def _connect(self):
        from datasources.fb_client import connect_path, _get_db_copy
        temp_db = _get_db_copy(self._get_db_path())
        return connect_path(temp_db)

    def load(self, cancel_flag, max_parallelism: int = 4,
             progress_callback: Optional[Callable] = None) -> list:
        images_path = os.path.normpath(self._get_images_path())
        # Always load all patients — filtering by media presence is done at
        # migration time via Migration Filters, not at load time.
        skip_empty  = False

        from datasources.fb_client import connect_path, _get_db_copy
        self.logger.debug("Copying Institution.fdb to temp location...")
        try:
            temp_db = _get_db_copy(self._get_db_path())
        except Exception as exc:
            raise ConnectionError(str(exc)) from exc

        def _open():
            return connect_path(temp_db)

        self.logger.debug("Fetching patient UIDs from Firebird")
        try:
            with _open() as conn:
                cur = conn.cursor()
                cur.execute("SELECT UID FROM PATIENT WHERE ISHIDDEN=0")
                patient_ids = [str(row[0]) for row in cur.fetchall()]
        except Exception as exc:
            raise ConnectionError(str(exc)) from exc

        total = len(patient_ids)
        self.logger.debug(f"Total patients: {total}")
        if progress_callback:
            progress_callback(0, total, f"Loading media for {total} patients…")

        patient_folder_map = _build_folder_map(images_path)
        patients  = []
        completed = [0]
        errors    = [0]
        lock      = __import__("threading").Lock()

        def process_patient(patient_id: str):
            if cancel_flag():
                return None
            try:
                uid_hex = patient_id.replace("-", "")
                with _open() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        f"SELECT UID, ID, BIRTHDATE, GIVENNAMES, FAMILYNAME, SEX "
                        f"FROM PATIENT WHERE UID = X'{uid_hex}'"
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(f"Patient not found: {patient_id}")

                    _raw_given  = str(row[3]) if row[3] else ""
                    _raw_family = str(row[4]) if row[4] else ""
                    if self.get_config_value("NameOrder") == "family_given":
                        _given, _family = _raw_family, _raw_given
                    else:
                        _given, _family = _raw_given, _raw_family
                    patient = {
                        "uid":         str(row[0]) if row[0] else patient_id,
                        "id":          str(row[1]) if row[1] else "",
                        "birth_date":  str(row[2]) if row[2] else "",
                        "given_names": _given,
                        "family_name": _family,
                        "sex":         str(row[5]) if row[5] else "",
                        "studies":     {},
                    }

                    # Resolve disk folder
                    disk_patient_uid = _partial_reverse_uuid(patient_id)
                    patient_folder   = patient_folder_map.get(disk_patient_uid.lower())
                    file_map = _build_file_map(patient_folder) if patient_folder else {}

                    img_cur = conn.cursor()
                    img_cur._images_dir = images_path
                    img_cur.execute(
                        f"SELECT IMAGE.UID, IMAGE.STUDYUID, IMAGE.SOPINSTANCEUID, "
                        f"IMAGE.IMAGECLASS, IMAGE.COMMENTS, IMAGE.ACQUISITIONDATETIME, "
                        f"STUDY.STUDYINSTANCEUID, STUDY.STUDYDATETIME, STUDY.ACCESSIONNUMBER, "
                        f"STUDY.STUDYDESCRIPTION, STUDY.REFERRINGPHYSICIANSNAME "
                        f"FROM IMAGE LEFT JOIN STUDY ON IMAGE.STUDYUID = STUDY.UID "
                        f"WHERE IMAGE.ISHIDDEN=0 AND STUDY.PATIENTUID = X'{uid_hex}'"
                    )
                    file_paths = getattr(img_cur, "_file_paths", [])
                    for idx, img_row in enumerate(img_cur.fetchall()):
                        if cancel_flag():
                            break
                        try:
                            image_uid   = str(img_row[0]) if img_row[0] else ""
                            study_uid   = str(img_row[1]) if img_row[1] else ""
                            image_class = str(img_row[3]) if img_row[3] else "DEFAULT"
                            if image_class == "Video":
                                self.logger.debug(f"  Skipping Video image — not supported for export")
                                continue  # Video not supported by VistaSoft export

                            # Resolve file path via disk mapping
                            disk_image_uid = _partial_reverse_uuid(image_uid)
                            resolved_path  = file_map.get(disk_image_uid.lower())
                            if resolved_path is None:
                                fb_path = (file_paths[idx] if idx < len(file_paths)
                                           else os.path.join(images_path, study_uid, image_uid))
                                resolved_path = os.path.normpath(fb_path)

                            sidecar = _read_sidecar(resolved_path)

                            study = patient["studies"].setdefault(study_uid, {
                                "uid":            study_uid,
                                "study_instance": str(img_row[6]) if img_row[6] else "",
                                "study_datetime": str(img_row[7]) if img_row[7] else "",
                                "accession":      str(img_row[8]) if img_row[8] else "",
                                "description":    str(img_row[9]) if img_row[9] else "",
                                "ref_physician":  str(img_row[10]) if img_row[10] else "",
                                "series":         {},
                            })
                            series = study["series"].setdefault(image_class, {
                                "series_key": image_class,
                                "media":      [],
                            })
                            # Preview: .jpg alongside the .dcm
                            _preview = None
                            if resolved_path:
                                for _ext in (".jpg", ".JPG"):
                                    _p = os.path.splitext(resolved_path)[0] + _ext
                                    if os.path.isfile(_p):
                                        _preview = _p
                                        break
                            series["media"].append({
                                "uid":          image_uid,
                                "sop_instance": str(img_row[2]) if img_row[2] else "",
                                "image_class":  image_class,
                                "modality":     _CLASS_TO_DTX_MODALITY.get(image_class, "PICTURE"),
                                "content_type": _CLASS_TO_CONTENT_TYPE.get(image_class, "application/dicom"),
                                "acq_datetime": str(img_row[5]) if img_row[5] else "",
                                "comments":     str(img_row[4]) if img_row[4] else "",
                                "file_path":    resolved_path,
                                "content_type": "application/dicom",
                                "preview_path": _preview,
                                "sidecar":      sidecar,
                            })
                        except Exception as exc:
                            errors[0] += 1
                            self.logger.error(f"Failed to load image for {patient_id}: {exc}")

                has_media = any(
                    s["media"]
                    for st in patient["studies"].values()
                    for s in st["series"].values()
                )
                if has_media or not skip_empty:
                    return patient
                self.logger.info(f"Skipping patient {patient_id} — no media")
                return None

            except Exception as exc:
                errors[0] += 1
                self.logger.error(f"Failed to load patient {patient_id}: {exc}")
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallelism) as pool:
            futures = {pool.submit(process_patient, pid): pid for pid in patient_ids}
            for future in concurrent.futures.as_completed(futures):
                if cancel_flag():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                result = future.result()
                with lock:
                    completed[0] += 1
                    if result:
                        patients.append(result)
                        name = f"{result['given_names']} {result['family_name']}".strip()
                        if progress_callback:
                            progress_callback(completed[0], total, f"Loaded: {name}")
                    else:
                        if progress_callback:
                            progress_callback(completed[0], total,
                                              f"Skipped ({completed[0]}/{total})")

        self.logger.info(
            f"VistaSoft load complete — {len(patients)} patients, {errors[0]} errors")

        try:
            if os.path.isfile(temp_db):
                os.remove(temp_db)
        except Exception:
            pass

        return patients

    def read_patients(self, progress_callback=None):
        cancelled = [False]
        return self.load(cancel_flag=lambda: cancelled[0],
                         progress_callback=progress_callback)