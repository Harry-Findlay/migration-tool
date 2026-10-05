"""
datasources/csimaging_source.py
===============================
Source connector for Carestream / CS Imaging (and legacy Trophy / Kodak RVG
folder structures).

Ported from the C# CSImagingDatasource reference. Two modes:

  mssql   — CS Imaging v8.3+. Patients + patient folder paths come from the
            CSIS SQL Server database (PATIENT, PATIENT_DIR, IMAGE_REPO).
            PATIENT_DIR.path contains a %ImageRepo% token that is replaced
            with the IMAGE_REPO.path for the patient's image_repo_id
            (overridable per repo for when the migration machine sees the
            share under a different path).

  folder  — No database. Patient folders are found by walking MediaPath up
            to SearchDepth levels and matching the path (relative to
            MediaPath) against PatientFolderTemplate. Demographics come from
            the DICOM headers of the files in each folder.

Per patient folder (both modes):
  - Top-level files: DICOM (by magic, any extension) and JPEG/PNG/BMP/TIFF
  - VOL_* / TASK_* subfolders: CBCT. The first DICOM file is read; a
    multi-frame file becomes one VOLUME media item. Single-slice series
    are NOT yet supported by the targets and are logged + counted.
  - 3DIO_* / DIO_* subfolders: CS ScanFlow intraoral scans — not yet
    supported by the targets; logged + counted.

Output follows the standard media contract — nothing CS-specific leaks
into targets: image_class (Intra/Pano/Ceph/Dvt/Snapshot), modality
(INTRAORAL/PANORAMIC/CEPHALOGRAM/VOLUME/PICTURE), acq_datetime,
content_type, preview_path, plus dicom_tags passthrough.

MSSQL access: pyodbc with the best installed SQL Server ODBC driver
(falls back to the built-in "SQL Server" driver that ships with Windows).
An ADO.NET-style connection string (as CS Imaging / the old tool used) is
converted automatically; a string containing "Driver=" is passed through.
"""

import mimetypes
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional

from core.base_datasource import BaseDatasource
from core.models import ConfigurationItem, ConfigurationType
from datasources.dicom_header import read_dicom_header

COMMON_ENCODINGS = {
    "utf-8":         "UTF-8",
    "utf-16":        "UTF-16",
    "windows-1252":  "Windows-1252 (Western Europe)",
    "iso-8859-1":    "ISO-8859-1 (Latin-1)",
    "iso-8859-15":   "ISO-8859-15 (Latin-9)",
    "cp850":         "CP850 (DOS Western Europe)",
    "cp1250":        "CP1250 (Windows Central Europe)",
    "cp1251":        "CP1251 (Windows Cyrillic)",
}

_DEFAULT_CONN = ("Server=localhost\\CSISSERVER;Database=CSIS;"
                 "Trusted_Connection=True;TrustServerCertificate=True;")
_DEFAULT_TEMPLATE = r"^[0-9a-f-]+$"

_VOLUME_PREFIXES = ("VOL_", "TASK_")
_SCAN_PREFIXES   = ("3DIO_", "DIO_")      # TASK_ is tried as volume first

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}

# SOP Class UIDs
_SOP_IO_PRES   = "1.2.840.10008.5.1.4.1.1.1.3"
_SOP_IO_PROC   = "1.2.840.10008.5.1.4.1.1.1.3.1"
_SOP_CT        = "1.2.840.10008.5.1.4.1.1.2"
_SOP_ENH_CT    = "1.2.840.10008.5.1.4.1.1.2.1"
_SOP_VL_PHOTO  = "1.2.840.10008.5.1.4.1.1.77.1.4"
_SOP_SC        = "1.2.840.10008.5.1.4.1.1.7"

_CLASS = {
    "Intra":    ("Intra",    "INTRAORAL"),
    "Pano":     ("Pano",     "PANORAMIC"),
    "Ceph":     ("Ceph",     "CEPHALOGRAM"),
    "Dvt":      ("Dvt",      "VOLUME"),
    "Snapshot": ("Snapshot", "PICTURE"),
}

_ODBC_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_on(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("on", "true", "1", "yes")


def _da_to_iso(da: str) -> str:
    da = (da or "").strip().replace("-", "").replace(".", "")
    if len(da) >= 8 and da[:8].isdigit():
        return f"{da[:4]}-{da[4:6]}-{da[6:8]}"
    return ""


def _dicom_dt(tags: dict) -> str:
    """Acquisition → Content → Series → Study date/time, as ISO local time."""
    for d_key, t_key in (("AcquisitionDate", "AcquisitionTime"),
                         ("ContentDate",     "ContentTime"),
                         ("SeriesDate",      "SeriesTime"),
                         ("StudyDate",       "StudyTime")):
        d = _da_to_iso(tags.get(d_key, ""))
        if not d:
            continue
        t = (tags.get(t_key, "") or "").replace(":", "").split(".")[0]
        t = (t + "000000")[:6] if t.isdigit() else "000000"
        return f"{d}T{t[:2]}:{t[2:4]}:{t[4:6]}"
    return ""


def _parse_person_name(pn: str) -> tuple[str, str, str]:
    """DICOM PN → (family, given, middle). Uses the alphabetic group only."""
    pn = (pn or "").split("=")[0].strip()
    if not pn:
        return "", "", ""
    if "^" not in pn:
        return pn, "", ""
    parts = (pn.split("^") + ["", "", ""])[:3]
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def _parse_sex(s) -> str:
    c = str(s or "").strip().upper()[:1]
    return {"M": "MALE", "F": "FEMALE", "O": "OTHER"}.get(c, "")


def _birth_date(v) -> str:
    if v is None or v == "":
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip().split("T")[0].split(" ")[0]
    if len(s) == 10 and s[4] == "-":
        return s
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    for fmt in ("%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _classify_dicom(tags: dict) -> tuple[str, str, str]:
    """
    Return (image_class, modality, reason). Reason is logged at debug so
    classification can be checked against real CS data.

    Order of trust:
      1. SOP Class UID — intraoral / CT / photo classes are unambiguous
      2. Modality tag — IO, PX, CT, XC/ES/VL/OP are unambiguous
      3. DX / CR / OT — CS uses these for both pano and ceph, so look at
         descriptive text, then fall back to image geometry
    """
    sop = tags.get("SOPClassUID", "")
    mod = (tags.get("Modality", "") or "").upper()

    if sop in (_SOP_IO_PRES, _SOP_IO_PROC):
        return (*_CLASS["Intra"], "sop:intraoral")
    if sop in (_SOP_CT, _SOP_ENH_CT):
        return (*_CLASS["Dvt"], "sop:ct")
    if sop == _SOP_VL_PHOTO:
        return (*_CLASS["Snapshot"], "sop:vl-photo")

    if mod == "IO":
        return (*_CLASS["Intra"], "modality:IO")
    if mod == "PX":
        return (*_CLASS["Pano"], "modality:PX")
    if mod == "CT":
        return (*_CLASS["Dvt"], "modality:CT")
    if mod in ("XC", "ES", "VL", "OP", "GM", "SM"):
        return (*_CLASS["Snapshot"], f"modality:{mod}")

    text = " ".join(tags.get(k, "") for k in (
        "ImageType", "SeriesDescription", "StudyDescription",
        "ProtocolName", "BodyPartExamined", "ViewPosition",
    )).upper()
    if "CEPH" in text or re.search(r"\b(LAT|LATERAL|PA|AP|SMV|CARPUS|HAND)\b", text):
        return (*_CLASS["Ceph"], "text:ceph")
    if "PANO" in text or "OPG" in text or "PANORAMIC" in text:
        return (*_CLASS["Pano"], "text:pano")
    if re.search(r"\b(BITEWING|BW|PERIAPICAL|PA\d|INTRA|SENSOR|RVG)\b", text):
        return (*_CLASS["Intra"], "text:intra")

    # Geometry fallback for DX/CR/OT with no descriptive text
    try:
        rows, cols = int(tags.get("Rows", 0)), int(tags.get("Columns", 0))
    except ValueError:
        rows = cols = 0
    if rows and cols:
        if cols / rows >= 1.6:
            return (*_CLASS["Pano"], f"geometry:{cols}x{rows}")
        if max(rows, cols) <= 2200:
            return (*_CLASS["Intra"], f"geometry:{cols}x{rows}")
        return (*_CLASS["Ceph"], f"geometry:{cols}x{rows}")

    if mod == "SC" or sop == _SOP_SC:
        return (*_CLASS["Snapshot"], "secondary-capture")
    return (*_CLASS["Intra"], f"default ({mod or 'no modality'})")


def _version_key(name: str) -> tuple:
    """'.version_4.4' → (4, 4) for sorting; non-numeric parts sort lowest."""
    out = []
    for part in name.split("_", 1)[-1].split("."):
        out.append(int(part) if part.isdigit() else -1)
    return tuple(out)


def _csi_thumbnail(dicom_path: str) -> Optional[str]:
    """
    CS Imaging stores a thumbnail per image at
        <folder>\\.csi_data\\.version_<n>\\<idx>@<dicom file name>\\t.png
    e.g.  .csi_data\\.version_4.4\\0@IT_Infinity_29052026_124111.dcm\\t.png
    The "<idx>@" prefix is matched with the lowest index preferred; an
    unprefixed folder name is also accepted. Newest .version_ wins.
    Returns the t.png path or None.
    """
    folder, name = os.path.split(dicom_path)
    csi = os.path.join(folder, ".csi_data")
    try:
        versions = [e.path for e in os.scandir(csi)
                    if e.is_dir() and e.name.lower().startswith(".version_")]
    except OSError:
        return None

    lname = name.lower()
    for vdir in sorted(versions, key=lambda p: _version_key(os.path.basename(p)), reverse=True):
        candidates = []
        try:
            for e in os.scandir(vdir):
                if not e.is_dir():
                    continue
                n = e.name.lower()
                if n == lname:
                    candidates.append((-1, e.path))
                else:
                    idx, sep, rest = n.partition("@")
                    if sep and rest == lname and idx.isdigit():
                        candidates.append((int(idx), e.path))
        except OSError:
            continue
        for _idx, cdir in sorted(candidates):
            thumb = os.path.join(cdir, "t.png")
            if os.path.isfile(thumb):
                return thumb
    return None


def _dicom_passthrough(tags: dict) -> dict:
    """Tags passed to targets as media['dicom_tags'] (patient fields excluded)."""
    skip = {"PatientName", "PatientID", "PatientBirthDate", "PatientSex",
            "TransferSyntaxUID"}
    return {k: v for k, v in tags.items() if v and k not in skip}


# ---------------------------------------------------------------------------
# MSSQL helpers
# ---------------------------------------------------------------------------

def _parse_conn_string(cs: str) -> dict:
    out = {}
    for part in (cs or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


def _pick_odbc_driver() -> str:
    import pyodbc
    installed = set(pyodbc.drivers())
    for d in _ODBC_PREFERENCE:
        if d in installed:
            return d
    raise RuntimeError(
        "No SQL Server ODBC driver found. Installed drivers: "
        + (", ".join(sorted(installed)) or "none"))


def _to_odbc(cs: str) -> str:
    """Convert an ADO.NET SqlClient connection string to an ODBC one."""
    if "driver=" in (cs or "").lower():
        return cs
    p = _parse_conn_string(cs)
    driver   = _pick_odbc_driver()
    server   = p.get("server") or p.get("data source") or p.get("address") or "localhost"
    database = p.get("database") or p.get("initial catalog") or "CSIS"
    trusted  = (p.get("trusted_connection") or p.get("integrated security") or "").lower()
    trusted  = trusted in ("true", "yes", "sspi")
    user     = p.get("user id") or p.get("uid") or p.get("user")
    pwd      = p.get("password") or p.get("pwd")
    trust_ct = (p.get("trustservercertificate") or "").lower() in ("true", "yes")

    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    if trusted or not user:
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={user}", f"PWD={{{pwd or ''}}}"]
    # Only the modern drivers understand these keywords
    if driver.startswith("ODBC Driver"):
        if trust_ct:
            parts.append("TrustServerCertificate=yes")
        if driver.startswith("ODBC Driver 18") and "encrypt" not in p:
            parts.append("Encrypt=optional")
    return ";".join(parts) + ";"


def _service_account() -> str:
    try:
        import getpass
        return f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
    except Exception:
        return "unknown"


_PATIENTS_SQL = """
SELECT
    PATIENT.id,
    PATIENT.dicom_patient_id,
    PATIENT.last_name,
    PATIENT.first_name,
    PATIENT.middle_name,
    PATIENT.sex,
    PATIENT.birth_date,
    PATIENT.image_repo_id,
    PATIENT_DIR.path AS patient_dir
FROM PATIENT
LEFT JOIN PATIENT_DIR ON PATIENT.id = PATIENT_DIR.patient_id
ORDER BY PATIENT.id
"""

_REPOS_SQL = "SELECT id, path FROM IMAGE_REPO"


# ---------------------------------------------------------------------------
# Source class
# ---------------------------------------------------------------------------

class CSImagingSource(BaseDatasource):

    @property
    def name(self) -> str:
        return "csimaging"

    @property
    def display_name(self) -> str:
        return "CS Imaging"

    @property
    def role(self) -> str:
        return "source"

    def _setup_configuration(self):
        self.configuration = [
            ConfigurationItem(
                key="Mode",
                name="Source Type",
                description="CS Imaging v8.3+ uses the MSSQL database. Use Patient Folder "
                            "for older installs or Trophy / Kodak RVG folder structures.",
                value="mssql",
                options={"mssql": "MSSQL Database (v8.3+)", "folder": "Patient Folder"},
                config_type=ConfigurationType.SELECT,
                group="Connection",
            ),
            ConfigurationItem(
                key="ConnectionString",
                name="Connection String",
                description="MSSQL mode. SqlClient (ADO.NET) or ODBC format. Windows "
                            "authentication runs as the account the tool is running under.",
                value=_DEFAULT_CONN,
                placeholder=_DEFAULT_CONN,
                config_type=ConfigurationType.CONNECTION_STRING,
                group="Connection",
            ),
            ConfigurationItem(
                key="MediaPath",
                name="Media Folder",
                description="Patient Folder mode: root folder to search for patient folders.",
                value="",
                placeholder=r"C:\CSImaging\Images",
                config_type=ConfigurationType.PATH,
                group="Connection",
            ),
            ConfigurationItem(
                key="PatientFolderTemplate",
                name="Patient Folder Template",
                description=r"Patient Folder mode. Case-insensitive regex matched against the "
                            r"path relative to the Media Folder. CS Imaging: ^[0-9a-f-]+$  "
                            r"Trophy/Kodak: ^(.)\.RVG\\\1\d+$",
                value=_DEFAULT_TEMPLATE,
                placeholder=_DEFAULT_TEMPLATE,
                advanced=True,
                config_type=ConfigurationType.TEXT,
                group="Folder Mode",
            ),
            ConfigurationItem(
                key="SearchDepth",
                name="Search Depth",
                description="Patient Folder mode: maximum number of subfolder levels to search.",
                value="3",
                placeholder="3",
                advanced=True,
                config_type=ConfigurationType.TEXT,
                group="Folder Mode",
            ),
            ConfigurationItem(
                key="ImageRepoPaths",
                name="Image Repository Overrides",
                description="MSSQL mode, optional. Remap image repositories when this machine "
                            "sees them under a different path, as id=path;id=path "
                            r"(e.g. 1=\\SERVER\CSImages). Test Connection lists the repository IDs.",
                value="",
                advanced=True,
                config_type=ConfigurationType.TEXT,
                group="Advanced",
            ),
            ConfigurationItem(
                key="Encoding",
                name="Text Encoding",
                description="Fallback encoding for DICOM text when the file declares no character set",
                value="windows-1252",
                options=COMMON_ENCODINGS,
                advanced=True,
                config_type=ConfigurationType.SELECT,
                group="Advanced",
            ),
        ]

    # ------------------------------------------------------------------
    # Config accessors
    # ------------------------------------------------------------------

    def _mode(self) -> str:
        return (self.get_config_value("Mode") or "mssql").strip().lower()

    def _encoding(self) -> str:
        return self.get_config_value("Encoding") or "windows-1252"

    def _repo_overrides(self) -> dict:
        out = {}
        for part in (self.get_config_value("ImageRepoPaths") or "").split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
        return out

    def _connect(self):
        import pyodbc
        return pyodbc.connect(_to_odbc(self.get_config_value("ConnectionString") or ""),
                              timeout=15)

    def _get_repos(self, conn) -> dict:
        """{repo_id(str): effective_path} — overrides applied."""
        overrides = self._repo_overrides()
        repos = {}
        cur = conn.cursor()
        for rid, path in cur.execute(_REPOS_SQL).fetchall():
            if rid is None:
                continue
            repos[str(rid)] = overrides.get(str(rid)) or (path or "")
        return repos

    # ------------------------------------------------------------------
    # Validation / connection test
    # ------------------------------------------------------------------

    def validate(self) -> tuple[bool, str]:
        mode = self._mode()
        if mode == "mssql":
            if not (self.get_config_value("ConnectionString") or "").strip():
                return False, "Connection string is empty."
            try:
                import pyodbc  # noqa: F401
            except ImportError:
                return False, "pyodbc is not installed — run: pip install pyodbc"
            return True, "CS Imaging (MSSQL) configuration is valid."
        if mode == "folder":
            mp = self.get_config_value("MediaPath") or ""
            if not mp:
                return False, "Media folder is not set."
            if not os.path.isdir(mp):
                return False, f"Media folder not found: {mp}"
            try:
                re.compile(self.get_config_value("PatientFolderTemplate") or _DEFAULT_TEMPLATE)
            except re.error as exc:
                return False, f"Patient folder template is not a valid regex: {exc}"
            try:
                if int(self.get_config_value("SearchDepth") or "3") < 1:
                    raise ValueError
            except ValueError:
                return False, "Search depth must be a whole number of 1 or more."
            return True, "CS Imaging (Patient Folder) configuration is valid."
        return False, f"Unknown source type: {mode}"

    def test_connection(self) -> tuple[bool, str]:
        return self.test_db_connection()

    def test_db_connection(self) -> tuple[bool, str]:
        ok, msg = self.validate()
        if not ok:
            return False, msg

        if self._mode() == "folder":
            folders = list(self._enumerate_patient_folders(lambda: False))
            return True, f"Found {len(folders)} patient folder(s) under {self.get_config_value('MediaPath')}."

        try:
            conn = self._connect()
        except Exception as exc:
            text = str(exc)
            if "Login failed" in text or "18456" in text:
                return False, (f"Login failed for the account the tool is running as "
                               f"({_service_account()}). Grant it access to the CSIS database, "
                               f"or use SQL authentication in the connection string.\n\n{text}")
            return False, text

        try:
            cur = conn.cursor()
            count = cur.execute("SELECT COUNT(*) FROM PATIENT").fetchone()[0]
            repos = self._get_repos(conn)
        finally:
            conn.close()

        lines = [f"Connected to CSIS. Found {count} patient(s)."]
        missing = 0
        for rid, path in sorted(repos.items()):
            exists = bool(path) and os.path.isdir(path)
            missing += 0 if exists else 1
            lines.append(f"  Repository {rid}: {path or '(empty)'} — "
                         f"{'OK' if exists else 'NOT FOUND'}")
        if missing:
            lines.append("Set Image Repository Overrides (Advanced) for repositories "
                         "this machine can't reach.")
            return False, "\n".join(lines)
        return True, "\n".join(lines)

    # ------------------------------------------------------------------
    # Folder discovery (folder mode)
    # ------------------------------------------------------------------

    def _enumerate_patient_folders(self, cancel_flag):
        base     = os.path.normpath(self.get_config_value("MediaPath") or "")
        pattern  = re.compile(self.get_config_value("PatientFolderTemplate") or _DEFAULT_TEMPLATE,
                              re.IGNORECASE)
        max_depth = int(self.get_config_value("SearchDepth") or "3")
        stack = [(base, 1)]
        while stack and not cancel_flag():
            path, depth = stack.pop()
            try:
                entries = [e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
            except OSError as exc:
                self.logger.warning(f"Cannot read {path}: {exc}")
                continue
            for e in entries:
                rel = os.path.relpath(e.path, base)
                if pattern.search(rel):
                    yield e.path
                elif depth < max_depth:
                    stack.append((e.path, depth + 1))

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self, cancel_flag, max_parallelism: int = 4,
             progress_callback: Optional[Callable] = None) -> list:
        # skip_empty is deliberately not offered: all patients load, media
        # filtering happens at migration time.
        if self._mode() == "mssql":
            jobs = self._mssql_jobs()
        else:
            if progress_callback:
                progress_callback(0, 1, "Locating patient folders…")
            base = os.path.normpath(self.get_config_value("MediaPath") or "")
            jobs = [{"patient": self._blank_patient(os.path.relpath(p, base)),
                     "folder": p, "from_files": True}
                    for p in self._enumerate_patient_folders(cancel_flag)]

        total     = len(jobs)
        patients  = []
        completed = [0]
        errors    = [0]
        skipped   = {"volume_series": 0, "scan": 0}
        lock      = threading.Lock()
        encoding  = self._encoding()

        if progress_callback:
            progress_callback(0, total, f"Loading media for {total} patients…")

        def process(job: dict) -> Optional[dict]:
            if cancel_flag():
                return None
            patient = job["patient"]
            try:
                counts = self._load_patient_media(patient, job["folder"],
                                                  job["from_files"], encoding, cancel_flag)
                with lock:
                    errors[0] += counts["errors"]
                    skipped["volume_series"] += counts["volume_series"]
                    skipped["scan"] += counts["scan"]
                if job["from_files"] and not (patient["family_name"] or patient["given_names"]):
                    patient["family_name"] = os.path.basename(job["folder"])
                    self.logger.warning(f"No demographics in {job['folder']} — "
                                        f"using folder name as surname")
                return patient
            except Exception as exc:
                self.logger.error(f"Failed to load patient {patient.get('uid')}: {exc}")
                with lock:
                    errors[0] += 1
                return patient   # still listed so it can be matched / reviewed

        with ThreadPoolExecutor(max_workers=max_parallelism) as pool:
            futures = [pool.submit(process, j) for j in jobs]
            for fut in as_completed(futures):
                if cancel_flag():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                result = fut.result()
                with lock:
                    completed[0] += 1
                    if result is not None:
                        patients.append(result)
                if progress_callback:
                    name = (f"{result['given_names']} {result['family_name']}".strip()
                            if result else "")
                    progress_callback(completed[0], total,
                                      f"Loaded: {name}" if name else
                                      f"Loaded {completed[0]}/{total} patients…")

        self.logger.info(
            f"CS Imaging load complete — {len(patients)} patients, {errors[0]} error(s), "
            f"{skipped['volume_series']} single-slice CBCT series and "
            f"{skipped['scan']} intraoral scan(s) not migrated (unsupported by targets)")
        return patients

    def read_patients(self, progress_callback=None):
        cancelled = [False]
        return self.load(cancel_flag=lambda: cancelled[0],
                         progress_callback=progress_callback)

    # ------------------------------------------------------------------

    @staticmethod
    def _blank_patient(uid: str) -> dict:
        return {"uid": uid, "id": "", "given_names": "", "family_name": "",
                "middle_name": "", "birth_date": "", "sex": "", "studies": {}}

    def _mssql_jobs(self) -> list:
        conn = self._connect()
        try:
            repos = self._get_repos(conn)
            rows  = conn.cursor().execute(_PATIENTS_SQL).fetchall()
        finally:
            conn.close()

        jobs, seen = [], set()
        token = re.compile(re.escape("%ImageRepo%"), re.IGNORECASE)
        for r in rows:
            pid = str(r.id)
            if pid in seen:
                self.logger.warning(f"Patient {pid} has more than one PATIENT_DIR — "
                                    f"using the first")
                continue
            seen.add(pid)
            folder = r.patient_dir or ""
            if folder:
                repo = repos.get(str(r.image_repo_id), "")
                folder = token.sub(lambda _m: repo, folder)
                folder = os.path.normpath(folder)
            patient = self._blank_patient(pid)
            patient.update({
                "id":          (r.dicom_patient_id or "").strip() or pid,
                "family_name": (r.last_name or "").strip(),
                "given_names": (r.first_name or "").strip(),
                "middle_name": (r.middle_name or "").strip(),
                "birth_date":  _birth_date(r.birth_date),
                "sex":         _parse_sex(r.sex),
            })
            jobs.append({"patient": patient, "folder": folder, "from_files": False})
        return jobs

    def _apply_demographics(self, patient: dict, tags: dict, prefer_file: bool):
        family, given, middle = _parse_person_name(tags.get("PatientName", ""))
        values = {
            "family_name": family,
            "given_names": given,
            "middle_name": middle,
            "id":          tags.get("PatientID", ""),
            "birth_date":  _da_to_iso(tags.get("PatientBirthDate", "")),
            "sex":         _parse_sex(tags.get("PatientSex", "")),
        }
        for k, v in values.items():
            if v and (prefer_file or not patient.get(k)):
                patient[k] = v

    def _add_media(self, patient: dict, media: dict, study_uid: str, series_uid: str,
                   tags: dict):
        study = patient["studies"].setdefault(study_uid, {
            "uid":            study_uid,
            "study_instance": tags.get("StudyInstanceUID", ""),
            "study_datetime": _dicom_dt({"StudyDate": tags.get("StudyDate", ""),
                                         "StudyTime": tags.get("StudyTime", "")})
                              or media.get("acq_datetime", ""),
            "accession":      tags.get("AccessionNumber", ""),
            "description":    tags.get("StudyDescription", ""),
            "ref_physician":  tags.get("ReferringPhysicianName", ""),
            "series":         {},
        })
        series = study["series"].setdefault(series_uid, {
            "uid":        series_uid,
            "dicom_tags": _dicom_passthrough(tags),
            "media":      [],
        })
        series["media"].append(media)

    def _dicom_media(self, path: str, tags: dict, force_class: Optional[str] = None) -> dict:
        if force_class:
            image_class, modality = _CLASS[force_class]
            reason = f"folder:{force_class}"
        else:
            image_class, modality, reason = _classify_dicom(tags)
        self.logger.debug(f"{os.path.basename(path)} → {image_class} ({reason})")
        # CS Imaging keeps its own thumbnail per image
        preview = _csi_thumbnail(path)
        if preview is None:
            self.logger.debug(f"No CS thumbnail for {path}")
        return {
            "uid":          tags.get("SOPInstanceUID") or path,
            "sop_instance": tags.get("SOPInstanceUID", ""),
            "image_class":  image_class,
            "modality":     modality,
            "media_type":   modality,
            "file_path":    path,
            "content_type": "application/dicom",
            "preview_path": preview,
            "acq_datetime": _dicom_dt(tags),
            "dicom_tags":   _dicom_passthrough(tags),
            "comments":     tags.get("SeriesDescription", ""),
        }

    def _load_patient_media(self, patient: dict, folder: str, from_files: bool,
                            encoding: str, cancel_flag) -> dict:
        counts = {"errors": 0, "volume_series": 0, "scan": 0}
        if not folder or not os.path.isdir(folder):
            self.logger.warning(f"Patient folder {folder or '(none)'} does not exist for "
                                f"patient src id {patient['uid']}")
            return counts

        subdirs, files = [], []
        for e in os.scandir(folder):
            if e.name.startswith("."):
                continue
            (subdirs if e.is_dir() else files).append(e)

        # ── Top-level 2D images ────────────────────────────────────────
        for e in sorted(files, key=lambda x: x.name):
            if cancel_flag():
                return counts
            try:
                ext = os.path.splitext(e.name)[1].lower()
                tags = read_dicom_header(e.path, encoding) if ext not in _IMAGE_EXTS else None
                if tags is not None:
                    if from_files:
                        self._apply_demographics(patient, tags, prefer_file=False)
                    media = self._dicom_media(e.path, tags)
                    self._add_media(patient, media,
                                    tags.get("StudyInstanceUID") or f"{patient['uid']}-{e.name}",
                                    tags.get("SeriesInstanceUID") or f"{e.name}-series",
                                    tags)
                elif ext in _IMAGE_EXTS:
                    ct = mimetypes.guess_type(e.path)[0] or "application/octet-stream"
                    acq = datetime.fromtimestamp(e.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
                    media = {
                        "uid":          e.name,
                        "sop_instance": "",
                        "image_class":  "Snapshot",
                        "modality":     "PICTURE",
                        "media_type":   "PICTURE",
                        "file_path":    e.path,
                        "content_type": ct,
                        "preview_path": e.path if ct in ("image/jpeg", "image/png") else None,
                        "acq_datetime": acq,
                        "dicom_tags":   {},
                        "comments":     "",
                    }
                    # One study+series per non-DICOM file, as SOPRO does
                    self._add_media(patient, media, f"{patient['uid']}-{e.name}",
                                    "Snapshot", {})
                # anything else (thumbnails db, xml, etc.) is ignored
            except Exception as exc:
                counts["errors"] += 1
                self.logger.error(f"Failed to load {e.path} for patient {patient['uid']}: {exc}")

        # ── CBCT volumes (VOL_ / TASK_) ────────────────────────────────
        volume_dirs = set()
        for d in sorted(subdirs, key=lambda x: x.name):
            if cancel_flag() or not d.name.upper().startswith(_VOLUME_PREFIXES):
                continue
            try:
                first = next((f for f in sorted(os.scandir(d.path), key=lambda x: x.name)
                              if f.is_file() and not f.name.startswith(".")
                              and read_dicom_header(f.path, encoding) is not None), None)
                if first is None:
                    continue
                tags = read_dicom_header(first.path, encoding)
                frames = int(tags.get("NumberOfFrames") or "1")
                volume_dirs.add(d.path)
                if from_files:
                    self._apply_demographics(patient, tags, prefer_file=True)
                if frames <= 1:
                    counts["volume_series"] += 1
                    self.logger.warning(f"Single-slice CBCT series in {d.path} not migrated "
                                        f"(not yet supported by targets)")
                    continue
                media = self._dicom_media(first.path, tags, force_class="Dvt")
                self._add_media(patient, media,
                                tags.get("StudyInstanceUID") or f"{patient['uid']}-{d.name}",
                                tags.get("SeriesInstanceUID") or d.name, tags)
            except Exception as exc:
                counts["errors"] += 1
                self.logger.error(f"Failed to load volume {d.path} for patient "
                                  f"{patient['uid']}: {exc}")

        # ── Intraoral scans (3DIO_ / DIO_, and TASK_ that wasn't a volume) ──
        for d in subdirs:
            name = d.name.upper()
            is_scan = name.startswith(_SCAN_PREFIXES) or (
                name.startswith("TASK_") and d.path not in volume_dirs)
            if is_scan:
                counts["scan"] += 1
                self.logger.warning(f"Intraoral scan in {d.path} not migrated "
                                    f"(not yet supported by targets)")
        return counts
