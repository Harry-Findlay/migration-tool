"""
SOPRO — SOURCE datasource.
Reads patients and imaging data from SOPRO dental imaging software.

SOPRO stores data in a folder hierarchy:
  {MediaPath}/{group}/{subgroup}/{patientFolder}/
    patient.dat  or  patient.dax  — patient demographics (INI format)
    image.dat                     — image index (INI format)
    *.bmp / *.jpg / *.dcm         — image files

Patient folders are named with a numeric ID like "0001-0001".
"""
import os
import sys
import re
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

# SOPRO image type codes → image class (VistaSoft style) and DTX modality
# From C# SOPROExtensions.ToMediaType:
#   1  => image/intraoral_xray
#   2  => image/intraoral_camera
#   4  => image/intraoral_xray
#   8  => other/movie
#   16 => image/clinical_picture
#   64 => image/intraoral_xray
#   128 => image/intraoral_camera
_SOPRO_TYPE_MAP = {
    "1":   ("Intra",    "INTRAORAL"),
    "4":   ("Intra",    "INTRAORAL"),
    "64":  ("Intra",    "INTRAORAL"),
    "2":   ("Snapshot", "PICTURE"),    # intraoral camera
    "128": ("Snapshot", "PICTURE"),    # intraoral camera
    "8":   ("Video",    "MOVIE"),
    "16":  ("Snapshot", "PICTURE"),    # clinical picture
}

_FOLDER_PATTERN = re.compile(r'^\d+-\d+$')


def _read_ini(path: str, encoding: str = "utf-16") -> dict:
    """Read a simple INI file into a dict of {section: {key: value}}."""
    result = {}
    section = ""
    try:
        with open(path, encoding=encoding, errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip()
                    result.setdefault(section, {})
                elif "=" in line and section:
                    k, _, v = line.partition("=")
                    result[section][k.strip()] = v.strip()
    except Exception:
        pass
    return result


def _decode21(text: str) -> str:
    """Decode SOPRO base-21 encoded string."""
    if not text or len(text) % 4 != 0:
        return text
    chars = []
    prev = 0
    for i in range(0, len(text), 4):
        chunk = text[i:i+2].upper()
        val = 0
        for ch in reversed(chunk):
            idx = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ".find(ch)
            if idx == -1:
                return text
            val = val * 21 + idx
        decoded = prev ^ val
        prev = val
        chars.append(chr(decoded))
    return "".join(reversed(chars))


def _parse_date(s: str) -> str:
    """Parse SOPRO date string (yyyy-MM-dd) to ISO date."""
    if not s:
        return ""
    s = s.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s
    return s


def _get_patient_from_dat(folder: str, encoded: bool) -> dict:
    """Read patient demographics from patient.dat or patient.dax."""
    fname = "patient.dax" if encoded else "patient.dat"
    path = os.path.join(folder, fname)
    if not os.path.isfile(path):
        return {}
    ini = _read_ini(path)
    section = ini.get("PATIENT", {})

    def _dec(v):
        v = v or ""
        return _decode21(v) if encoded else v

    lastname = _dec(section.get("Nom", ""))
    if "SOPRO.INI" in lastname:
        lastname = ""
    firstname = _dec(section.get("Prenom", ""))
    dob = _parse_date(_dec(section.get("DateNaissance", "")))
    return {
        "family_name": lastname,
        "given_names": firstname,
        "birth_date":  dob,
    }


def _get_patient_from_csv_line(line: str) -> dict:
    """Parse a CSV line from pat*.idx."""
    parts = line.strip().split(";")
    if len(parts) < 3:
        return {}
    src_id    = parts[0].strip() if len(parts) > 0 else ""
    lastname  = parts[1].strip() if len(parts) > 1 else ""
    if "SOPRO.INI" in lastname:
        lastname = ""
    firstname = parts[2].strip() if len(parts) > 2 else ""
    dob       = parts[4].strip() if len(parts) > 4 else ""
    return {
        "id":          src_id,
        "family_name": lastname,
        "given_names": firstname,
        "birth_date":  _parse_date(dob),
    }


def _load_patient_index(media_path: str) -> dict:
    """
    Load patient demographics from pat*.idx and pat*.dax index files.
    Searches media_path and one level of subdirectories so we find index
    files regardless of where SOPRO placed them.
    Returns dict of {src_id: {demographics}}.
    """
    patients = {}

    # Collect all candidate directories to search (root + immediate subdirs)
    search_dirs = [media_path]
    try:
        for entry in os.scandir(media_path):
            if entry.is_dir():
                search_dirs.append(entry.path)
    except Exception:
        pass

    for search_dir in search_dirs:
        try:
            dir_entries = os.listdir(search_dir)
        except Exception:
            continue

        # .idx files (plain CSV)
        for fname in dir_entries:
            if re.match(r'pat.*\.idx$', fname, re.IGNORECASE):
                try:
                    with open(os.path.join(search_dir, fname),
                              encoding="utf-16", errors="replace") as f:
                        f.readline()  # skip header
                        for line in f:
                            p = _get_patient_from_csv_line(line)
                            if p.get("id"):
                                src_id = p["id"]
                                if src_id in patients:
                                    patients[src_id].update({k: v for k, v in p.items() if v})
                                else:
                                    patients[src_id] = p
                except Exception:
                    pass

        # .dax files (encoded CSV)
        for fname in dir_entries:
            if re.match(r'pat.*\.dax$', fname, re.IGNORECASE):
                try:
                    with open(os.path.join(search_dir, fname),
                              encoding="utf-16", errors="replace") as f:
                        f.readline()  # skip header
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                decoded = _decode21(line)
                                p = _get_patient_from_csv_line(decoded)
                            except Exception:
                                continue
                            if p.get("id"):
                                src_id = p["id"]
                                if src_id not in patients:
                                    patients[src_id] = p
                except Exception:
                    pass

    return patients


def _find_patient_folders(media_path: str) -> list:
    """
    Find all SOPRO patient folders by recursively walking media_path up to 4
    levels deep and collecting any folder that contains patient.dat, patient.dax,
    or image.dat.  This handles all known SOPRO folder layouts:

        Layout A (3-level):  media_path/d+-d+/d+-d+/patientFolder/
        Layout B (2-level):  media_path/d+-d+/patientFolder/
        Layout C (1-level):  media_path/patientFolder/

    A folder qualifies if it directly contains at least one of:
        patient.dat, patient.dax, image.dat
    """
    folders = []
    _PATIENT_FILES = {"patient.dat", "patient.dax", "image.dat"}

    def _walk(path: str, depth: int):
        if depth > 4:
            return
        try:
            entries = list(os.scandir(path))
        except Exception:
            return

        file_names = {e.name.lower() for e in entries if e.is_file()}

        # If this folder directly contains SOPRO patient files it IS a patient folder
        if _PATIENT_FILES & file_names:
            folders.append(path)
            return  # don't recurse into patient folders

        # Otherwise descend into subdirectories
        for e in entries:
            if e.is_dir():
                _walk(e.path, depth + 1)

    _walk(media_path, 0)

    # Deduplicate and log what we found
    unique = list(dict.fromkeys(folders))
    import logging
    logging.getLogger("SOPROSource").debug(
        f"_find_patient_folders: {len(unique)} folder(s) found under {media_path}"
    )
    return unique


def _load_images_from_folder(folder: str) -> list:
    """Read image.dat and return list of media dicts."""
    media = []
    image_dat = os.path.join(folder, "image.dat")
    if not os.path.isfile(image_dat):
        return media
    ini = _read_ini(image_dat)
    for section_name, file_data in ini.items():
        if not section_name:
            continue
        file_path = os.path.join(folder, section_name)
        if not os.path.isfile(file_path):
            continue
        stype              = file_data.get("Type", "")
        iclass, dtx_mod    = _SOPRO_TYPE_MAP.get(stype, ("Snapshot", "PICTURE"))
        date_str  = file_data.get("Date", "")
        time_str  = file_data.get("Heure", "")
        acq_dt    = ""
        if date_str:
            try:
                from datetime import datetime
                dt = datetime.strptime(date_str, "%Y%m%d")
                if time_str:
                    try:
                        t = datetime.strptime(time_str, "%H%M%S")
                        dt = dt.replace(hour=t.hour, minute=t.minute, second=t.second)
                    except Exception:
                        pass
                acq_dt = dt.strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                acq_dt = date_str
        import mimetypes as _mt
        import uuid as _uuid
        ct = _mt.guess_type(file_path)[0] or "application/octet-stream"
        media.append({
            "uid":          section_name,
            "image_class":  iclass,
            "modality":     dtx_mod,
            "file_path":    file_path,
            "content_type": ct,
            "preview_path": file_path if ct.startswith("image/") else None,
            "acq_datetime": acq_dt,
            "sop_instance": str(_uuid.uuid4()),  # unique per image for DTX dedup
            "comments":     "",
        })
    return media


class SOPROSource(BaseDatasource):

    @property
    def name(self) -> str:
        return "sopro"

    @property
    def display_name(self) -> str:
        return "SOPRO"

    @property
    def role(self) -> str:
        return "source"

    def _setup_configuration(self):
        self.configuration = [
            ConfigurationItem(
                key="MediaPath",
                name="SOPRO Images Folder",
                description="Location of the SOPRO images folder",
                value="",
                placeholder="C:\\SOPRO\\Images",
                advanced=False,
                config_type=ConfigurationType.PATH,
                group="Connection",
            ),
            ConfigurationItem(
                key="Encoding",
                name="Text Encoding",
                value="utf-16",
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

    def validate(self) -> tuple[bool, str]:
        media_path = self.get_config_value("MediaPath")
        if not media_path:
            return False, "SOPRO Images folder is not set."
        if not os.path.isdir(media_path):
            return False, f"Folder not found: {media_path}"
        return True, "SOPRO configuration is valid."

    def test_connection(self) -> tuple[bool, str]:
        ok, msg = self.validate()
        if not ok:
            return False, msg
        media_path = self.get_config_value("MediaPath")
        folders = _find_patient_folders(media_path)
        return True, f"Found {len(folders)} patient folder(s)."

    def load(self, cancel_flag, max_parallelism: int = 4,
             progress_callback: Optional[Callable] = None) -> list:

        media_path  = self.get_config_value("MediaPath") or ""
        # skip_empty is set to False below — see comment there

        self.logger.info(f"SOPRO load starting — MediaPath={media_path!r}")

        if not media_path:
            self.logger.error("MediaPath is empty — config was not applied correctly")
            return []
        if not os.path.isdir(media_path):
            self.logger.error(f"MediaPath does not exist: {media_path!r}")
            return []

        # Always load ALL patients at this stage regardless of SkipEmptyPatients.
        # Filtering patients with no media is done at migration time via Migration
        # Filters, so the user can see everything that exists in the source first.
        skip_empty = False

        # Load patient index
        patient_index = _load_patient_index(media_path)
        self.logger.info(f"Patient index: {len(patient_index)} entries")

        # Find all patient folders
        folders = _find_patient_folders(media_path)
        total   = len(folders)
        self.logger.info(f"Found {total} patient folder(s) under {media_path!r}")
        if total == 0:
            try:
                items = os.listdir(media_path)[:20]
                self.logger.info(f"Contents of MediaPath: {items}")
            except Exception as e:
                self.logger.warning(f"Could not list MediaPath: {e}")
        if progress_callback:
            progress_callback(0, total, f"Loading {total} patient folders…")

        patients  = []
        completed = [0]
        errors    = [0]
        lock      = __import__("threading").Lock()

        def process_folder(folder: str):
            if cancel_flag():
                return None
            src_id = os.path.basename(folder)
            try:
                # Demographics — start from index, then override with local files
                demo = dict(patient_index.get(src_id, {}))

                # Try patient.dat (plain)
                dat = _get_patient_from_dat(folder, encoded=False)
                if dat:
                    demo.update({k: v for k, v in dat.items() if v})

                # Try patient.dax (encoded)
                dax = _get_patient_from_dat(folder, encoded=True)
                if dax:
                    demo.update({k: v for k, v in dax.items() if v})

                _raw_given  = demo.get("given_names", "")
                _raw_family = demo.get("family_name", "")
                if self.get_config_value("NameOrder") == "family_given":
                    _given, _family = _raw_family, _raw_given
                else:
                    _given, _family = _raw_given, _raw_family
                patient = {
                    "uid":         src_id,
                    "id":          demo.get("id", src_id),
                    "given_names": _given,
                    "family_name": _family,
                    "birth_date":  demo.get("birth_date", ""),
                    "sex":         "",
                    "studies":     {},
                }

                # Load images
                media_list = _load_images_from_folder(folder)
                # Each SOPRO image gets its own study+series for correct display in target
                for m in media_list:
                    study_uid = f"{src_id}-{m['uid']}"
                    patient["studies"][study_uid] = {
                        "uid":            study_uid,
                        "study_instance": "",
                        "study_datetime": m.get("acq_datetime", ""),
                        "accession":      "",
                        "description":    "",
                        "ref_physician":  "",
                        "series": {
                            m["image_class"]: {
                                "series_key": m["image_class"],
                                "media":      [m],
                            }
                        },
                    }

                has_media = bool(media_list)
                if has_media or not skip_empty:
                    return patient
                return None

            except Exception as exc:
                errors[0] += 1
                self.logger.error(f"Failed to load folder {folder}: {exc}")
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallelism) as pool:
            futures = {pool.submit(process_folder, f): f for f in folders}
            for future in concurrent.futures.as_completed(futures):
                if cancel_flag():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                result = future.result()
                with lock:
                    completed[0] += 1
                    if result:
                        patients.append(result)
                        name = f"{result['given_names']} {result['family_name']}".strip() or result['id']
                        if progress_callback:
                            progress_callback(completed[0], total, f"Loaded: {name}")
                    else:
                        if progress_callback:
                            progress_callback(completed[0], total,
                                              f"Skipped ({completed[0]}/{total})")

        self.logger.info(
            f"SOPRO load complete — {len(patients)} patients, {errors[0]} errors")
        return patients

    def read_patients(self, progress_callback=None):
        cancelled = [False]
        return self.load(cancel_flag=lambda: cancelled[0],
                         progress_callback=progress_callback)