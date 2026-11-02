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

The Firebird connection is made via FbBridge.exe (lib/fb_bridge_v2, Firebird 2.5
embedded). Firebird 2.5 embedded locks the database to one process at a time,
so ALL database reads here are sequential and done in bulk:
  1 query  for every patient
  N queries for media, batched by patient (_MEDIA_BATCH patients per query)
instead of two process launches per patient.

Previews are NOT generated during load. Each media item carries a
`_fetch_preview` callable that renders a small JPEG on first use and caches it
under %PROGRAMDATA%\\ITInfinityMigrator\\preview_cache\\dbswin\\ — nothing is
written into the practice's XrayImg / VidImg folders. `preview_path` is filled
in once a cached preview exists.

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

import hashlib
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
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

# Patients per media query. Firebird 2.5 allows up to 1500 items in an IN list;
# 500 keeps each FbBridge payload small and well inside its 60s timeout.
_MEDIA_BATCH = 500

# Longest edge of generated previews, in pixels
_PREVIEW_MAX = 512


# ---------------------------------------------------------------------------
# INI helpers
# A small lenient parser is used instead of configparser: DBSWin sidecars can
# have a BOM, duplicate keys or lines without '=' — configparser rejects the
# whole file in those cases, which silently breaks image classification.
# Sections and keys are stored lower-case; first occurrence of a key wins.
# ---------------------------------------------------------------------------

def _decode(data: bytes, encoding: Optional[str]) -> str:
    if encoding:
        text = data.decode(encoding, errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("windows-1252", errors="replace")
    # Strip a UTF-8 BOM whether it decoded correctly or as cp1252 junk
    if text.startswith("\ufeff"):
        text = text[1:]
    elif text.startswith("ï»¿"):
        text = text[3:]
    return text


def _parse_ini_text(text: str) -> dict:
    out: dict = {}
    current = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        if s[0] == "[" and "]" in s:
            current = out.setdefault(s[1:s.index("]")].strip().lower(), {})
            continue
        if current is None or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip().lower()
        if k not in current:
            current[k] = v.strip()
    return out


def _read_ini_file(path: str, encoding: Optional[str] = None) -> dict:
    """Parse an INI file from disk. Returns {section: {key: value}} (lower-case names)."""
    with open(path, "rb") as f:
        return _parse_ini_text(_decode(f.read(), encoding))


def _ini_get(ini: dict, section: str, key: str) -> Optional[str]:
    """Case-insensitive lookup in a parsed INI dict."""
    return (ini.get(section.lower()) or {}).get(key.lower())


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_DEFAULT_DBSDATA = r"C:\DBS\DBSDATA"


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


def _get_dbident_value(media_path: Optional[str], key: str) -> Optional[str]:
    dbident = _get_dbident_path(media_path)
    if not dbident:
        return None
    try:
        return _ini_get(_read_ini_file(dbident), "Praxis", key) or None
    except OSError:
        return None


def _get_xray_image_path(media_path: Optional[str]) -> Optional[str]:
    if not media_path:
        return None
    return (_get_dbident_value(media_path, "XRayImagePath")
            or os.path.join(media_path, "pr1", "XrayImg"))


def _get_video_image_path(media_path: Optional[str]) -> Optional[str]:
    if not media_path:
        return None
    return (_get_dbident_value(media_path, "VideoImagePath")
            or os.path.join(media_path, "pr1", "VidImg"))


class _DirIndex:
    """
    One directory listing per folder, shared by every media item.

    Replaces several os.path.isfile() calls per image (image, .im0, .imo,
    size) with a single scandir per folder. On Windows the file size comes
    free with the listing. Lookups are case-insensitive, like NTFS.
    """

    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def _listing(self, folder: str) -> dict:
        key = os.path.normcase(os.path.abspath(folder))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        names: dict = {}
        try:
            with os.scandir(folder) as it:
                for e in it:
                    try:
                        if e.is_file():
                            names[e.name.lower()] = (e.name, e.stat().st_size)
                    except OSError:
                        continue
        except OSError:
            pass
        with self._lock:
            self._cache.setdefault(key, names)
        return names

    def find(self, path: str):
        """Return (actual_path, size) if the file exists, else (None, None)."""
        folder, name = os.path.split(path)
        hit = self._listing(folder or ".").get(name.lower())
        if not hit:
            return None, None
        return os.path.join(folder, hit[0]), hit[1]


# ---------------------------------------------------------------------------
# Preview generation (on demand, cached outside the practice's folders)
# ---------------------------------------------------------------------------

def _preview_cache_dir() -> str:
    base = os.environ.get("PROGRAMDATA") or tempfile.gettempdir()
    d = os.path.join(base, "ITInfinityMigrator", "preview_cache", "dbswin")
    os.makedirs(d, exist_ok=True)
    return d


def _preview_cache_path(file_path: str, media_uid: str) -> str:
    key = media_uid or hashlib.md5(file_path.lower().encode("utf-8", "replace")).hexdigest()
    return os.path.join(_preview_cache_dir(), re.sub(r"[^A-Za-z0-9_-]", "_", key) + ".jpg")


def _render_preview_jpeg(src_path: str) -> bytes:
    """
    Render a small greyscale/RGB JPEG preview of a DBSWin image.

    JPEG 2000 files are decoded at reduced resolution (wavelet level skip),
    which is roughly 10-20x faster than a full decode for a 512px preview.
    High bit-depth images are normalised over their full range, matching the
    previews that were previously generated during load.
    """
    import io
    import numpy as np
    from PIL import Image

    def _open_reduced():
        img = Image.open(src_path)
        if getattr(img, "format", "") == "JPEG2000":
            w, h = img.size
            r = 0
            while r < 5 and (max(w, h) >> (r + 1)) >= _PREVIEW_MAX:
                r += 1
            if r:
                img.reduce = r
        elif getattr(img, "format", "") == "JPEG":
            img.draft(img.mode, (_PREVIEW_MAX, _PREVIEW_MAX))
        img.load()
        return img

    try:
        img = _open_reduced()
    except Exception:
        # Reduced decode isn't supported by every codestream — full decode
        img = Image.open(src_path)
        img.load()

    with img:
        arr = np.asarray(img)
        if arr.dtype != np.uint8:
            arr = arr.astype(np.float32)
            lo, hi = float(arr.min()), float(arr.max())
            if hi > lo:
                arr = ((arr - lo) / (hi - lo) * 255.0).astype(np.uint8)
            else:
                arr = np.zeros(arr.shape, dtype=np.uint8)
        preview = Image.fromarray(arr)
        if preview.mode not in ("L", "RGB"):
            preview = preview.convert("RGB" if arr.ndim == 3 else "L")
        preview.thumbnail((_PREVIEW_MAX, _PREVIEW_MAX))
        buf = io.BytesIO()
        preview.save(buf, "JPEG", quality=75)
        return buf.getvalue()


def _make_preview_fetch(media: dict) -> Callable[[], Optional[bytes]]:
    """
    Build the `_fetch_preview` callable for one media item.

    First call renders and caches the JPEG, then sets media["preview_path"]
    so later readers (Media page, targets) can use the file directly.
    """
    def _fetch() -> Optional[bytes]:
        p = media.get("preview_path")
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        src = media.get("file_path")
        if not src or not os.path.isfile(src):
            return None
        dest = _preview_cache_path(src, media.get("uid", ""))
        if os.path.isfile(dest):
            with open(dest, "rb") as f:
                data = f.read()
        else:
            data = _render_preview_jpeg(src)
            tmp = f"{dest}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)   # atomic — safe if two requests race
        media["preview_path"] = dest
        return data
    return _fetch


# ---------------------------------------------------------------------------
# FbBridge query helpers
# All SQL is passed via JSON envelope (stdin) to avoid Windows shell quoting.
# Queries run one at a time: Firebird 2.5 embedded won't share the file
# between processes.
# ---------------------------------------------------------------------------

def _fb_query(db_path: str, sql: str) -> list:
    """Execute SQL via FbBridge v2 (Firebird 2.5 embedded). Returns rows with UPPER-CASE keys."""
    import json as _json
    from datasources.fb_client import _run_v2
    data = _run_v2("query", db_path, _json.dumps({"sql": sql}))
    return [{str(k).upper(): v for k, v in r.items()} for r in data.get("rows", []) if r]


def _esc(s: str) -> str:
    return s.replace("'", "''")


_PATIENTS_SQL = (
    "SELECT PNR, KNR, OPENDENTAID, SEXPAT, GDATE, PVNAME, PNNAME "
    "FROM PATIENT WHERE PNR <> '1'"
)

# IMGINFO is deliberately not selected: FbBridge returns BLOBs as the string
# "System.Byte[]", so it was never usable and only bloated every row.
# The .im0 sidecar on disk carries the same data.
_MEDIA_SQL = (
    "SELECT v.PNR, v.VORGNR, v.AUFNDATUM, v.AUFNZEIT, v.ORGFILE, "
    "v.KVOLT, v.MILLIAMP, v.DOSEAREAPRODUCT, v.ROEDAUER, v.ITYP, "
    "i.SOPINSTANCEUID, i.INSTANCENUMBER, i.MODALITY, i.SERIESINSTANCEUID, "
    "i.STUDYINSTANCEUID, i.SERIESDATE, i.SERIESTIME, i.SERIESNUMBER, "
    "i.PERFORMINGPHYSICIAN, i.SERIESDESCRIPTION, "
    "s.STUDYID, s.STUDYDESCRIPTION, s.STUDYDATE, s.STUDYTIME, "
    "s.REFERRINGPHYSICIAN, s.ACCESSIONNUMBER "
    "FROM XRAYVIDEO AS v "
    "LEFT JOIN DCMIMG AS i ON i.VORGNR = v.VORGNR "
    "LEFT JOIN STUDY  AS s ON s.UID    = i.STUDYINSTANCEUID "
    "WHERE v.PNR IN ({pids})"
)


def _fb_query_media_batch(db_path: str, patient_ids: list, logger) -> list:
    """
    Media rows for a batch of patients. If a batch fails (e.g. FbBridge
    timeout on a very image-heavy batch) it is split in half and retried,
    down to a single patient, so one bad batch can't fail the whole load.
    """
    if not patient_ids:
        return []
    sql = _MEDIA_SQL.format(pids=",".join(f"'{_esc(p)}'" for p in patient_ids))
    try:
        return _fb_query(db_path, sql)
    except Exception as exc:
        if len(patient_ids) == 1:
            logger.error(f"Failed to read media for patient {patient_ids[0]}: {exc}")
            return []
        logger.warning(f"Media batch of {len(patient_ids)} failed ({exc}) — splitting and retrying")
        mid = len(patient_ids) // 2
        return (_fb_query_media_batch(db_path, patient_ids[:mid], logger)
                + _fb_query_media_batch(db_path, patient_ids[mid:], logger))


# ---------------------------------------------------------------------------
# Row → patient / media dict builders
# ---------------------------------------------------------------------------

def _g(row: dict, *keys) -> Optional[str]:
    """First non-empty value from a row (rows have UPPER-CASE keys)."""
    for k in keys:
        v = row.get(k.upper())
        if v is not None:
            s = str(v).strip()
            if s:
                return s
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


def _parse_imo(imo_ini: dict):
    """Orientation / invert from a .imo sidecar. Operations apply in file order."""
    ops = imo_ini.get("imageoperations")
    if not ops:
        return None, False
    rot_steps   = 0
    mirrored    = False
    is_inverted = False
    for op_val in ops.values():
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
    orientation = {0: None, 1: "ROT90", 2: "ROT180", 3: "ROT270"}[rot_steps]
    if mirrored:
        orientation = f"MIRROR_{orientation}" if orientation else "MIRROR"
    return orientation, is_inverted


def _build_media_item(
    row: dict,
    xray_path: str,
    vid_path: str,
    encoding: str,
    dirs: _DirIndex,
    preview_cache: set,
) -> Optional[dict]:
    """
    Convert a single XRAYVIDEO/DCMIMG/STUDY joined row to the internal media dict.
    Returns None if ITYP is missing or unrecognised.
    """
    ityp_raw = _g(row, "ITYP")
    try:
        ityp = int(ityp_raw) if ityp_raw is not None else None
    except (ValueError, TypeError):
        ityp = None

    if ityp == 0:
        base_path = vid_path
    elif ityp == 1:
        base_path = xray_path
    else:
        return None

    orgfile   = _g(row, "ORGFILE")
    file_path = os.path.join(base_path, orgfile) if orgfile else None

    # One directory lookup tells us whether the image exists and its size
    real_path, file_size = (dirs.find(file_path) if file_path else (None, None))
    if real_path:
        file_path = real_path

    # ── Step 1: resolve from MODALITY column ─────────────────────────────
    modality = _g(row, "MODALITY") or ""
    if ityp == 0:
        image_class, media_type = _ITYP_0_CLASS
    else:
        image_class, media_type = _MODALITY_MAP.get(modality, _ITYP_1_DEFAULT)
    needs_sidecar = modality in _MODALITY_NEEDS_SIDECAR

    # ── Step 2: .im0 sidecar ─────────────────────────────────────────────
    manufacturer = serial_number = model_name = station_name = None
    imager_pixel_x = imager_pixel_y = None
    orientation, is_inverted = None, False

    if real_path:
        stem = os.path.splitext(real_path)[0]

        im0_path, _ = dirs.find(stem + ".im0")
        if im0_path:
            try:
                im0_ini = _read_ini_file(im0_path, encoding)
            except OSError:
                im0_ini = None
            if im0_ini:
                manufacturer  = _ini_get(im0_ini, "XRAYEMITTER",         "Manufacturer")
                serial_number = _ini_get(im0_ini, "RawImageCreationInfo", "SerialNr")
                model_name    = _ini_get(im0_ini, "CREATION",             "SOURCE")
                station_name  = _ini_get(im0_ini, "CREATION",             "MachineName")

                if needs_sidecar:
                    image_class, media_type = _resolve_image_class_from_sidecar(
                        im0_ini, image_class, media_type
                    )

                dpi_x_str = _ini_get(im0_ini, "CREATION", "InputDeviceDPI_X")
                dpi_y_str = _ini_get(im0_ini, "CREATION", "InputDeviceDPI_Y")
                if dpi_x_str and dpi_y_str:
                    try:
                        imager_pixel_x = float(dpi_x_str.replace(",", ".")) / 25.4
                        imager_pixel_y = float(dpi_y_str.replace(",", ".")) / 25.4
                    except ValueError:
                        pass

        # ── Step 3: .imo sidecar (orientation / invert) ──────────────────
        imo_path, _ = dirs.find(stem + ".imo")
        if imo_path:
            try:
                orientation, is_inverted = _parse_imo(_read_ini_file(imo_path, encoding))
            except OSError:
                pass

    # ── IDs / timestamps ─────────────────────────────────────────────────
    vorgnr     = _g(row, "VORGNR") or ""
    study_uid  = _g(row, "STUDYINSTANCEUID") or vorgnr
    series_uid = _g(row, "SERIESINSTANCEUID") or vorgnr
    sop_uid    = _g(row, "SOPINSTANCEUID") or ""
    acq_dt     = _parse_datetime(_g(row, "AUFNDATUM"), _g(row, "AUFNZEIT"))

    # ── Preview: use an already-cached one if it exists, else lazy ───────
    preview_path = None
    if real_path:
        cached = _preview_cache_path(real_path, vorgnr)
        if os.path.basename(cached).lower() in preview_cache:
            preview_path = cached
        else:
            # Previews written next to the image by earlier versions
            legacy, _ = dirs.find(real_path + ".preview.jpg")
            preview_path = legacy

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

    media = {
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
    if real_path:
        media["_fetch_preview"] = _make_preview_fetch(media)
    return media


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
            rows = _fb_query(fdb, "SELECT COUNT(*) AS CNT FROM PATIENT WHERE PNR <> '1'")
            count = rows[0].get("CNT", "?") if rows else "?"
            return True, f"Connected to DUERRDBSWIN.FDB. Found {count} patient(s)."
        except Exception as exc:
            from datasources.fb_client import run_diag
            return False, f"{exc}\n\n--- FbBridge diagnostics ---\n{run_diag()}"

    def reattach_fetch(self, patients: list) -> list:
        """
        Re-attach `_fetch_preview` after patients are restored from the
        migration store (resumed migrations), which only keeps JSON.
        Called by the engine before write_patients.
        """
        for patient in patients:
            for study in patient.get("studies", {}).values():
                for series in study.get("series", {}).values():
                    for media in series.get("media", []):
                        if media.get("file_path") and "_fetch_preview" not in media:
                            media["_fetch_preview"] = _make_preview_fetch(media)
        return patients

    def load(
        self,
        cancel_flag,
        max_parallelism: int = 4,
        progress_callback: Optional[Callable] = None,
    ) -> list:
        import time
        t0 = time.time()

        def _progress(pct: int, msg: str):
            if progress_callback:
                progress_callback(pct, 100, msg)

        media_path = self.get_config_value("MediaPath") or ""
        encoding   = self.get_config_value("Encoding") or "windows-1252"
        # All patients load regardless of media (needed for PMS matching);
        # filtering happens at migration time. Same as VistaSoft / SOPRO.
        skip_empty = False

        xray_path = self.get_config_value("XRayImgPath") or _get_xray_image_path(media_path) or ""
        vid_path  = self.get_config_value("VidImgPath")  or _get_video_image_path(media_path) or ""
        self.set_config_value("XRayImgPath", xray_path)
        self.set_config_value("VidImgPath",  vid_path)

        fdb = _get_fdb_path(media_path)
        if not fdb:
            raise FileNotFoundError(
                f"DUERRDBSWIN.FDB not found under {media_path}\\pr1\\database\\"
            )

        # ── 1. All patients in one query ─────────────────────────────────
        _progress(2, "Reading patients…")
        patients: list = []
        by_uid:   dict = {}
        for row in _fb_query(fdb, _PATIENTS_SQL):
            p = _build_patient(row)
            if p["uid"] and p["uid"] not in by_uid:
                by_uid[p["uid"]] = p
                patients.append(p)
        self.logger.info(f"DBSWin: {len(patients)} patients read in {time.time() - t0:.1f}s")

        # ── 2. Media rows, batched by patient, sequential ────────────────
        uids     = [p["uid"] for p in patients]
        batches  = [uids[i:i + _MEDIA_BATCH] for i in range(0, len(uids), _MEDIA_BATCH)]
        rows: list = []
        for n, batch in enumerate(batches, 1):
            if cancel_flag():
                return patients
            rows.extend(_fb_query_media_batch(fdb, batch, self.logger))
            _progress(5 + int(45 * n / max(len(batches), 1)),
                      f"Reading media records… ({len(rows):,} so far)")
        self.logger.info(f"DBSWin: {len(rows)} media rows read in {time.time() - t0:.1f}s")

        # ── 3. Sidecars + file checks in parallel (disk I/O only) ────────
        dirs = _DirIndex()
        try:
            preview_cache = {n.lower() for n in os.listdir(_preview_cache_dir())}
        except OSError:
            preview_cache = set()

        errors = [0]
        def _safe_build(row):
            if cancel_flag():
                return None
            try:
                return _build_media_item(row, xray_path, vid_path, encoding, dirs, preview_cache)
            except Exception as exc:
                errors[0] += 1
                self.logger.error(
                    f"Failed to load media {_g(row, 'VORGNR')} for patient {_g(row, 'PNR')}: {exc}"
                )
                return None

        workers = min(16, max(4, int(max_parallelism or 4) * 2))
        items: list = []
        total_rows = len(rows) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, item in enumerate(pool.map(_safe_build, rows, chunksize=64), 1):
                items.append(item)
                if i % 500 == 0:
                    _progress(50 + int(45 * i / total_rows), f"Reading image details… {i:,}/{len(rows):,}")
        if cancel_flag():
            return patients

        # ── 4. Assemble study → series → media in DB order ───────────────
        missing_files = 0
        for row, media_item in zip(rows, items):
            if media_item is None:
                continue
            patient = by_uid.get(_g(row, "PNR") or "")
            if patient is None:
                continue
            if media_item.get("size_bytes") is None:
                missing_files += 1
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

        if skip_empty:
            patients = [p for p in patients if p["studies"]]

        _progress(100, f"Loaded {len(patients):,} patients")
        self.logger.info(
            f"DBSWin load complete: {len(patients)} patients, "
            f"{sum(1 for i in items if i)} media, {missing_files} missing file(s), "
            f"{errors[0]} error(s) in {time.time() - t0:.1f}s"
        )
        return patients
