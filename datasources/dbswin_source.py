"""
datasources/dbswin_source.py
============================
Source datasource for DBSWin (Dürr Dental) imaging software.

DBSWin stores imaging data in a Firebird database (DUERRDBSWIN.FDB) with image
files written to two directories:
  - XrayImg/   — X-ray images (ITYP=1), JP2 format with optional .im0 sidecar
  - VidImg/    — Video/camera images (ITYP=0), JP2 format

Database structure (key tables):
  PATIENT     — demographics (PNR, KNR, OPENDENTAID, PVNAME, PNNAME, GDATE, SEXPAT)
  XRAYVIDEO   — media records  (VORGNR, PNR, ORGFILE, ITYP, AUFNDATUM, AUFNZEIT,
                                 KVOLT, MILLIAMP, DOSEAREAPRODUCT, ROEDAUER, IMGINFO)
  DCMIMG      — DICOM metadata (VORGNR, SOPINSTANCEUID, INSTANCENUMBER, MODALITY,
                                 SERIESINSTANCEUID, STUDYINSTANCEUID, SERIESDATE,
                                 SERIESTIME, SERIESNUMBER, PERFORMINGPHYSICIAN,
                                 SERIESDESCRIPTION)
  STUDY       — study metadata  (UID, STUDYID, STUDYDESCRIPTION, STUDYDATE, STUDYTIME,
                                 REFERRINGPHYSICIAN, ACCESSIONNUMBER)

Paths are discovered from:
  <DBSDATA>/pr1/database/dbident.ini  — contains XRayImagePath and VideoImagePath keys
  <DBSDATA>/pr1/database/DUERRDBSWIN.FDB — the Firebird database

The Firebird connection is made via FbBridge.exe (embedded ServerType=1) using
the same infrastructure as VistaSoftSource.

MODALITY → image_class / media_type mapping (from C# reference):
  OT → unknown (fallback to sidecar)
  IO → INTRAORAL / image/intraoral_xray
  CR → unknown (fallback to sidecar)
  PX → PANORAMIC / image/pano
  DX → CEPHALOGRAM / image/ceph
  CT → VOLUME / volume/multi_frame
  ES → PICTURE / image/intraoral_camera
  XC → PICTURE / image/clinical_picture

DBSWin Subtype values (from ImageUserData in .im0 sidecar):
  59  = Intraoral X-ray
  232 = Panoramic (OPG)
  60  = Cephalogram
"""

import configparser
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from core.base_datasource import BaseDatasource
from core.models import ConfigurationItem, ConfigurationType

# ---------------------------------------------------------------------------
# Shared encoding options
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# MODALITY → (image_class, media_type)
# OT and CR are intentionally absent — they fall through to sidecar resolution
# ---------------------------------------------------------------------------
_MODALITY_MAP = {
    "IO": ("Intra",    "INTRAORAL"),
    "PX": ("Pano",     "PANORAMIC"),
    "DX": ("Ceph",     "CEPHALOGRAM"),
    "CT": ("Dvt",      "VOLUME"),
    "ES": ("Snapshot", "PICTURE"),
    "XC": ("Snapshot", "PICTURE"),
}

# Modalities that provide no useful type info — must resolve from sidecar
_MODALITY_NEEDS_SIDECAR = {"OT", "CR", ""}

# Default fallback when nothing else resolves
_ITYP_0_CLASS   = ("Snapshot", "PICTURE")   # ITYP=0 video/camera
_ITYP_1_DEFAULT = ("Intra",    "INTRAORAL")  # ITYP=1 X-ray fallback

# DBSWin Subtype → (image_class, media_type)
# Only subtypes with confirmed values are mapped; unknown falls back to INTRAORAL
_SUBTYPE_MAP = {
    232: ("Pano", "PANORAMIC"),
    60:  ("Ceph", "CEPHALOGRAM"),
    # 59 and all others → INTRAORAL (handled by default)
}


# ---------------------------------------------------------------------------
# INI file helpers
# ---------------------------------------------------------------------------

def _read_ini_bytes(data: bytes, encoding: str = "utf-8") -> dict:
    """Parse an INI file from raw bytes. Returns {section: {key: value}}."""
    try:
        text = data.decode(encoding, errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    cp = configparser.RawConfigParser()
    cp.read_string(text)
    return {s: dict(cp.items(s)) for s in cp.sections()}


def _read_ini_file(path: str, encoding: str = "utf-8") -> dict:
    """Parse an INI file from disk. Returns {section: {key: value}}."""
    cp = configparser.RawConfigParser()
    cp.read(path, encoding=encoding)
    return {s: dict(cp.items(s)) for s in cp.sections()}


def _ini_get(ini: dict, section: str, key: str) -> Optional[str]:
    """Case-insensitive lookup in a parsed INI dict."""
    sl = section.lower()
    kl = key.lower()
    for s, vals in ini.items():
        if s.lower() == sl:
            for k, v in vals.items():
                if k.lower() == kl:
                    return v
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_DEFAULT_DBSDATA = r"C:\DBS\DBSDATA"
_FIREBIRD_CONF   = r"C:\Program Files (x86)\Duerr\FBS\firebird.conf"


def _get_fdb_path(media_path: Optional[str]) -> Optional[str]:
    if not media_path:
        return None
    p = os.path.join(media_path, "pr1", "database", "DUERRDBSWIN.FDB")
    return p if os.path.isfile(p) else None


def _get_dbident_path(media_path: Optional[str]) -> Optional[str]:
    if not media_path:
        return None
    p = os.path.join(media_path, "pr1", "database", "dbident.ini")
    return p if os.path.isfile(p) else None


def _get_default_media_path() -> Optional[str]:
    return _DEFAULT_DBSDATA if os.path.isdir(_DEFAULT_DBSDATA) else None


def _get_xray_image_path(media_path: Optional[str]) -> Optional[str]:
    if not media_path:
        return None
    dbident = _get_dbident_path(media_path)
    if dbident:
        ini = _read_ini_file(dbident)
        v = _ini_get(ini, "Praxis", "XRayImagePath")
        if v:
            return v
    return os.path.join(media_path, "pr1", "XrayImg")


def _get_video_image_path(media_path: Optional[str]) -> Optional[str]:
    if not media_path:
        return None
    dbident = _get_dbident_path(media_path)
    if dbident:
        ini = _read_ini_file(dbident)
        v = _ini_get(ini, "Praxis", "VideoImagePath")
        if v:
            return v
    return os.path.join(media_path, "pr1", "VidImg")


def _get_fdb_port() -> int:
    if os.path.isfile(_FIREBIRD_CONF):
        try:
            ini = _read_ini_file(_FIREBIRD_CONF)
            v = _ini_get(ini, "DEFAULT", "RemoteServicePort")
            if v and v.strip().isdigit():
                return int(v.strip())
        except Exception:
            pass
    return 3050


# ---------------------------------------------------------------------------
# FbBridge query helpers
# All SQL is passed via JSON envelope (stdin) to avoid Windows shell quoting
# ---------------------------------------------------------------------------

def _fb_query(db_path: str, sql: str) -> list:
    """Execute SQL via FbBridge v2 (Firebird 2.5 embedded). Returns list of row dicts."""
    import json as _json
    from datasources.fb_client import _run_v2
    payload = _json.dumps({"sql": sql})
    data = _run_v2("query", db_path, payload)
    return data.get("rows", [])


def _fb_query_patient_ids(db_path: str) -> list:
    rows = _fb_query(db_path, "SELECT PNR FROM PATIENT WHERE PNR != '1'")
    return [str(r.get("PNR", r.get("pnr", ""))) for r in rows if r]


def _fb_query_patient(db_path: str, patient_id: str) -> Optional[dict]:
    sql = (
        "SELECT PNR, KNR, OPENDENTAID, SEXPAT, GDATE, PVNAME, PNNAME "
        f"FROM PATIENT WHERE PNR = '{_esc(patient_id)}'"
    )
    rows = _fb_query(db_path, sql)
    return rows[0] if rows else None


_MEDIA_SQL = (
    "SELECT v.PNR, v.VORGNR, v.AUFNDATUM, v.AUFNZEIT, v.ORGFILE, v.IMGINFO, "
    "v.KVOLT, v.MILLIAMP, v.DOSEAREAPRODUCT, v.ROEDAUER, v.ITYP, "
    "i.SOPINSTANCEUID, i.INSTANCENUMBER, i.MODALITY, i.SERIESINSTANCEUID, "
    "i.STUDYINSTANCEUID, i.SERIESDATE, i.SERIESTIME, i.SERIESNUMBER, "
    "i.PERFORMINGPHYSICIAN, i.SERIESDESCRIPTION, "
    "s.STUDYID, s.STUDYDESCRIPTION, s.STUDYDATE, s.STUDYTIME, "
    "s.REFERRINGPHYSICIAN, s.ACCESSIONNUMBER "
    "FROM XRAYVIDEO AS v "
    "LEFT JOIN DCMIMG AS i ON i.VORGNR = v.VORGNR "
    "LEFT JOIN STUDY  AS s ON s.UID    = i.STUDYINSTANCEUID "
    "WHERE v.PNR = '{pid}'"
)


def _esc(s: str) -> str:
    return s.replace("'", "''")


def _fb_query_media(db_path: str, patient_id: str) -> list:
    return _fb_query(db_path, _MEDIA_SQL.format(pid=_esc(patient_id)))


# ---------------------------------------------------------------------------
# Row → patient / media dict builders
# ---------------------------------------------------------------------------

def _g(row: dict, *keys) -> Optional[str]:
    """Get first non-empty value from row, case-insensitive key lookup."""
    row_lower = {k.lower(): v for k, v in row.items()}
    for k in keys:
        v = row_lower.get(k.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _parse_gender(s: Optional[str]) -> str:
    if not s:
        return ""
    c = s.strip().lower()[:1]
    if c == "m":
        return "MALE"
    if c == "f":
        return "FEMALE"
    return "OTHER"


def _parse_date(val: Optional[str]) -> str:
    if not val:
        return ""
    val = str(val).strip().split("T")[0].split(" ")[0]
    if len(val) == 10 and val[4] == "-":
        return val                                         # YYYY-MM-DD
    if len(val) == 8 and val.isdigit():
        return f"{val[:4]}-{val[4:6]}-{val[6:]}"         # YYYYMMDD
    if len(val) == 10 and val[2] == "/" and val[5] == "/":
        return f"{val[6:]}-{val[3:5]}-{val[:2]}"         # DD/MM/YYYY
    if len(val) == 10 and val[2] == "." and val[5] == ".":
        return f"{val[6:]}-{val[3:5]}-{val[:2]}"         # DD.MM.YYYY
    try:
        from datetime import datetime
        for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    except Exception:
        pass
    return val


def _parse_datetime(date_val: Optional[str], time_val: Optional[str]) -> str:
    d = _parse_date(date_val)
    if not d:
        return ""
    if not time_val:
        return f"{d}T00:00:00"
    t = str(time_val).strip()
    if len(t) == 6 and t.isdigit():
        t = f"{t[:2]}:{t[2:4]}:{t[4:]}"
    return f"{d}T{t[:8]}"


def _build_patient(row: dict) -> dict:
    return {
        "uid":           _g(row, "PNR") or "",
        "id":            _g(row, "PNR") or "",
        "external_id":   _g(row, "KNR") or "",
        "additional_id": _g(row, "OPENDENTAID") or "",
        "given_names":   _g(row, "PVNAME") or "",
        "family_name":   _g(row, "PNNAME") or "",
        "dob":           _parse_date(_g(row, "GDATE")),
        "sex":           _parse_gender(_g(row, "SEXPAT")),
        "studies":       {},
    }


def _resolve_image_class_from_sidecar(im0_ini: dict, image_class: str, media_type: str):
    """
    Resolve image_class / media_type from .im0 sidecar when MODALITY is
    ambiguous (OT, CR, empty).

    Priority:
      1. [XRAYEMITTER] Category  — sensor-acquired images
      2. [ImageUserData] Subtype — imported DICOM images (Subtype is authoritative)
    """
    # 1. Sensor-acquired: [XRAYEMITTER] Category
    category = (_ini_get(im0_ini, "XRAYEMITTER", "Category") or "").upper().strip()
    if category == "INTRA":
        return "Intra", "INTRAORAL"
    if category in ("PANO", "OPG", "PANORAMIC", "PANO_OPG"):
        return "Pano", "PANORAMIC"
    if category in ("CEPH", "CEPHALOMETRIC"):
        return "Ceph", "CEPHALOGRAM"

    # 2. Imported image: [ImageUserData] Subtype
    # Use ONLY Subtype — do NOT use ImgXrayEmitter as it contains the machine
    # name (e.g. "ORTHOPANTOMOGRAPH OP 3D") which is the same for ALL images
    # from the same device regardless of image type.
    subtype_str = (_ini_get(im0_ini, "ImageUserData", "Subtype") or "").strip()
    if subtype_str:
        try:
            subtype = int(subtype_str)
            if subtype in _SUBTYPE_MAP:
                return _SUBTYPE_MAP[subtype]
        except ValueError:
            pass

    # Nothing resolved — return current values unchanged
    return image_class, media_type


def _build_media_item(
    row: dict,
    xray_path: str,
    vid_path: str,
    encoding: str = "utf-8",
) -> Optional[dict]:
    """
    Convert a single XRAYVIDEO/DCMIMG/STUDY joined row to the internal media dict.
    Returns None if the row has no usable file or ITYP is unrecognised.
    """
    ityp_raw = _g(row, "ITYP")
    try:
        ityp = int(ityp_raw) if ityp_raw is not None else None
    except (ValueError, TypeError):
        ityp = None

    if ityp is None:
        return None

    if ityp == 0:
        base_path = vid_path
    elif ityp == 1:
        base_path = xray_path
    else:
        return None

    orgfile = _g(row, "ORGFILE")
    file_path = os.path.join(base_path, orgfile) if orgfile else None

    # ── Step 1: resolve from MODALITY column ─────────────────────────────
    modality = _g(row, "MODALITY") or ""
    if ityp == 0:
        image_class, media_type = _ITYP_0_CLASS
    else:
        image_class, media_type = _MODALITY_MAP.get(modality, _ITYP_1_DEFAULT)

    # Flag modalities that need sidecar resolution
    needs_sidecar = modality in _MODALITY_NEEDS_SIDECAR

    # ── Step 2: read .im0 sidecar ─────────────────────────────────────────
    manufacturer  = None
    serial_number = None
    model_name    = None
    station_name  = None
    imager_pixel_x = None
    imager_pixel_y = None

    if file_path and os.path.isfile(file_path):
        imginfo_bytes = row.get("IMGINFO") or row.get("imginfo")
        im0_ini: Optional[dict] = None

        if isinstance(imginfo_bytes, (bytes, bytearray)) and imginfo_bytes:
            try:
                im0_ini = _read_ini_bytes(bytes(imginfo_bytes), encoding)
            except Exception:
                pass

        if im0_ini is None:
            im0_path = os.path.splitext(file_path)[0] + ".im0"
            if os.path.isfile(im0_path):
                try:
                    im0_ini = _read_ini_file(im0_path, encoding)
                except Exception:
                    pass

        if im0_ini:
            manufacturer  = _ini_get(im0_ini, "XRAYEMITTER",         "Manufacturer")
            serial_number = _ini_get(im0_ini, "RawImageCreationInfo", "SerialNr")
            model_name    = _ini_get(im0_ini, "CREATION",             "SOURCE")
            station_name  = _ini_get(im0_ini, "CREATION",             "MachineName")

            # Step 2a: resolve image type from sidecar when MODALITY is ambiguous
            if needs_sidecar:
                image_class, media_type = _resolve_image_class_from_sidecar(
                    im0_ini, image_class, media_type
                )

            # Step 2b: pixel spacing from DPI
            dpi_x_str = _ini_get(im0_ini, "CREATION", "InputDeviceDPI_X")
            dpi_y_str = _ini_get(im0_ini, "CREATION", "InputDeviceDPI_Y")
            if dpi_x_str and dpi_y_str:
                try:
                    mm_per_inch = 25.4
                    imager_pixel_x = float(dpi_x_str.replace(",", ".")) / mm_per_inch
                    imager_pixel_y = float(dpi_y_str.replace(",", ".")) / mm_per_inch
                except (ValueError, AttributeError):
                    pass

    # ── Step 3: .imo sidecar (orientation / invert) ───────────────────────
    orientation = None
    is_inverted = False

    if file_path and os.path.isfile(file_path):
        imo_path = os.path.splitext(file_path)[0] + ".imo"
        if os.path.isfile(imo_path):
            try:
                imo_ini = _read_ini_file(imo_path, encoding)
                ops_section = None
                for sec in imo_ini:
                    if sec.lower() == "imageoperations":
                        ops_section = imo_ini[sec]
                        break
                if ops_section:
                    rot_steps = 0
                    mirrored  = False
                    for _, op_val in sorted(ops_section.items()):
                        v = op_val.strip()
                        if v == "Invert  st=0 r=1 g=1 b=1":
                            is_inverted = not is_inverted
                        elif v == "Rotate  st=0 ax10=900 anc=5":
                            rot_steps = (rot_steps + 3) % 4
                        elif v == "Rotate  st=0 ax10=1800 anc=5":
                            rot_steps = (rot_steps + 2) % 4
                        elif v == "Rotate  st=0 ax10=2700 anc=5":
                            rot_steps = (rot_steps + 1) % 4
                        elif v in ("Orient  st=0 or=1", "Orient  st=0 or=2"):
                            mirrored = not mirrored
                    rot_map = {0: None, 1: "ROT90", 2: "ROT180", 3: "ROT270"}
                    orientation = rot_map[rot_steps]
                    if mirrored:
                        orientation = f"MIRROR_{orientation}" if orientation else "MIRROR"
            except Exception:
                pass

    # ── Step 4: generate preview JPEG ────────────────────────────────────
    preview_path = None
    if file_path and os.path.isfile(file_path):
        try:
            from PIL import Image
            import numpy as np
            preview_file = file_path + ".preview.jpg"
            if not os.path.isfile(preview_file):
                with Image.open(file_path) as img:
                    arr = np.array(img)
                    if arr.dtype != np.uint8:
                        arr_min, arr_max = arr.min(), arr.max()
                        if arr_max > arr_min:
                            arr = ((arr - arr_min) / (arr_max - arr_min) * 255).astype(np.uint8)
                        else:
                            arr = np.zeros_like(arr, dtype=np.uint8)
                    preview_img = Image.fromarray(arr)
                    if preview_img.mode not in ('L', 'RGB'):
                        preview_img = preview_img.convert('L')
                    preview_img.thumbnail((512, 512))
                    preview_img.save(preview_file, "JPEG", quality=75)
            preview_path = preview_file
        except Exception:
            preview_path = None

    # ── IDs / timestamps ─────────────────────────────────────────────────
    vorgnr     = _g(row, "VORGNR") or ""
    study_uid  = _g(row, "STUDYINSTANCEUID") or vorgnr
    series_uid = _g(row, "SERIESINSTANCEUID") or vorgnr
    sop_uid    = _g(row, "SOPINSTANCEUID") or ""
    acq_dt     = _parse_datetime(_g(row, "AUFNDATUM"), _g(row, "AUFNZEIT"))

    file_size = None
    if file_path and os.path.isfile(file_path):
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            pass

    dicom_tags = {
        "StudyInstanceUID":        study_uid,
        "SeriesInstanceUID":       series_uid,
        "SOPInstanceUID":          sop_uid,
        "Modality":                modality,
        "StudyDate":               _g(row, "STUDYDATE") or "",
        "StudyTime":               _g(row, "STUDYTIME") or "",
        "SeriesDate":              _g(row, "SERIESDATE") or "",
        "SeriesTime":              _g(row, "SERIESTIME") or "",
        "SeriesNumber":            _g(row, "SERIESNUMBER") or "",
        "InstanceNumber":          _g(row, "INSTANCENUMBER") or "",
        "SeriesDescription":       _g(row, "SERIESDESCRIPTION") or "",
        "StudyDescription":        _g(row, "STUDYDESCRIPTION") or "",
        "AccessionNumber":         _g(row, "ACCESSIONNUMBER") or "",
        "ReferringPhysicianName":  _g(row, "REFERRINGPHYSICIAN") or "",
        "PerformingPhysicianName": _g(row, "PERFORMINGPHYSICIAN") or "",
        "KVP":                     _g(row, "KVOLT") or "",
        "XRayTubeCurrent":         _g(row, "MILLIAMP") or "",
        "ExposureTime":            _g(row, "ROEDAUER") or "",
        "DoseAreaProduct":         _g(row, "DOSEAREAPRODUCT") or "",
        "Manufacturer":            manufacturer or "",
        "DeviceSerialNumber":      serial_number or "",
        "ManufacturerModelName":   model_name or "",
        "StationName":             station_name or "",
        "ImagerPixelSpacing":      (
            f"{imager_pixel_x:.6f}\\{imager_pixel_y:.6f}"
            if imager_pixel_x and imager_pixel_y else ""
        ),
    }

    return {
        "uid":          vorgnr,
        "sop_instance": sop_uid,
        "_study_uid":   study_uid,
        "_series_uid":  series_uid,
        "image_class":  image_class,
        "media_type":   media_type,
        "modality":     modality,
        "file_path":    file_path,
        "content_type": "image/jp2",
        "preview_path": preview_path,
        "size_bytes":   file_size,
        "acq_datetime": acq_dt,
        "orientation":  orientation,
        "is_inverted":  is_inverted,
        "dicom_tags":   dicom_tags,
        "comments":     _g(row, "SERIESDESCRIPTION") or "",
    }


# ---------------------------------------------------------------------------
# DBSWin Source class
# ---------------------------------------------------------------------------

class DBSWinSource(BaseDatasource):

    @property
    def name(self) -> str:
        return "dbswin"

    @property
    def display_name(self) -> str:
        return "DBSWin"

    @property
    def role(self) -> str:
        return "source"

    def _setup_configuration(self):
        default_path = _get_default_media_path()
        self.configuration = [
            ConfigurationItem(
                key="MediaPath",
                name="DBSDATA Folder",
                description=(
                    "Path to the DBSDATA folder (e.g. C:\\DBS\\DBSDATA). "
                    "Must be on a locally accessible drive."
                ),
                value=default_path or "",
                placeholder=r"C:\DBS\DBSDATA",
                advanced=False,
                config_type=ConfigurationType.PATH,
                group="Connection",
            ),
            ConfigurationItem(
                key="Encoding",
                name="Text Encoding",
                description="Encoding used to read .im0 / .imo sidecar files",
                value="windows-1252",
                advanced=True,
                options=COMMON_ENCODINGS,
                config_type=ConfigurationType.SELECT,
                group="Advanced",
            ),
            ConfigurationItem(
                key="SkipEmptyPatients",
                name="Skip Patients Without Media",
                description="Skip patients that have no associated imaging media",
                value="On",
                advanced=True,
                config_type=ConfigurationType.SWITCH,
                group="Advanced",
            ),
            ConfigurationItem(
                key="XRayImgPath",
                name="XrayImg Folder",
                description="Path to the XrayImg folder (auto-detected from dbident.ini)",
                value="",
                advanced=True,
                hidden=True,
                config_type=ConfigurationType.PATH,
                group="Advanced",
            ),
            ConfigurationItem(
                key="VidImgPath",
                name="VidImg Folder",
                description="Path to the VidImg folder (auto-detected from dbident.ini)",
                value="",
                advanced=True,
                hidden=True,
                config_type=ConfigurationType.PATH,
                group="Advanced",
            ),
        ]

    def validate(self) -> tuple[bool, str]:
        media_path = self.get_config_value("MediaPath")
        if not media_path:
            return False, "DBSDATA folder is not set."
        if not os.path.isdir(media_path):
            return False, f"DBSDATA folder not found: {media_path}"
        if not _get_fdb_path(media_path):
            return False, f"DUERRDBSWIN.FDB not found under {media_path}\\pr1\\database\\"
        if not _get_dbident_path(media_path):
            return False, f"dbident.ini not found under {media_path}\\pr1\\database\\"
        xray = _get_xray_image_path(media_path)
        if not xray or not os.path.isdir(xray):
            return False, f"XrayImg folder not found: {xray}"
        vid = _get_video_image_path(media_path)
        if not vid or not os.path.isdir(vid):
            return False, f"VidImg folder not found: {vid}"
        return True, "DBSWin configuration is valid."

    def test_connection(self) -> tuple[bool, str]:
        return self.test_db_connection()

    def test_db_connection(self) -> tuple[bool, str]:
        media_path = self.get_config_value("MediaPath")
        ok, msg = self.validate()
        if not ok:
            return False, msg
        fdb = _get_fdb_path(media_path)
        try:
            rows = _fb_query(fdb, "SELECT COUNT(*) AS CNT FROM PATIENT WHERE PNR != '1'")
            count = rows[0].get("CNT", rows[0].get("cnt", "?")) if rows else "?"
            return True, f"Connected to DUERRDBSWIN.FDB. Found {count} patient(s)."
        except Exception as exc:
            from datasources.fb_client import run_diag
            return False, f"{exc}\n\n--- FbBridge diagnostics ---\n{run_diag()}"

    def load(
        self,
        cancel_flag,
        max_parallelism: int = 4,
        progress_callback: Optional[Callable] = None,
    ) -> list:
        media_path = self.get_config_value("MediaPath") or ""
        encoding   = self.get_config_value("Encoding") or "windows-1252"
        skip_empty = (self.get_config_value("SkipEmptyPatients") or "On").strip().lower() in ("on", "true", "1")

        xray_path = (
            self.get_config_value("XRayImgPath") or
            _get_xray_image_path(media_path) or ""
        )
        vid_path = (
            self.get_config_value("VidImgPath") or
            _get_video_image_path(media_path) or ""
        )
        self.set_config_value("XRayImgPath", xray_path)
        self.set_config_value("VidImgPath",  vid_path)

        fdb = _get_fdb_path(media_path)
        if not fdb:
            raise FileNotFoundError(
                f"DUERRDBSWIN.FDB not found under {media_path}\\pr1\\database\\"
            )

        self.logger.debug("Fetching patient IDs from DBSWin database")
        patient_ids = _fb_query_patient_ids(fdb)
        total = len(patient_ids)
        self.logger.debug(f"Total patients: {total}")

        if progress_callback:
            progress_callback(0, total, f"Loading media for {total} patients…")

        patients  = []
        completed = [0]
        errors    = [0]
        lock      = threading.Lock()

        def process_patient(patient_id: str) -> Optional[dict]:
            if cancel_flag():
                return None
            try:
                pat_row = _fb_query_patient(fdb, patient_id)
                if not pat_row:
                    raise ValueError(f"Patient row not found for PNR={patient_id}")
                patient = _build_patient(pat_row)

                for row in _fb_query_media(fdb, patient_id):
                    if cancel_flag():
                        break
                    try:
                        media_item = _build_media_item(row, xray_path, vid_path, encoding)
                        if media_item is None:
                            continue

                        study_uid  = media_item.pop("_study_uid")
                        series_uid = media_item.pop("_series_uid")

                        study = patient["studies"].setdefault(study_uid, {
                            "uid":            study_uid,
                            "study_instance": study_uid,
                            "study_datetime": media_item["dicom_tags"].get("StudyDate", ""),
                            "accession":      media_item["dicom_tags"].get("AccessionNumber", ""),
                            "description":    media_item["dicom_tags"].get("StudyDescription", ""),
                            "ref_physician":  media_item["dicom_tags"].get("ReferringPhysicianName", ""),
                            "series":         {},
                        })
                        series = study["series"].setdefault(series_uid, {
                            "uid":        series_uid,
                            "dicom_tags": media_item["dicom_tags"],
                            "media":      [],
                        })
                        series["media"].append(media_item)

                    except Exception as exc:
                        self.logger.error(f"Failed to load media row for patient {patient_id}: {exc}")
                        with lock:
                            errors[0] += 1

                return patient

            except Exception as exc:
                self.logger.error(f"Failed to load patient {patient_id}: {exc}")
                with lock:
                    errors[0] += 1
                return None

        with ThreadPoolExecutor(max_workers=max_parallelism) as pool:
            futures = {pool.submit(process_patient, pid): pid for pid in patient_ids}
            for fut in as_completed(futures):
                if cancel_flag():
                    break
                patient = fut.result()
                if patient is not None:
                    has_media = any(
                        series.get("media")
                        for study in patient.get("studies", {}).values()
                        for series in study.get("series", {}).values()
                    )
                    if has_media or not skip_empty:
                        with lock:
                            patients.append(patient)

                with lock:
                    completed[0] += 1
                if progress_callback:
                    progress_callback(
                        completed[0], total,
                        f"Loaded {completed[0]}/{total} patients…"
                    )

        self.logger.info(
            f"DBSWin load complete: {len(patients)} patients, {errors[0]} error(s)"
        )
        return patients