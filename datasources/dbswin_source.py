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

Images are converted on demand, not during load. DBSWin keeps the raw image and
re-applies its display settings (window, invert, filter, brightness, rotation)
each time it opens one; each media item carries `_fetch` (the image as DBSWin
shows it: DICOM for X-rays, JPEG for camera images) and `_fetch_preview`.
Results are cached under %PROGRAMDATA%\\ITInfinityMigrator\\ — nothing is
written into the practice's XrayImg / VidImg folders.

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
# Image conversion
#
# DBSWin stores the raw plate/sensor image in the .XTF (JPEG 2000, usually
# 8-bit with a square-root tone curve) and re-applies its display settings
# every time the image is opened. Those settings live in sidecar files:
#
#   .ima  [ImageAfterLoad]   DDIP8To16Square HValue=N   8-bit → 16-bit expansion
#   .imo  [ImageOperations]  filter / invert / rotate / mirror, in order
#         [ZoomGray]         Lower / Upper display window (16-bit values)
#         [Luminance]        Brightness / Contrast / Gamma (50 / 50 / 20 = neutral)
#   .im0  same sections as at acquisition — used when .imo lacks one
#
# Targets get the image as DBSWin shows it, as a DICOM (X-rays) or JPEG
# (camera images), built on demand and cached outside the practice folders:
#   %PROGRAMDATA%\ITInfinityMigrator\converted\dbswin\
#   %PROGRAMDATA%\ITInfinityMigrator\preview_cache\dbswin\
#
# The DBSWin sharpening filter (DDIP Sc_Filter_Image) is proprietary; it is
# approximated with a two-scale unsharp mask.
# ---------------------------------------------------------------------------

_RENDER_VERSION = "r5"   # bump to invalidate cached conversions / previews
_ZOOMGRAY_SCALE = 0.58     # DDIP8To16Square output → [ZoomGray] units

_IMPL_UID = "2.25.29861396240039232786060277192702024864"   # fixed implementation class UID

_SOP_CLASS = {
    "Intra": "1.2.840.10008.5.1.4.1.1.1.3",   # Digital Intra-Oral X-Ray — For Presentation
    "Pano":  "1.2.840.10008.5.1.4.1.1.1.1",   # Digital X-Ray — For Presentation
    "Ceph":  "1.2.840.10008.5.1.4.1.1.1.1",
}
_SOP_CLASS_SC = "1.2.840.10008.5.1.4.1.1.7"    # Secondary Capture
_CLASS_TO_DICOM_MODALITY = {"Intra": "IO", "Pano": "PX", "Ceph": "DX", "Dvt": "CT", "Snapshot": "XC"}


def _cache_dir(kind: str) -> str:
    base = os.environ.get("PROGRAMDATA") or tempfile.gettempdir()
    d = os.path.join(base, "ITInfinityMigrator", kind, "dbswin")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(file_path: str, media_uid: str, bake: bool) -> str:
    key = media_uid or hashlib.md5(file_path.lower().encode("utf-8", "replace")).hexdigest()
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)}.{_RENDER_VERSION}{'' if bake else '.raw'}"


def _preview_cache_path(file_path: str, media_uid: str, bake: bool = True) -> str:
    return os.path.join(_cache_dir("preview_cache"), _cache_key(file_path, media_uid, bake) + ".jpg")


def _write_atomic(path: str, data: bytes):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _to_uid(value: str) -> str:
    """Return a valid DICOM UID: the value itself if valid, else a stable 2.25 UID."""
    v = (value or "").strip()
    if v and len(v) <= 64 and re.fullmatch(r"[0-9]+(\.[0-9]+)+", v) and not re.search(r"\.0[0-9]", v):
        return v
    seed = v or os.urandom(16).hex()
    return "2.25." + str(int(hashlib.md5(seed.encode("utf-8", "replace")).hexdigest(), 16))


def _find_sidecar(stem: str, ext: str) -> Optional[str]:
    p = stem + ext
    if os.path.isfile(p):
        return p
    folder, base = os.path.split(stem)
    want = (base + ext).lower()
    try:
        for n in os.listdir(folder or "."):
            if n.lower() == want:
                return os.path.join(folder, n)
    except OSError:
        pass
    return None


def _load_display_settings(src_path: str, encoding: Optional[str]) -> dict:
    """Read DBSWin's display settings for an image from its .imo / .im0 / .ima."""
    stem = os.path.splitext(src_path)[0]

    def _read(ext):
        p = _find_sidecar(stem, ext)
        if not p:
            return {}
        try:
            return _read_ini_file(p, encoding)
        except OSError:
            return {}

    imo, im0, ima = _read(".imo"), _read(".im0"), _read(".ima")

    def _section(name):
        return imo.get(name) or im0.get(name) or {}

    ops = list(_section("imageoperations").values())

    def _num(sec, key, default):
        try:
            return float(str(sec.get(key, default)).replace(",", "."))
        except ValueError:
            return float(default)

    zoom = _section("zoomgray")
    lum  = _section("luminance")

    hvalue = None
    for op in (ima.get("imageafterload") or {}).values():
        if "DDIP8To16Square" in op:
            m = re.search(r"HValue=(\d+)", op)
            hvalue = int(m.group(1)) if m else 65535

    return {
        "ops":        ops,
        "lower":      _num(zoom, "lower", -1) if zoom else None,
        "upper":      _num(zoom, "upper", -1) if zoom else None,
        "brightness": _num(lum, "brightness", 50),
        "contrast":   _num(lum, "contrast", 50),
        "gamma":      _num(lum, "gamma", 20),
        "hvalue":     hvalue,
    }


def _gauss_blur(x, sigma: float):
    import numpy as np
    if sigma < 0.3:
        return x
    if sigma >= 6:
        # Wide blur: do it at reduced resolution and scale back up — ~10x
        # faster, and the result is smooth anyway (mean difference in the
        # final image < 0.5 grey levels).
        from PIL import Image
        f = int(sigma / 3)
        h, w = x.shape
        small = np.asarray(Image.fromarray(np.ascontiguousarray(x, dtype=np.float32), mode="F")
                           .resize((max(1, w // f), max(1, h // f)), Image.BOX))
        sb = _gauss_blur(small, sigma / f)
        return np.asarray(Image.fromarray(np.ascontiguousarray(sb, dtype=np.float32), mode="F")
                          .resize((w, h), Image.BILINEAR))
    r = max(1, int(3 * sigma + 0.5))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2).astype(np.float32)
    k /= k.sum()
    p = np.pad(x, ((r, r), (r, r)), mode="reflect")
    # Separable convolution via cumulative windows of shifted copies
    rows = sum(k[i] * p[:, i:i + x.shape[1]] for i in range(2 * r + 1))
    return sum(k[i] * rows[i:i + x.shape[0], :] for i in range(2 * r + 1))


def _apply_geometry(arr, ops):
    """Rotate / mirror in the order DBSWin recorded them."""
    import numpy as np
    for op in ops:
        v = op.strip()
        if v.startswith("Rotate"):
            m = re.search(r"ax10=(\d+)", v)
            ang = int(m.group(1)) // 10 if m else 0
            k = (ang // 90) % 4             # ax10=900 → 90° anticlockwise
            if k:
                arr = np.rot90(arr, k)
        elif v.startswith("Orient"):
            if "or=1" in v:
                arr = arr[:, ::-1]           # mirror left/right
            elif "or=2" in v:
                arr = arr[::-1, :]           # mirror top/bottom
    return np.ascontiguousarray(arr)


def _open_image(src_path: str, max_size: Optional[int]):
    """Open an image, decoding JPEG 2000 / JPEG at reduced size when only a preview is needed."""
    from PIL import Image
    img = Image.open(src_path)
    scale = 1
    if max_size:
        try:
            if getattr(img, "format", "") == "JPEG2000":
                w, h = img.size
                r = 0
                while r < 5 and (max(w, h) >> (r + 1)) >= max_size:
                    r += 1
                if r:
                    img.reduce = r
                    scale = 1 << r
            elif getattr(img, "format", "") == "JPEG":
                img.draft(img.mode, (max_size, max_size))
            img.load()
        except Exception:
            img = Image.open(src_path)
            scale = 1
    img.load()
    return img, scale


def _ddip_param(op: str, key: str, default: float) -> float:
    m = re.search(r"\b" + key + r"=(-?[\d.]+)", op)
    try:
        return float(m.group(1)) if m else default
    except ValueError:
        return default


def _ddip_filter(v, op: str, scale: float):
    """
    DBSWin's Gauss_FD2 frequency filter, driven by its recorded parameters, e.g.
        Type=Gauss_FD2 al1=1.00 al0=0.0 ftp=10.0 ah0=0.5 ah1=1.00 fhp=0.70
    Calibrated against a DBSWin screenshot of the same image: it behaves as a
    local-contrast boost of gain al1 at Gaussian radius ftp px (matched both
    the tones and the grain of DBSWin's view). ah1 is treated as the gain for
    fine detail above the fhp cut-off (1.0 = unchanged).
    """
    al1 = _ddip_param(op, "al1", 1.0)
    ftp = _ddip_param(op, "ftp", 10.0)
    ah1 = _ddip_param(op, "ah1", 1.0)
    fhp = _ddip_param(op, "fhp", 0.7)
    if al1:
        v = v + al1 * (v - _gauss_blur(v, max(ftp, 0.5) / scale))
    if ah1 != 1.0:
        sigma = max(1.0 / max(fhp, 0.05), 0.5) / scale
        base = _gauss_blur(v, sigma)
        v = base + ah1 * (v - base)
    return v


def _render_xray(src_path: str, disp: dict, bake: bool, max_size: Optional[int] = None):
    """
    Render a DBSWin X-ray as DBSWin displays it. Returns a float32 array in 0..1
    (0 = black). With bake=False only the tone expansion is applied.
    """
    import numpy as np
    img, scale = _open_image(src_path, max_size)
    with img:
        if img.mode not in ("L", "I;16", "I;16L", "I;16B", "I", "F"):
            img = img.convert("L")
        a = np.asarray(img).astype(np.float32)
        eight_bit = img.mode == "L"

    # 1. Tone expansion (8-bit square-root encoded → 16-bit linear)
    h = disp.get("hvalue")
    if eight_bit and h:
        v = (a / 255.0) ** 2 * h
        full = float(h)
    elif eight_bit:
        v = a * 257.0
        full = 65535.0
    else:
        v = a
        full = float(v.max() or 65535)

    if not bake:
        x = np.clip(v / full, 0, 1)
        return x

    # 2. Image operations in recorded order (filter now; invert after windowing)
    invert = False
    geometry = []
    for op in disp.get("ops", []):
        s = op.strip()
        if s.startswith("Invert"):
            invert = not invert
        elif s.startswith("Rotate") or s.startswith("Orient"):
            geometry.append(s)
        elif "Sc_Filter_Image" in s:
            v = _ddip_filter(v, s, scale)

    # 3. Display window ([ZoomGray] Lower/Upper, in 16-bit units)
    lo, hi = disp.get("lower"), disp.get("upper")
    if lo is not None and hi is not None and hi > lo and eight_bit and h:
        # [ZoomGray] is in DBSWin's internal 16-bit scale, which is ~0.58 x
        # the DDIP8To16Square output (calibrated on a DBSWin screenshot; the
        # same factor also explained a second image). Without it the window
        # was too narrow and crushed the shadows to black.
        v = v * _ZOOMGRAY_SCALE
    if lo is None or hi is None or hi <= lo:
        lo, hi = (float(p) for p in np.percentile(v, [0.5, 99.5]))
        if hi <= lo:
            hi = lo + 1
    elif eight_bit and not h and hi <= 255:
        lo, hi = lo * 257.0, hi * 257.0
    x = np.clip((v - lo) / (hi - lo), 0, 1)

    if invert:
        x = 1.0 - x

    # 4. Brightness / contrast / gamma (DBSWin neutral = 50 / 50 / 20). The
    #    display is linear in the windowed values (no display gamma); small
    #    slider moves have a small effect (Brightness 45 / Gamma 19 were barely
    #    measurable against DBSWin's view).
    c = 1.0 + (disp.get("contrast", 50) - 50) / 50.0
    b = (disp.get("brightness", 50) - 50) / 250.0
    x = np.clip((x - 0.5) * c + 0.5 + b, 0, 1)
    g = disp.get("gamma", 20) or 20
    if g != 20:
        x = np.power(x, g / 20.0)

    # 5. Rotation / mirroring
    if geometry:
        x = _apply_geometry(x, geometry)
    return x.astype(np.float32)


def _render_photo(src_path: str, disp: dict, bake: bool, max_size: Optional[int] = None):
    """Camera image as an 8-bit RGB / L PIL image, with DBSWin rotation applied."""
    import numpy as np
    from PIL import Image
    img, _ = _open_image(src_path, max_size)
    with img:
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        arr = np.asarray(img)
    if bake:
        geometry = [o for o in disp.get("ops", []) if o.strip().startswith(("Rotate", "Orient"))]
        if geometry:
            arr = _apply_geometry(arr, geometry)
    return Image.fromarray(arr)


def _dicom_8bit(pixels, meta: dict) -> bytes:
    """
    Minimal MONOCHROME2 DICOM (Explicit VR Little Endian, uncompressed).
    `pixels` is a 2-D uint16 array holding 12-bit values (0..4095) — the
    layout intraoral sensors and DTX itself write — or a uint8 array.
    `meta` carries patient / study / image values.
    (8-bit X-rays displayed in DTX Studio Home, but went black there after
    being opened in DTX Studio Clinic / Diagnose.)
    """
    import struct
    rows, cols = pixels.shape

    def el(group, elem, vr, value: bytes) -> bytes:
        if len(value) % 2:
            value += b"\x00" if vr in ("UI", "OB", "OW", "UN") else b" "
        head = struct.pack("<HH", group, elem) + vr.encode("ascii")
        if vr in ("OB", "OW", "SQ", "UN", "UT", "UC", "UR"):
            return head + b"\x00\x00" + struct.pack("<I", len(value)) + value
        return head + struct.pack("<H", len(value)) + value

    def txt(s, vr="LO", limit=64) -> bytes:
        return str(s or "")[:limit].encode("latin-1", "replace")

    def us(n) -> bytes:
        return struct.pack("<H", int(n))

    wide = pixels.dtype.itemsize > 1
    if wide:
        pixels = pixels.astype("<u2", copy=False)
    bits_alloc, bits_stored = (16, 12) if wide else (8, 8)
    wc, ww = (b"2047.5", b"4096") if wide else (b"127.5", b"256")

    sop_class = meta["sop_class"]
    sop_inst  = meta["sop_instance"]
    ts        = "1.2.840.10008.1.2.1"

    group2 = (el(0x0002, 0x0001, "OB", b"\x00\x01")
              + el(0x0002, 0x0002, "UI", sop_class.encode())
              + el(0x0002, 0x0003, "UI", sop_inst.encode())
              + el(0x0002, 0x0010, "UI", ts.encode())
              + el(0x0002, 0x0012, "UI", _IMPL_UID.encode())
              + el(0x0002, 0x0013, "SH", b"ITINF_MIGRATOR"))
    meta_hdr = el(0x0002, 0x0000, "UL", struct.pack("<I", len(group2))) + group2

    ps = meta.get("pixel_spacing_mm")
    ps_str = f"{ps:.6f}\\{ps:.6f}".encode() if ps else None
    ds = b"".join([
        el(0x0008, 0x0005, "CS", b"ISO_IR 100"),
        el(0x0008, 0x0008, "CS", b"DERIVED\\PRIMARY"),
        el(0x0008, 0x0016, "UI", sop_class.encode()),
        el(0x0008, 0x0018, "UI", sop_inst.encode()),
        el(0x0008, 0x0020, "DA", txt(meta.get("date"), limit=8)),
        el(0x0008, 0x0022, "DA", txt(meta.get("date"), limit=8)),
        el(0x0008, 0x0023, "DA", txt(meta.get("date"), limit=8)),
        el(0x0008, 0x0030, "TM", txt(meta.get("time"), limit=6)),
        el(0x0008, 0x0032, "TM", txt(meta.get("time"), limit=6)),
        el(0x0008, 0x0033, "TM", txt(meta.get("time"), limit=6)),
        el(0x0008, 0x0060, "CS", txt(meta.get("modality"), limit=16)),
        el(0x0008, 0x0068, "CS", b"FOR PRESENTATION"),
        el(0x0008, 0x0070, "LO", txt(meta.get("manufacturer"))),
        el(0x0008, 0x2111, "ST", b"DBSWin display settings applied by IT INFINITY Migration Tool"),
        el(0x0010, 0x0010, "PN", txt(meta.get("patient_name"))),
        el(0x0010, 0x0020, "LO", txt(meta.get("patient_id"))),
        el(0x0010, 0x0030, "DA", txt(meta.get("patient_dob"), limit=8)),
        el(0x0010, 0x0040, "CS", txt(meta.get("patient_sex"), limit=16)),
        el(0x0018, 0x1164, "DS", ps_str) if ps_str else b"",
        el(0x0020, 0x000D, "UI", meta["study_uid"].encode()),
        el(0x0020, 0x000E, "UI", meta["series_uid"].encode()),
        el(0x0020, 0x0013, "IS", txt(meta.get("instance_number") or "1", limit=12)),
        el(0x0020, 0x0020, "CS", b""),
        el(0x0028, 0x0002, "US", us(1)),
        el(0x0028, 0x0004, "CS", b"MONOCHROME2"),
        el(0x0028, 0x0010, "US", us(rows)),
        el(0x0028, 0x0011, "US", us(cols)),
        el(0x0028, 0x0030, "DS", ps_str) if ps_str else b"",
        el(0x0028, 0x0100, "US", us(bits_alloc)),
        el(0x0028, 0x0101, "US", us(bits_stored)),
        el(0x0028, 0x0102, "US", us(bits_stored - 1)),
        el(0x0028, 0x0103, "US", us(0)),
        el(0x0028, 0x1040, "CS", b"LIN"),
        # DX image module (type 1): higher values = less X-ray (bone white)
        el(0x0028, 0x1041, "SS", struct.pack("<h", -1)),
        el(0x0028, 0x1050, "DS", wc),
        el(0x0028, 0x1051, "DS", ww),
        el(0x0028, 0x2110, "CS", b"01"),
        el(0x2050, 0x0020, "CS", b"IDENTITY"),
        el(0x7FE0, 0x0010, "OW" if wide else "OB", pixels.tobytes()),
    ])
    return b"\x00" * 128 + b"DICM" + meta_hdr + ds


def _dicom_meta(media: dict, rows_cols_scale: float = 1.0) -> dict:
    """Header values for the converted DICOM, from the media / patient info."""
    tags = media.get("dicom_tags") or {}
    pat  = media.get("_patient_info") or {}
    acq  = media.get("acq_datetime") or ""
    date = acq[:10].replace("-", "") if acq else ""
    time_ = acq[11:19].replace(":", "") if len(acq) >= 19 else ""
    sex = {"MALE": "M", "FEMALE": "F", "OTHER": "O"}.get((pat.get("sex") or "").upper(), "")
    ps = media.get("_pixel_spacing_mm")
    if not ps:   # resumed runs lose underscore keys — recover from the tag
        try:
            ps = float(str(tags.get("ImagerPixelSpacing") or "").split("\\")[0]) or None
        except ValueError:
            ps = None
    return {
        "sop_class":       _SOP_CLASS.get(media.get("image_class"), _SOP_CLASS_SC),
        "sop_instance":    tags.get("SOPInstanceUID") or _to_uid(media.get("uid", "")),
        "study_uid":       tags.get("StudyInstanceUID") or _to_uid("study-" + media.get("uid", "")),
        "series_uid":      tags.get("SeriesInstanceUID") or _to_uid("series-" + media.get("uid", "")),
        "instance_number": tags.get("InstanceNumber") or "1",
        "modality":        _CLASS_TO_DICOM_MODALITY.get(media.get("image_class"), "OT"),
        "manufacturer":    tags.get("Manufacturer") or "",
        "date": date, "time": time_,
        "patient_name":    "^".join(x for x in (pat.get("family_name"), pat.get("given_names")) if x),
        "patient_id":      pat.get("id") or "",
        "patient_dob":     (pat.get("dob") or "").replace("-", ""),
        "patient_sex":     sex,
        "pixel_spacing_mm": ps * rows_cols_scale if ps else None,
    }


def _converted_bytes(media: dict, bake: bool, encoding: Optional[str]) -> Optional[bytes]:
    """The file a target should receive: DICOM for X-rays, JPEG for camera images."""
    import io
    import numpy as np
    src = media.get("file_path")
    if not src or not os.path.isfile(src):
        return None
    is_photo = media.get("content_type") == "image/jpeg"
    # Not cached on disk: a full practice is tens of GB of converted images,
    # and writing them cost more time than re-rendering a retried image.
    disp = _load_display_settings(src, encoding)
    if is_photo:
        img = _render_photo(src, disp, bake)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95)
        data = buf.getvalue()
    else:
        x = _render_xray(src, disp, bake)
        pixels = (np.clip(x, 0, 1) * 4095.0 + 0.5).astype(np.uint16)
        data = _dicom_8bit(pixels, _dicom_meta(media))
    return data


def _preview_bytes(media: dict, bake: bool, encoding: Optional[str]) -> Optional[bytes]:
    """Small JPEG matching the converted image."""
    import io
    import numpy as np
    from PIL import Image
    src = media.get("file_path")
    if not src or not os.path.isfile(src):
        return None
    dest = _preview_cache_path(src, media.get("uid", ""), bake)
    if os.path.isfile(dest):
        with open(dest, "rb") as f:
            return f.read()
    disp = _load_display_settings(src, encoding)
    if media.get("content_type") == "image/jpeg":
        img = _render_photo(src, disp, bake, max_size=_PREVIEW_MAX)
    else:
        x = _render_xray(src, disp, bake, max_size=_PREVIEW_MAX)
        img = Image.fromarray((np.clip(x, 0, 1) * 255.0 + 0.5).astype(np.uint8))
    img.thumbnail((_PREVIEW_MAX, _PREVIEW_MAX))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    data = buf.getvalue()
    _write_atomic(dest, data)
    return data


def _attach_fetchers(media: dict, patient: Optional[dict], bake: bool, encoding: Optional[str]):
    """
    Give a media item its on-demand callables:
      _fetch          converted file for upload (DICOM / JPEG)
      _fetch_preview  matching JPEG preview; also fills in preview_path
    """
    if patient is not None:
        media["_patient_info"] = {k: patient.get(k) for k in
                                  ("family_name", "given_names", "id", "dob", "sex")}
    if not media.get("file_path"):
        return

    def _fetch():
        return _converted_bytes(media, bake, encoding)

    def _fetch_preview():
        p = media.get("preview_path")
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        data = _preview_bytes(media, bake, encoding)
        if data:
            media["preview_path"] = _preview_cache_path(media["file_path"], media.get("uid", ""), bake)
        return data

    media["_fetch"] = _fetch
    media["_fetch_preview"] = _fetch_preview


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
        "birth_date":    _parse_date(_g(row, "GDATE")),   # standard key used by targets
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
    bake: bool = True,
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
    imager_dpi = None
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

                # Pixel size: 25.4 mm / dots-per-inch (previously inverted)
                dpi_x_str = _ini_get(im0_ini, "CREATION", "InputDeviceDPI_X")
                if dpi_x_str:
                    try:
                        imager_dpi = float(dpi_x_str.replace(",", ".")) or None
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

    # ── Preview: use an already-converted one if it exists, else lazy ────
    # (".preview.jpg" files left next to images by earlier versions show the
    #  raw image and are deliberately ignored.)
    preview_path = None
    if real_path:
        cached = _preview_cache_path(real_path, vorgnr, bake)
        if os.path.basename(cached).lower() in preview_cache:
            preview_path = cached

    # Valid DICOM UIDs (DBSWin falls back to the hex VORGNR when DCMIMG has none)
    study_tag_uid  = _to_uid(_g(row, "STUDYINSTANCEUID") or ("study-" + vorgnr))
    series_tag_uid = _to_uid(_g(row, "SERIESINSTANCEUID") or ("series-" + vorgnr))
    sop_uid        = _to_uid(sop_uid or vorgnr)
    is_photo       = ityp == 0
    pixel_mm       = (25.4 / imager_dpi) if imager_dpi else None

    dicom_tags = {
        "StudyInstanceUID":        study_tag_uid,
        "SeriesInstanceUID":       series_tag_uid,
        "SOPInstanceUID":          sop_uid,
        # What the converted file actually is (DBSWin often stores "OT")
        "Modality":                _CLASS_TO_DICOM_MODALITY.get(image_class, "OT"),
        "SOPClassUID":             (None if is_photo else _SOP_CLASS.get(image_class, _SOP_CLASS_SC)),
        "PhotometricInterpretation": "RGB" if is_photo else "MONOCHROME2",
        "SamplesPerPixel":         3 if is_photo else 1,
        "BitsAllocated":           8 if is_photo else 16,
        "BitsStored":              8 if is_photo else 12,
        "HighBit":                 7 if is_photo else 11,
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
        "ImagerPixelSpacing":      (f"{pixel_mm:.6f}\\{pixel_mm:.6f}" if pixel_mm else ""),
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
        "content_type": "image/jpeg" if is_photo else "application/dicom",
        "preview_path": preview_path,
        "size_bytes":   file_size,
        "acq_datetime": acq_dt,
        "orientation":  orientation,
        "is_inverted":  is_inverted,
        "dicom_tags":   dicom_tags,
        "comments":     _g(row, "SERIESDESCRIPTION") or "",
        "_pixel_spacing_mm": pixel_mm,
    }
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
                key="BakeDisplaySettings",
                name="Apply DBSWin Image Settings",
                description=(
                    "Migrate X-rays as DBSWin displays them: window, invert, filter, "
                    "brightness/contrast and rotation from the .imo/.im0 files. "
                    "Off sends the unprocessed image."
                ),
                value="On",
                advanced=True,
                config_type=ConfigurationType.SWITCH,
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

    def _bake(self) -> bool:
        return (self.get_config_value("BakeDisplaySettings") or "On").strip().lower() in ("on", "true", "1")

    def reattach_fetch(self, patients: list) -> list:
        """
        Re-attach `_fetch` / `_fetch_preview` after patients are restored from
        the migration store (resumed migrations), which only keeps JSON.
        Called by the engine before write_patients.
        """
        bake     = self._bake()
        encoding = self.get_config_value("Encoding") or "windows-1252"
        for patient in patients:
            for study in patient.get("studies", {}).values():
                for series in study.get("series", {}).values():
                    for media in series.get("media", []):
                        if "_fetch" not in media:
                            _attach_fetchers(media, patient, bake, encoding)
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
        bake = self._bake()
        try:
            preview_cache = {n.lower() for n in os.listdir(_cache_dir("preview_cache"))}
        except OSError:
            preview_cache = set()

        errors = [0]
        def _safe_build(row):
            if cancel_flag():
                return None
            try:
                return _build_media_item(row, xray_path, vid_path, encoding, dirs, preview_cache, bake)
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
            _attach_fetchers(media_item, patient, bake, encoding)
            study = patient["studies"].setdefault(study_uid, {
                "uid":            study_uid,
                "study_instance": study_uid,
                # ISO date-time (FbBridge returns dates in the server's locale)
                "study_datetime": (_parse_datetime(media_item["dicom_tags"].get("StudyDate"),
                                                   media_item["dicom_tags"].get("StudyTime"))
                                   or media_item.get("acq_datetime", "")),
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
