"""DTX Studio — SOURCE datasource. Reads patients/media from DTX Studio Core REST API."""
import sys
import os
import concurrent.futures
import json
import base64
import ssl
import logging
from typing import Callable, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from core.base_datasource import BaseDatasource
from core.models import ConfigurationItem, ConfigurationType

MIN_CORE_VERSION = (3, 9)

COMMON_ENCODINGS = {
    "utf-8":   "UTF-8, Codepage:65001",
    "utf-16":  "UTF-16, Codepage:1200",
    "latin-1": "Latin-1 / ISO-8859-1, Codepage:28591",
    "cp1252":  "Windows-1252, Codepage:1252",
    "ascii":   "ASCII, Codepage:20127",
}

PATIENT_ID_OPTIONS = {
    "pms":    "PMS Patient ID only",
    "pms_cl": "PMS Patient ID and Clinic Patient ID",
}


# ---------------------------------------------------------------------------
# CoreClient — mirrors all C# extension methods exactly
# ---------------------------------------------------------------------------

class CoreClient:
    """
    HTTP client for the DTX Studio Core REST API.
    All paths use the dw-endpoint/api/ base, matching the C# extension methods.
    """

    BASE = "dw-endpoint/api"

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header = f"Basic {credentials}"
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
        self.logger = logging.getLogger("CoreClient")

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.BASE}/{path.lstrip('/')}"

    def _headers(self, extra: dict = None) -> dict:
        import socket
        h = {
            "Authorization":       self._auth_header,
            "Accept":              "application/json",
            # Required by DTX Studio Core to identify the calling application.
            # Without these the server returns 403 "Forbidden app".
            "x-user-agent":        "dtx-core",
            "x-dtxcore-username":  "ImagingMigrator",
            "x-dtxcore-machineid": socket.gethostname(),
        }
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, url: str, body: bytes = None, content_type: str = None) -> bytes:
        headers = self._headers()
        if content_type:
            headers["Content-Type"] = content_type
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30, context=self._ssl_ctx) as resp:
                return resp.read()
        except HTTPError as exc:
            body_snippet = ""
            try:
                body_snippet = exc.read().decode(errors="replace")[:300]
            except Exception:
                pass
            raise ConnectionError(
                f"HTTP {exc.code} {exc.reason} from {url}"
                + (f": {body_snippet}" if body_snippet else "")
            ) from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, Exception):
                reason = f"{type(reason).__name__}: {reason}"
            raise ConnectionError(f"Cannot reach {url}: {reason}") from exc
        except Exception as exc:
            raise ConnectionError(f"[{type(exc).__name__}] {exc}") from exc

    def _get(self, path: str) -> any:
        raw = self._request("GET", self._url(path))
        return json.loads(raw) if raw else None

    def _post(self, path: str, payload: dict) -> any:
        body = json.dumps(payload).encode()
        raw = self._request("POST", self._url(path), body=body, content_type="application/json")
        return json.loads(raw) if raw else None

    def _put_json(self, path: str, payload: dict) -> any:
        body = json.dumps(payload).encode()
        raw = self._request("PUT", self._url(path), body=body, content_type="application/json")
        return json.loads(raw) if raw else None

    def _put_stream(self, path: str, stream, content_type: str) -> bytes:
        data = stream.read() if hasattr(stream, "read") else stream
        raw = self._request("PUT", self._url(path), body=data, content_type=content_type)
        return raw or b""

    def _patch(self, path: str, payload: dict):
        body = json.dumps(payload).encode()
        self._request("PATCH", self._url(path), body=body, content_type="application/json")

    # ── Info / Practice ───────────────────────────────────────────

    def get_core_info(self) -> dict:
        """GET dw-endpoint/api/info"""
        return self._get("info")

    def get_practice(self) -> dict:
        """GET dw-endpoint/api/practice"""
        return self._get("practice")

    # ── Patients ──────────────────────────────────────────────────

    def get_patients(self) -> list:
        """GET dw-endpoint/api/patients?api-resource-version=1"""
        return self._get("patients?api-resource-version=1")

    def create_patient(self, patient: dict) -> dict:
        """POST dw-endpoint/api/patients/?api-resource-version=1"""
        # Remove None values — DTX Studio API rejects null fields
        payload = {k: v for k, v in patient.items() if v is not None}
        return self._post("patients/?api-resource-version=1", payload)

    def search_patients_by_pms_id(self, patient_id: str) -> list:
        """GET dw-endpoint/api/patients?api-resource-version=2&search.patientPmsId=...&sort.onLastCapturedDate=DESC"""
        return self._get(f"patients?api-resource-version=2&search.patientPmsId={patient_id}&sort.onLastCapturedDate=DESC")

    def search_patients_by_clinic_id(self, patient_id: str) -> list:
        """GET dw-endpoint/api/patients?api-resource-version=2&search.patientDicomId=...&sort.onLastCapturedDate=DESC"""
        return self._get(f"patients?api-resource-version=2&search.patientDicomId={patient_id}&sort.onLastCapturedDate=DESC")

    def search_patients_by_reference_id(self, patient_id: str) -> list:
        """GET dw-endpoint/api/patients?api-resource-version=2&search.patientReferenceId=...&sort.onLastCapturedDate=DESC"""
        return self._get(f"patients?api-resource-version=2&search.patientReferenceId={patient_id}&sort.onLastCapturedDate=DESC")

    # ── Patient media groups ──────────────────────────────────────

    def get_patient_media_groups(self, patient_id: str) -> list:
        """GET dw-endpoint/api/patients/{patientId}/media-groups"""
        return self._get(f"patients/{patient_id}/media-groups")

    def create_patient_media_group(self, patient_id: str, request: dict) -> dict:
        """POST dw-endpoint/api/patients/{patientId}/media-groups"""
        return self._post(f"patients/{patient_id}/media-groups", request)

    # ── Studies ───────────────────────────────────────────────────

    def get_patient_studies(self, patient_id: str) -> list:
        """GET dw-endpoint/api/patients/{patientId}/studies"""
        return self._get(f"patients/{patient_id}/studies")

    def create_patient_study(self, patient_id: str, study_request: dict) -> dict:
        """POST dw-endpoint/api/patients/{patientId}/studies"""
        return self._post(f"patients/{patient_id}/studies", study_request)

    # ── Series ────────────────────────────────────────────────────

    def get_study_series(self, study_id: str) -> list:
        """GET dw-endpoint/api/studies/{studyId}/series"""
        return self._get(f"studies/{study_id}/series")

    def create_study_series(self, study_id: str, series_request: dict) -> dict:
        """POST dw-endpoint/api/studies/{studyId}/series"""
        return self._post(f"studies/{study_id}/series", series_request)

    def create_study_series_with_dicom_tags(self, study_id: str, series_request: dict) -> dict:
        """POST dw-endpoint/api/studies/{studyId}/series?api-resource-version=1"""
        return self._post(f"studies/{study_id}/series?api-resource-version=1", series_request)

    def get_series_dicom_tags(self, series_id: str) -> dict:
        """GET dw-endpoint/api/series/{seriesId}/dicomtags"""
        return self._get(f"series/{series_id}/dicomtags")

    def create_series_dicom_tags(self, series_id: str, dicom_tags: dict) -> dict:
        """POST dw-endpoint/api/series/{seriesId}/dicomtags"""
        return self._post(f"series/{series_id}/dicomtags", dicom_tags)

    # ── Media ─────────────────────────────────────────────────────

    def get_patient_media(self, patient_id: str) -> list:
        """GET dw-endpoint/api/patients/{patientId}/media?api-resource-version=2"""
        return self._get(f"patients/{patient_id}/media?api-resource-version=2")

    def get_series_media(self, series_id: str) -> list:
        """GET dw-endpoint/api/series/{seriesId}/media?api-resource-version=2"""
        return self._get(f"series/{series_id}/media?api-resource-version=2")

    def create_series_media(self, series_id: str, media_request: dict) -> dict:
        """POST dw-endpoint/api/series/{seriesId}/media?api-resource-version=2"""
        return self._post(f"series/{series_id}/media?api-resource-version=2", media_request)

    def create_series_media_with_dicom_tags(self, series_id: str, media_request: dict) -> dict:
        """POST dw-endpoint/api/series/{seriesId}/media?api-resource-version=3"""
        return self._post(f"series/{series_id}/media?api-resource-version=3", media_request)

    def add_media_to_group(self, media_id: str, request: dict):
        """PATCH dw-endpoint/api/media/{mediaId}?api-resource-version=1&operation=assign-to-group"""
        self._patch(f"media/{media_id}?api-resource-version=1&operation=assign-to-group", request)

    # ── Media DICOM tags ──────────────────────────────────────────

    def get_media_dicom_tags(self, media_id: str) -> dict:
        """GET dw-endpoint/api/media/{mediaId}/dicomtags"""
        return self._get(f"media/{media_id}/dicomtags")

    def create_media_dicom_tags(self, media_id: str, dicom_tags: dict) -> dict:
        """PUT dw-endpoint/api/media/{mediaId}/dicomtags"""
        return self._put_json(f"media/{media_id}/dicomtags", dicom_tags)

    # ── Media files ───────────────────────────────────────────────

    def get_media_files(self, media_id: str) -> list:
        """GET dw-endpoint/api/media/{mediaId}/files"""
        return self._get(f"media/{media_id}/files")

    def create_media_file(self, media_id: str, request: dict) -> dict:
        """POST dw-endpoint/api/media/{mediaId}/files"""
        return self._post(f"media/{media_id}/files", request)

    # ── Media binary data ─────────────────────────────────────────

    def get_media_data(self, media_id: str) -> bytes:
        """GET dw-endpoint/api/media/{mediaId}/data"""
        return self._request("GET", self._url(f"media/{media_id}/data"))

    def put_media_data(self, media_id: str, stream, content_type: str) -> bytes:
        """PUT dw-endpoint/api/media/{mediaId}/data"""
        return self._put_stream(f"media/{media_id}/data", stream, content_type)

    def get_media_file_data(self, media_id: str, file_id: str) -> bytes:
        """GET dw-endpoint/api/media/{mediaId}/files/{fileId}/data"""
        return self._request("GET", self._url(f"media/{media_id}/files/{file_id}/data"))

    def put_media_file_data(self, media_id: str, file_id: str, stream, content_type: str):
        """PUT dw-endpoint/api/media/{mediaId}/files/{fileId}/data"""
        self._put_stream(f"media/{media_id}/files/{file_id}/data", stream, content_type)

    def get_media_preview_data(self, media_id: str) -> bytes:
        """GET dw-endpoint/api/media/{mediaId}/preview/data"""
        return self._request("GET", self._url(f"media/{media_id}/preview/data"))

    def put_media_preview_data(self, media_id: str, stream, content_type: str):
        """PUT dw-endpoint/api/media/{mediaId}/preview/data"""
        self._put_stream(f"media/{media_id}/preview/data", stream, content_type)


# ---------------------------------------------------------------------------
# DTXStudioDatasource
# ---------------------------------------------------------------------------

def _find_file(path: str) -> str | None:
    """
    Find a file that may or may not have an extension.
    VistaSoft stores files without extensions in the DB path but
    the actual files on disk may have .jpg, .dcm, .vsx, etc.
    Returns the actual path if found, None otherwise.
    """
    if not path:
        return None
    if os.path.isfile(path):
        return path
    # Try common image extensions
    for ext in (".jpg", ".jpeg", ".dcm", ".png", ".bmp", ".tif", ".tiff",
                ".vsx", ".vsf", ".raw", ".bin", ".dat", ".img"):
        candidate = path + ext
        if os.path.isfile(candidate):
            return candidate
    # Try listing the parent directory for a file starting with the same name
    parent = os.path.dirname(path)
    base   = os.path.basename(path).lower()
    if os.path.isdir(parent):
        for f in os.listdir(parent):
            if f.lower().startswith(base):
                return os.path.join(parent, f)
    return None


def _map_media_type(image_class: str) -> str:
    """Map VistaSoft IMAGECLASS to DTX Studio mediaType."""
    c = (image_class or "").upper()
    if c in ("VOLUME", "3D", "CT", "CBCT"):
        return "VOLUME"
    if c in ("ATTACHMENT", "PDF", "DOC"):
        return "ATTACHMENT"
    return "IMAGE"  # default for 2D X-ray, photo, etc.


def _format_study_datetime(raw: str) -> str | None:
    """
    Normalise a Firebird STUDYDATETIME string to ISO-8601 for the DTX Studio API.

    Firebird can return datetimes in several formats:
      "2014-12-09 16:48:28"   → "2014-12-09T16:48:28Z"
      "2014-12-09T16:48:28"   → "2014-12-09T16:48:28Z"
      "20141209164828"        → "2014-12-09T16:48:28Z"
      "2014-12-09"            → "2014-12-09T00:00:00Z"
    Returns None if unparseable.
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip().replace(" ", "T")
    # Already ISO-ish with time component
    if "T" in s:
        if not s.endswith("Z"):
            s = s.rstrip("Z") + "Z"
        return s
    # Date only with dashes e.g. "2014-12-09"
    if "-" in s and len(s) >= 10:
        base = s[:10].rstrip("Z")
        return base + "T00:00:00Z"
    # Pure digits: YYYYMMDDHHMMSS or YYYYMMDD
    digits = s.replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
    if len(digits) >= 8:
        try:
            y, mo, d = digits[:4], digits[4:6], digits[6:8]
            h = digits[8:10] if len(digits) >= 10 else "00"
            mi = digits[10:12] if len(digits) >= 12 else "00"
            sc = digits[12:14] if len(digits) >= 14 else "00"
            int(y); int(mo); int(d)  # validate
            return f"{y}-{mo}-{d}T{h}:{mi}:{sc}Z"
        except (ValueError, TypeError):
            pass
    return None


def _parse_acq_datetime(sidecar: dict) -> str | None:
    """
    Build an ISO-8601 datetime string from the VistaSoft sidecar.
    Tries AcquisitionDate+AcquisitionTime first, then ContentDate+ContentTime,
    then SeriesDate+SeriesTime.  Returns None if nothing usable is found.

    VistaSoft stores dates as "YYYYMMDD" and times as "HHMMSS" or "HHMMSS.fff".
    """
    for date_key, time_key in [
        ("AcquisitionDate", "AcquisitionTime"),
        ("ContentDate",     "ContentTime"),
        ("SeriesDate",      "SeriesTime"),
    ]:
        d = sidecar.get(date_key, "") or ""
        t = sidecar.get(time_key, "") or ""
        d = d.strip().replace("-", "").replace("/", "")
        t = t.strip().split(".")[0]  # drop sub-second fraction
        if len(d) == 8:
            try:
                year, month, day = int(d[:4]), int(d[4:6]), int(d[6:8])
                if len(t) >= 6:
                    hour, minute, sec = int(t[:2]), int(t[2:4]), int(t[4:6])
                else:
                    hour, minute, sec = 0, 0, 0
                return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{sec:02d}Z"
            except (ValueError, TypeError):
                continue
    return None


def _map_modality_from_sidecar(sidecar: dict) -> str | None:
    """
    Map VistaSoft image metadata to DTX Studio Core modality enum values.

    Confirmed accepted values (probed against Core 3.13.22.2):
      PANORAMIC   — panoramic X-ray
      INTRAORAL   — intraoral X-ray (bitewing, periapical, scout)
      CEPHALOGRAM — cephalometric / lateral skull
      VOLUME      — CBCT / cone beam 3D
      PICTURE     — photo, video, screen capture (no separate MOVIE bucket)

    Priority:
      1. ImageClass (DB field) — maps via C# ToMediaType() logic
      2. AcquisitionTypeName  — from sidecar JSON
      3. AcquisitionModeClasses / AcquisitionModeDimensionID — device class
      4. DICOM Modality tag   — last resort (CR/DX are ambiguous, skipped)
    """
    # ImageClass comes from the Firebird DB IMAGE.IMAGECLASS column and is
    # passed through the media dict. The C# ToMediaType() switch maps:
    #   Pano -> image/pano, Ceph -> image/ceph, Intra -> image/intraoral_xray,
    #   Xray -> image/intraoral_xray, Video -> unsupported, Snapshot -> screen_capture,
    #   Dvt -> volume/multi_frame, Proof -> image/scout
    ic       = (sidecar.get("ImageClass") or "").strip()
    atn      = (sidecar.get("AcquisitionTypeName") or "").strip().lower()
    modality = (sidecar.get("Modality") or "").strip().upper()

    # Check AcquisitionModeClasses first — these reveal the actual capture device.
    # VistaSoft uses ImageClass="Intra" for BOTH intraoral X-rays AND intraoral
    # camera photos. AcquisitionModeClasses disambiguates:
    #   ["VideoDentalIntra"] or ["CameraIntraoral"] = photo/video → PICTURE
    #   ["XRayDentalIntra"]  or similar              = X-ray      → INTRAORAL
    mode_classes = [c.lower() for c in (sidecar.get("AcquisitionModeClasses") or [])]
    mode_dim     = (sidecar.get("AcquisitionModeDimensionID") or "").lower()
    mode_all     = " ".join(mode_classes) + " " + mode_dim

    # Camera/video device → always PICTURE regardless of ImageClass
    if any(x in mode_all for x in ("video", "camera", "photo")):
        return "PICTURE"

    # DICOM Modality XC = External Camera — always a photo regardless of ImageClass
    if modality == "XC":
        return "PICTURE"

    # 1. ImageClass — exact C# ToMediaType() mapping
    _ic_map = {
        "Pano":     "PANORAMIC",
        "Ceph":     "CEPHALOGRAM",
        "Intra":    "INTRAORAL",
        "Xray":     "INTRAORAL",
        "Video":    "PICTURE",    # no MOVIE bucket in DTX Studio UI
        "Snapshot": "PICTURE",
        "Dvt":      "VOLUME",
        "Proof":    "INTRAORAL",
    }
    if ic in _ic_map:
        return _ic_map[ic]

    # 2. AcquisitionTypeName
    _atn_map = {
        "panoramic":     "PANORAMIC",
        "pano":          "PANORAMIC",
        "cephalometric": "CEPHALOGRAM",
        "cephalogram":   "CEPHALOGRAM",
        "ceph":          "CEPHALOGRAM",
        "intraoral":     "INTRAORAL",
        "bitewing":      "INTRAORAL",
        "periapical":    "INTRAORAL",
        "cbct":          "VOLUME",
        "cone beam":     "VOLUME",
        "3d":            "VOLUME",
        "volume":        "VOLUME",
        "video":         "PICTURE",
        "photo":         "PICTURE",
        "photograph":    "PICTURE",
        "camera":        "PICTURE",
        "extraoral":     "PICTURE",
    }
    for key, val in _atn_map.items():
        if key in atn:
            return val

    # 3. AcquisitionModeClasses / AcquisitionModeDimensionID (re-check for remaining cases)
    if any(x in mode_all for x in ("panorama", "pano")):
        return "PANORAMIC"
    if any(x in mode_all for x in ("ceph", "lateral", "skull")):
        return "CEPHALOGRAM"
    if any(x in mode_all for x in ("cbct", "cone", "volume", "3d", "dvt")):
        return "VOLUME"
    if any(x in mode_all for x in ("camera", "photo", "video", "extraoral", "facial")):
        return "PICTURE"
    if any(x in mode_all for x in ("intra", "bitewing", "periapical")):
        return "INTRAORAL"

    # 4. DICOM Modality tag (PX is unambiguous; CR/DX are not — skip them here)
    _dicom_map = {
        "PX": "PANORAMIC",
        "CT": "VOLUME",
        "XC": "PICTURE",
        "OT": "PICTURE",
        "ES": "PICTURE",
        "RF": "PICTURE",
    }
    if modality in _dicom_map:
        return _dicom_map[modality]
    if modality in ("CR", "DX", "IO"):
        return "INTRAORAL"

    return None


def _build_dicom_tags_request(sidecar: dict) -> dict:
    """
    Map fields from the VistaSoft .json sidecar to the CoreMediaDicomTagsRequest schema.

    Sidecar field names (from VistaSoft JSON)  →  CoreMediaDicomTagsRequest property
    ─────────────────────────────────────────────────────────────────────────────────
    SamplesPerPixel          → SamplesPerPixel       (int)
    PhotometricInterpretation→ PhotometricInterpretation (str)
    Rows                     → Rows                  (int)
    Columns                  → Columns               (int)
    BitsAllocated            → BitsAllocated         (int)
    BitsStored               → BitsStored            (int)
    HighBit                  → HighBit               (int)
    PixelRepresentation      → PixelRepresentation   (str)
    PlanarConfiguration      → PlanarConfiguration   (str)
    Manufacturer             → Manufacturer          (str)
    ManufacturerModelName    → ManufacturerModelName (str)
    SOPClassUID              → SopClassUid           (str)
    SOPInstanceUID           → SopInstanceUid        (str)
    ImageType                → ImageType             (str)
    ImageCompression         → LossyImageCompression (bool — "Lossy" → true)
    SpecificCharacterSet     → SpecificCharacterSet  (str)
    """

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _str(v):
        return str(v).strip() if v is not None and str(v).strip() else None

    tags = {}

    # Integer pixel geometry fields
    for src, dst in [
        ("SamplesPerPixel",  "samplesPerPixel"),
        ("Rows",             "rows"),
        ("Columns",          "columns"),
        ("BitsAllocated",    "bitsAllocated"),
        ("BitsStored",       "bitsStored"),
        ("HighBit",          "highBit"),
    ]:
        v = _int(sidecar.get(src))
        if v is not None:
            tags[dst] = v

    # String fields
    # NOTE: pixelRepresentation and planarConfiguration are stored as integers
    # in VistaSoft sidecars (0/1) but DTX Studio Core rejects them even as strings.
    # They are omitted here — the raw DICOM data in the uploaded .dcm file
    # carries these values natively.
    for src, dst in [
        ("PhotometricInterpretation", "photometricInterpretation"),
        ("Manufacturer",              "manufacturer"),
        # VistaSoft sidecar uses "ManufacturerModelName" but also check device desc
        ("ManufacturerModelName",     "manufacturerModelName"),
        ("DeviceDescription",         "manufacturerModelName"),  # fallback
        ("SOPClassUID",               "sopClassUid"),
        ("SOPInstanceUID",            "sopInstanceUid"),
        ("ImageType",                 "imageType"),
        ("SpecificCharacterSet",      "specificCharacterSet"),
        # Modality goes in DICOM tags (not in create payload — DTX rejects it there)
        ("Modality",                  "modality"),
    ]:
        v = _str(sidecar.get(src))
        if v:
            tags[dst] = v

    # OriginalSopInstanceUid = same as SopInstanceUid for source images
    if tags.get("sopInstanceUid"):
        tags["originalSopInstanceUid"] = tags["sopInstanceUid"]

    # LossyImageCompression: sidecar has "Uncompressed", "Lossy", "Lossless" etc.
    compression = _str(sidecar.get("ImageCompression") or "")
    if compression:
        tags["lossyImageCompression"] = compression.lower() == "lossy"

    return tags


def _map_gender(sex: str) -> Optional[str]:
    """Map Firebird SEX to DTX Studio gender enum: MALE / FEMALE / OTHER / None.
    From C# source: corePatient.Gender switch { "MALE" => Male, "FEMALE" => Female, "OTHER" => Other, _ => null }
    """
    s = (sex or "").strip().upper()
    if s in ("M", "MALE", "1"):
        return "MALE"
    if s in ("F", "FEMALE", "2"):
        return "FEMALE"
    if s:
        return "OTHER"
    return None  # empty — API accepts null/missing


class DTXStudioSource(BaseDatasource):

    @property
    def name(self) -> str:
        return "dtxstudio_source"

    @property
    def display_name(self) -> str:
        return "DTX Studio"

    @property
    def role(self) -> str:
        return "source"

    def _setup_configuration(self):
        self.configuration = [
            ConfigurationItem(
                key="CoreUrl",
                name="DTX Studio Core URL",
                placeholder="https://servername:port",
                advanced=False,
                config_type=ConfigurationType.TEXT,
                group="CoreConnection",
            ),
            ConfigurationItem(
                key="CoreUsername",
                name="DTX Studio Core Username",
                advanced=False,
                config_type=ConfigurationType.TEXT,
                group="CoreConnection",
            ),
            ConfigurationItem(
                key="CorePassword",
                name="DTX Studio Core Password",
                advanced=False,
                config_type=ConfigurationType.PASSWORD,
                group="CoreConnection",
            ),
            ConfigurationItem(
                key="CoreVersion",
                name="Detected Core Version",
                hidden=False,
                value="",
                config_type=ConfigurationType.READ_ONLY,
                group="CoreConnection",
            ),
            ConfigurationItem(
                key="PatientIds",
                name="Patient ID Mode",
                description="Which patient IDs to import from source",
                config_type=ConfigurationType.SELECT,
                options=PATIENT_ID_OPTIONS,
                hidden=False,
                value="pms",
                group="Import",
            ),
            ConfigurationItem(
                key="Encoding",
                name="Text Encoding",
                description="Default encoding to read text files",
                value="utf-8",
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
        ]

    def validate(self) -> tuple[bool, str]:
        url      = self.get_config_value("CoreUrl")
        username = self.get_config_value("CoreUsername")
        password = self.get_config_value("CorePassword")
        if not url:
            return False, "DTX Studio Core URL is required."
        if not url.startswith("http"):
            return False, "DTX Studio Core URL must start with http:// or https://"
        if not username:
            return False, "DTX Studio Core Username is required."
        if not password:
            return False, "DTX Studio Core Password is required."
        return True, "DTX Studio configuration is valid."

    def _make_client(self) -> CoreClient:
        return CoreClient(
            base_url=self.get_config_value("CoreUrl"),
            username=self.get_config_value("CoreUsername"),
            password=self.get_config_value("CorePassword"),
        )

    def test_connection(self) -> tuple[bool, str]:
        """
        Calls GET dw-endpoint/api/info and checks the version meets the minimum.
        """
        url = self.get_config_value("CoreUrl") or "(no URL set)"
        try:
            client = self._make_client()
            info = client.get_core_info()

            # Extract version — CoreInfo likely has a 'version' or 'coreVersion' field
            version_str = (
                info.get("version")
                or info.get("coreVersion")
                or info.get("buildVersion")
                or info.get("Version")
                or ""
            )
            self.set_config_value("CoreVersion", version_str or "unknown")

            if version_str:
                try:
                    parts = [int(x) for x in version_str.split(".")[:2] if x.isdigit()]
                    detected = tuple(parts) if len(parts) >= 2 else (0, 0)
                except (ValueError, AttributeError):
                    detected = (0, 0)
                if detected < MIN_CORE_VERSION:
                    return False, (
                        f"Core version {version_str!r} is below the minimum "
                        f"required version {MIN_CORE_VERSION[0]}.{MIN_CORE_VERSION[1]}."
                    )
                return True, f"Connected to {url}. Core version: {version_str}"

            # Info responded but had no version field — still connected
            practice_name = info.get("practiceName") or info.get("name") or ""
            detail = f" ({practice_name})" if practice_name else ""
            return True, f"Connected to {url}{detail}. (Version not reported by server)"

        except ConnectionError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"[{type(exc).__name__}] {exc!r}"

    def load(
        self,
        cancel_flag,
        max_parallelism: int = 4,
        progress_callback: Optional[Callable] = None,
    ) -> list:
        """
        Mirrors DTXStudioDatasource.LoadAsync():
        Fetches all patients then in parallel loads their full
        studies → series → DICOM tags → media hierarchy.
        """
        use_internal_id = self.get_config_value("PatientIds") == "pms_cv11"
        skip_empty      = self.get_config_value("SkipEmptyPatients") == "On"
        client          = self._make_client()

        self.logger.debug("Fetching patient list from DTX Studio Core")
        raw_patients = client.get_patients()
        total = len(raw_patients)
        self.logger.debug(f"Total patients: {total}")
        if progress_callback:
            progress_callback(0, total, f"Loading media for {total} patient folders…")

        completed = [0]
        errors    = [0]
        lock      = __import__("threading").Lock()
        patients  = []

        def process_patient(raw: dict):
            if cancel_flag():
                return None
            patient_id = raw.get("id", "")
            try:
                patient = {
                    "uid":          patient_id,
                    "dtx_id":       patient_id,   # DTX Studio UUID — used as SOURCEINSTANCEID
                    "id":           raw.get("internalId" if use_internal_id else "pmsId", patient_id),
                    "pms_id":       raw.get("pmsId", ""),
                    "given_names":  raw.get("firstName", raw.get("firstname", "")),
                    "family_name":  raw.get("lastName",  raw.get("lastname",  "")),
                    "date_of_birth": raw.get("dateOfBirth", raw.get("birthDate", "")),
                    "gender":       raw.get("gender", ""),
                    "studies":      {},
                }

                for study_data in client.get_patient_studies(patient_id):
                    if cancel_flag():
                        break
                    study_id = study_data.get("id", "")
                    study = patient["studies"].setdefault(study_id, {
                        "uid":            study_id,
                        "study_instance": study_data.get("studyInstanceUid", ""),
                        "study_datetime": study_data.get("studyDateTime", ""),
                        "accession":      study_data.get("accessionNumber", ""),
                        "description":    study_data.get("studyDescription", ""),
                        "ref_physician":  study_data.get("referringPhysicianName", ""),
                        "series":         {},
                    })

                    for series_data in client.get_study_series(study_id):
                        if cancel_flag():
                            break
                        series_id   = series_data.get("id", "")
                        series_dicom = client.get_series_dicom_tags(series_id)
                        series = study["series"].setdefault(series_id, {
                            "uid":        series_id,
                            "dicom_tags": series_dicom,
                            "media":      [],
                        })

                        for media_data in client.get_series_media(series_id):
                            if cancel_flag():
                                break
                            media_id   = media_data.get("id", "")
                            media_type = media_data.get("mediaType", "")
                            has_file   = media_data.get("hasFile", False)
                            try:
                                media_dicom = client.get_media_dicom_tags(media_id)
                                if media_type in ("IMAGE", "VOLUME") and has_file:
                                    # Capture client + media_id in closure for lazy fetch
                                    def _make_fetch(c, mid):
                                        return lambda: c.get_media_data(mid)
                                    def _make_preview_fetch(c, mid):
                                        return lambda: c.get_media_preview_data(mid)
                                    # Map DTX modality -> VistaSoft image_class
                                    _dtx_to_class = {
                                        "PANORAMIC": "Pano", "CEPHALOGRAM": "Ceph",
                                        "INTRAORAL": "Intra", "VOLUME": "Dvt",
                                        "PICTURE": "Snapshot", "MOVIE": "Snapshot",
                                    }
                                    _mod = media_data.get("modality", "")
                                    _iclass = _dtx_to_class.get(_mod, "Intra")
                                    series["media"].append({
                                        "uid":          media_id,
                                        "media_type":   media_type,
                                        "image_class":  _iclass,
                                        "modality":     _mod,
                                        "acq_datetime": media_data.get("acquisitionDate", ""),
                                        "sop_instance": media_data.get("sopInstanceUid", ""),
                                        "dicom_tags":   media_dicom,
                                        "content_type": "application/dicom",
                                        "preview_path": None,
                                        "file":         None,
                                        "_fetch":       _make_fetch(client, media_id),
                                        "_fetch_preview": _make_preview_fetch(client, media_id),
                                    })
                                elif media_type in ("ATTACHMENT", "SURFACE"):
                                    files = client.get_media_files(media_id)
                                    series["media"].append({
                                        "uid":        media_id,
                                        "media_type": media_type,
                                        "dicom_tags": media_dicom,
                                        "file":       files[0] if files else None,
                                    })
                            except Exception as exc:
                                errors[0] += 1
                                self.logger.error(f"Failed to load media {media_id}: {exc}")

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
            futures = {pool.submit(process_patient, rp): rp for rp in raw_patients}
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
                            progress_callback(completed[0], total, f"Skipped patient ({completed[0]}/{total})")

        self.logger.info(f"DTX Studio load complete — {len(patients)} patients, {errors[0]} errors")
        return patients

    def reattach_fetch(self, patients: list) -> list:
        """
        Re-attach download callbacks to media items after deserialisation
        from the migration store (which can only store JSON).
        Called by the engine before write_patients when source is DTX Studio.
        """
        client = self._make_client()
        for patient in patients:
            for study in patient.get("studies", {}).values():
                for series in study.get("series", {}).values():
                    for media in series.get("media", []):
                        media_id = media.get("uid", "")
                        if media_id and "_fetch" not in media:
                            def _make_fetch(c, mid):
                                return lambda: c.get_media_data(mid)
                            media["_fetch"] = _make_fetch(client, media_id)
        return patients