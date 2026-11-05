"""DTX Studio — TARGET datasource. Writes patients/media to DTX Studio Core REST API."""
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

    # ── Transport ──────────────────────────────────────────────────
    # One keep-alive connection per client (a client is used by one thread),
    # instead of a new TCP + TLS handshake for every request. Transient
    # failures are retried; uploads are streamed from disk with a timeout
    # that scales with size.
    _RETRY_STATUS = {429, 502, 503, 504}
    _STALE_ERRORS = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)

    def _connection(self, timeout: float):
        import http.client
        from urllib.parse import urlsplit
        if getattr(self, "_conn", None) is None:
            u = urlsplit(self.base_url)
            if u.scheme == "https":
                self._conn = http.client.HTTPSConnection(
                    u.hostname, u.port or 443, timeout=timeout, context=self._ssl_ctx)
            else:
                self._conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            self._conn_fresh = True
        else:
            self._conn.timeout = timeout
            if self._conn.sock is not None:
                self._conn.sock.settimeout(timeout)
            self._conn_fresh = False
        return self._conn

    def close(self):
        conn, self._conn = getattr(self, "_conn", None), None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _request(self, method: str, url: str, body=None, content_type: str = None,
                 timeout: float = None) -> bytes:
        import http.client
        import time
        from urllib.parse import urlsplit

        u = urlsplit(url)
        path = u.path + (f"?{u.query}" if u.query else "")
        headers = self._headers()
        if content_type:
            headers["Content-Type"] = content_type

        # Body: bytes, or a seekable file object streamed in chunks
        start, length = 0, 0
        if body is not None and hasattr(body, "read"):
            start = body.tell()
            body.seek(0, 2)
            length = body.tell() - start
            body.seek(start)
        elif body is not None:
            length = len(body)
        if body is not None:
            headers["Content-Length"] = str(length)
        if timeout is None:
            timeout = 60 if length < 5_000_000 else max(120, length / 1_000_000 * 5)

        idempotent = method in ("GET", "PUT", "DELETE", "HEAD")
        attempts = 4 if idempotent else 2
        last_exc = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2 ** (attempt - 1), 8))
            if body is not None and hasattr(body, "read"):
                body.seek(start)
            fresh = True
            try:
                conn = self._connection(timeout)
                fresh = self._conn_fresh
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                if resp.will_close:
                    self.close()
            except (http.client.RemoteDisconnected, *self._STALE_ERRORS) as exc:
                # A reused keep-alive connection the server had already closed:
                # the request never reached it, so retrying is safe for any method.
                self.close()
                last_exc = exc
                if not fresh or idempotent:
                    continue
                break
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                self.close()
                last_exc = exc
                if idempotent:
                    continue
                break

            if resp.status >= 400:
                snippet = data.decode(errors="replace")[:300]
                err = ConnectionError(
                    f"HTTP {resp.status} {resp.reason} from {url}" + (f": {snippet}" if snippet else ""))
                # 429/5xx gateway errors mean the server did not process the request
                if resp.status in self._RETRY_STATUS and attempt + 1 < attempts:
                    last_exc = err
                    continue
                raise err
            return data

        reason = last_exc
        if isinstance(reason, Exception):
            reason = f"{type(reason).__name__}: {reason}"
        raise ConnectionError(f"Cannot reach {url}: {reason}")

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
        # File objects are streamed, not read into memory (CBCT volumes can be hundreds of MB)
        raw = self._request("PUT", self._url(path), body=stream, content_type=content_type)
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
        from urllib.parse import quote
        return self._get(f"patients?api-resource-version=2&search.patientPmsId={quote(str(patient_id), safe='')}"
                         f"&sort.onLastCapturedDate=DESC")

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
    VistaSoft stores files without extensions but the actual files
    on disk may have .dcm, .jpg etc.
    """
    if not path:
        return None
    if os.path.isfile(path):
        return path
    for ext in (".dcm", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff",
                ".vsx", ".vsf", ".raw", ".bin", ".dat", ".img"):
        candidate = path + ext
        if os.path.isfile(candidate):
            return candidate
    # Same name with some other extension. Must be "<name>." exactly — a bare
    # prefix match could pick up a different image ("123" vs "1234.dcm").
    parent = os.path.dirname(path)
    base   = os.path.basename(path).lower()
    if os.path.isdir(parent):
        for f in os.listdir(parent):
            fl = f.lower()
            if fl == base or (fl.startswith(base + ".")
                              and not fl.endswith((".json", ".jpg.tmp", ".tmp"))):
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


def _normalise_datetime(dt: str) -> str:
    """Ensure a datetime string is full ISO 8601 with Z suffix for DTX API."""
    if not dt:
        return ""
    dt = dt.strip().rstrip('Z')
    if 'T' in dt:
        return dt[:19] + "Z"
    if len(dt) >= 10:
        return dt[:10] + "T00:00:00Z"
    return ""


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
        ("PixelRepresentation", "pixelRepresentation"),
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


_NO_PNG_CLASSES = {"Dvt", "Video"}      # volumes / movies keep their own format


def _image_to_png(data: bytes):
    """
    Re-encode a 2D image (DICOM, JPEG, BMP, TIFF…) as PNG, losslessly.
    Greyscale above 8 bits is scaled to the full 16-bit range so every viewer
    shows it correctly whatever the original BitsStored. Returns
    (png_bytes, pixel_tags) or (None, None) if it can't be decoded here.
    """
    import io
    import numpy as np
    from PIL import Image

    arr = None
    if len(data) > 132 and data[128:132] == b"DICM":
        from datasources.dicom_header import read_dicom_image
        info = read_dicom_image(data)
        if not info:
            return None, None
        arr, bits = info["array"], info["bits_stored"]
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            return None, None
        if img.mode in ("I;16", "I;16L", "I;16B", "I"):
            arr, bits = np.asarray(img).astype(np.int32), 16
        elif img.mode == "L":
            arr, bits = np.asarray(img), 8
        else:
            arr, bits = np.asarray(img.convert("RGB")), 8

    if arr.ndim == 3:
        out = Image.fromarray(arr.astype(np.uint8), "RGB")
        tags = {"SamplesPerPixel": 3, "PhotometricInterpretation": "RGB",
                "BitsAllocated": 8, "BitsStored": 8, "HighBit": 7}
    elif bits <= 8:
        out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "L")
        tags = {"SamplesPerPixel": 1, "PhotometricInterpretation": "MONOCHROME2",
                "BitsAllocated": 8, "BitsStored": 8, "HighBit": 7}
    else:
        full = (1 << bits) - 1
        a16 = (arr.astype(np.float64) * (65535.0 / full) + 0.5).astype(np.uint16) if bits < 16 \
            else arr.astype(np.uint16)
        out = Image.fromarray(a16)          # mode I;16
        tags = {"SamplesPerPixel": 1, "PhotometricInterpretation": "MONOCHROME2",
                "BitsAllocated": 16, "BitsStored": 16, "HighBit": 15}
    tags.update({"Rows": out.size[1], "Columns": out.size[0], "PixelRepresentation": 0})
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=3)
    return buf.getvalue(), tags


_PIXEL_TAG_KEYS = ("Rows", "Columns", "SamplesPerPixel", "PhotometricInterpretation",
                   "BitsAllocated", "BitsStored", "HighBit", "PixelRepresentation")


def _dicom_pixel_tags(data: bytes = None, path: str = None) -> dict:
    """Pixel-layout tags read from the DICOM being uploaded ({} if not DICOM)."""
    try:
        from datasources.dicom_header import read_dicom_header, read_dicom_header_bytes
        hdr = read_dicom_header_bytes(data) if data is not None else read_dicom_header(path)
    except Exception:
        hdr = None
    if not hdr:
        return {}
    return {k: hdr[k] for k in _PIXEL_TAG_KEYS if hdr.get(k) not in (None, "")}


def _merge_pixel_tags(source_tags, file_tags: dict) -> dict:
    tags = dict(source_tags or {})
    tags.update(file_tags or {})          # the uploaded file is the truth
    return tags


def _as_list(resp) -> list:
    """DTX list endpoints return a bare list or a paged object — accept both."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("items", "content", "data", "results", "patients", "media", "value"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def _same_person(existing: dict, patient: dict) -> bool:
    """A DTX patient with the same PMS ID only counts as the same person if
    the surname or the date of birth also agrees — guards against PMS-ID
    collisions with patients already in DTX from another system."""
    ex_last = _norm(existing.get("lastname") or existing.get("lastName"))
    src_last = _norm(patient.get("family_name"))
    if ex_last and src_last and ex_last == src_last:
        return True
    ex_dob = str(existing.get("dateOfBirth") or existing.get("birthDate") or "")[:10]
    src_dob = str(patient.get("birth_date") or patient.get("dob") or "")[:10]
    return bool(ex_dob and src_dob and ex_dob == src_dob)


def _media_sop(media: dict) -> str:
    tags = media.get("dicom_tags") or {}
    return str(tags.get("SOPInstanceUID") or media.get("sop_instance") or "").strip()


def _map_gender(sex: str) -> Optional[str]:
    """Map Firebird SEX to DTX Studio gender enum: MALE / FEMALE / OTHER / None.
    From C# source: corePatient.Gender switch { "MALE" => Male, "FEMALE" => Female, "OTHER" => Other, _ => null }
    """
    s = (sex or "").strip().upper()
    # VistaSoft stores the ASCII code of M / F / O (77 / 70 / 79)
    if s in ("M", "MALE", "1", "77"):
        return "MALE"
    if s in ("F", "FEMALE", "2", "70"):
        return "FEMALE"
    if s:
        return "OTHER"
    return None  # empty — API accepts null/missing


class DTXStudioTarget(BaseDatasource):
    # Each write_patients call builds its own client and holds no shared
    # state, so the engine may write several patients at once.
    supports_parallel_writes = True


    @property
    def name(self) -> str:
        return "dtxstudio_target"

    @property
    def display_name(self) -> str:
        return "DTX Studio"

    @property
    def role(self) -> str:
        return "target"

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

    def write_patients(self, patients: list, progress_callback=None,
                       cancel_flag=None, incremental: bool = False) -> dict:
        """
        Writes VistaSoft patients into DTX Studio Core:
          1. Create patient  →  POST /patients
          2. Create study    →  POST /patients/{id}/studies
          3. Create series   →  POST /studies/{id}/series
          4. Create media    →  POST /series/{id}/media
          5. Upload binary   →  PUT  /media/{id}/data
        """
        client  = self._make_client()
        results = {"success": 0, "failed": 0, "errors": [],
                   "media_uploaded": 0, "media_missing": 0, "media_existing": 0}
        try:
            return self._write_patients(client, patients, results, progress_callback)
        finally:
            client.close()

    _search_warned = False
    _send_pixel_rep = True      # cleared if this Core rejects pixelRepresentation

    def _put_dicom_tags(self, client, media_id: str, tags: dict):
        """PUT the media DICOM tags. Never fails the image."""
        req = _build_dicom_tags_request(tags)
        if not req:
            return
        if not DTXStudioTarget._send_pixel_rep:
            req.pop("pixelRepresentation", None)
        try:
            client.create_media_dicom_tags(media_id, req)
            return
        except Exception as exc:
            if "pixelRepresentation" not in req:
                self.logger.warning(f"        DICOM tags failed: {exc}")
                return
            first = exc
        # Some Core versions reject pixelRepresentation — retry without it
        req.pop("pixelRepresentation", None)
        try:
            client.create_media_dicom_tags(media_id, req)
            DTXStudioTarget._send_pixel_rep = False
            self.logger.info("        DTX Core rejected pixelRepresentation in DICOM tags — sending without it")
        except Exception as exc:
            self.logger.warning(f"        DICOM tags failed: {exc} (first attempt: {first})")

    def _find_existing_patient(self, client, patient: dict) -> Optional[dict]:
        """
        Look the patient up in DTX by PMS ID before creating it, so a retry of
        a partly-migrated patient (or an incremental run after a filtered one)
        adds to the existing DTX patient instead of creating a duplicate.
        """
        pms_id = str(patient.get("id", "") or "").strip()
        if not pms_id:
            return None
        try:
            found = _as_list(client.search_patients_by_pms_id(pms_id))
        except Exception as exc:
            if not DTXStudioTarget._search_warned:
                DTXStudioTarget._search_warned = True
                self.logger.warning(f"DTX patient search by PMS ID failed ({exc}) — "
                                    f"duplicate check skipped, patients will be created.")
            return None
        matches = [p for p in found
                   if str(p.get("pmsId", "")).strip().lower() == pms_id.lower() and p.get("id")]
        same = [p for p in matches if _same_person(p, patient)]
        if matches and not same:
            self.logger.warning(f"  DTX already has a patient with PMS ID {pms_id} but a different "
                                f"surname and date of birth — creating a separate patient.")
            return None
        if len(same) > 1:
            self.logger.warning(f"  {len(same)} DTX patients share PMS ID {pms_id} — using the most recent.")
        return same[0] if same else None

    def _existing_index(self, client, dtx_patient_id: str) -> dict:
        """Studies (by StudyInstanceUID) and media (by SOPInstanceUID) the DTX
        patient already has."""
        studies, media = {}, {}
        for st in _as_list(client.get_patient_studies(dtx_patient_id)):
            uid = str(st.get("studyInstanceUid") or "").strip()
            if uid and st.get("id"):
                studies[uid] = st["id"]
        for m in _as_list(client.get_patient_media(dtx_patient_id)):
            mid = m.get("id")
            if not mid:
                continue
            sop = str(m.get("sopInstanceUid") or "").strip()
            if not sop:
                try:
                    tags = client.get_media_dicom_tags(mid) or {}
                    sop = str(tags.get("sopInstanceUid") or tags.get("SopInstanceUid") or "").strip()
                except Exception:
                    sop = ""
            if sop:
                media[sop] = (mid, bool(m.get("hasFile", True)))
        return {"studies": studies, "media": media}

    def _write_patients(self, client, patients, results, progress_callback):

        for i, patient in enumerate(patients):
            name = f"{patient.get('given_names', '')} {patient.get('family_name', '')}".strip()
            try:
                # 1. Create patient — gender is required by DTX, default OTHER
                raw_sex = (patient.get("sex") or patient.get("gender") or "")
                gender  = _map_gender(raw_sex) or "OTHER"
                payload = {
                    "dicomId":    patient.get("id", "") or None,
                    "pmsId":      patient.get("id", "") or None,
                    "firstname":  patient.get("given_names", ""),
                    "lastname":   patient.get("family_name", ""),
                    "middlename": patient.get("middle_name", "") or None,
                    "dateOfBirth": patient.get("birth_date") or patient.get("dob") or None,
                    "gender":     gender,
                }
                payload = {k: v for k, v in payload.items() if v is not None}
                existing = self._find_existing_patient(client, patient)
                if existing:
                    new_patient_id = existing["id"]
                    idx = self._existing_index(client, new_patient_id)
                    self.logger.info(f"Patient already in DTX ({new_patient_id}) — adding missing images "
                                     f"({len(idx['media'])} already there)")
                else:
                    idx = None
                    self.logger.debug(f"Creating patient payload: {payload}")
                    new_patient    = client.create_patient(payload)
                    new_patient_id = new_patient.get("id", "")
                    self.logger.info(f"Created patient {new_patient_id} — studies: {len(patient.get('studies', {}))}")

                # 2. Create studies
                studies = patient.get("studies", {})
                self.logger.info(f"  Studies to create: {len(studies)}")
                for study in studies.values():
                    # Format study datetime — Firebird returns it in various formats
                    raw_study_dt = study.get("study_datetime", "") or ""
                    study_dt = _format_study_datetime(raw_study_dt)

                    study_uid = str(study.get("study_instance", "") or "").strip()
                    existing_series = []
                    if idx and study_uid and study_uid in idx["studies"]:
                        new_study_id = idx["studies"][study_uid]
                        existing_series = [x.get("id") for x in _as_list(client.get_study_series(new_study_id))
                                           if x.get("id")]
                        self.logger.info(f"    Using existing study {new_study_id}")
                    else:
                        new_study = client.create_patient_study(new_patient_id, {
                            "studyInstanceUid":       study_uid or None,
                            "studyDateTime":          study_dt or None,
                            "accessionNumber":        study.get("accession", "") or None,
                            "studyDescription":       study.get("description", "") or None,
                            "referringPhysicianName": study.get("ref_physician", "") or None,
                        })
                        new_study_id = new_study.get("id", "")
                        self.logger.info(f"    Created study {new_study_id}")

                    # 3. Create series
                    series_list = study.get("series", {})
                    self.logger.info(f"    Series to create: {len(series_list)}")
                    for s_no, series in enumerate(series_list.values()):
                        if s_no < len(existing_series):
                            new_series_id = existing_series[s_no]
                        else:
                            new_series = client.create_study_series(new_study_id, {})
                            new_series_id = new_series.get("id", "")
                            self.logger.info(f"      Created series {new_series_id}")

                            if series.get("dicom_tags"):
                                client.create_series_dicom_tags(new_series_id, series["dicom_tags"])

                        # 4. Create media entries and upload binary data
                        media_list = series.get("media", [])
                        self.logger.info(f"      Media items to upload: {len(media_list)}")
                        for media in media_list:
                            import io as _io
                            import mimetypes as _mt

                            # Already in DTX from an earlier run? Skip it if its
                            # image arrived; re-upload into the same record if not.
                            reuse_media_id = None
                            sop = _media_sop(media)
                            if idx and sop and sop in idx["media"]:
                                mid, has_file = idx["media"][sop]
                                if has_file:
                                    results["media_existing"] = results.get("media_existing", 0) + 1
                                    self.logger.info(f"        Already in DTX: {mid[:8]}… — skipped")
                                    continue
                                reuse_media_id = mid

                            # 4a. Get the image data first, so a missing / unreadable
                            #     file doesn't leave an empty media record in DTX.
                            #     A source-supplied _fetch (e.g. a converted image)
                            #     wins over the raw file on disk.
                            fetch = media.get("_fetch") if callable(media.get("_fetch")) else None
                            data_obj, data_desc, file_tags = None, "", {}
                            if fetch:
                                try:
                                    data = fetch()
                                    if data:
                                        data_obj, data_desc = _io.BytesIO(data), f"{len(data):,} bytes"
                                        file_tags = _dicom_pixel_tags(data=data)
                                    else:
                                        raise FileNotFoundError(media.get("file_path") or "no data")
                                except Exception as fe:
                                    self.logger.warning(f"        Image not migrated — could not read/convert "
                                                        f"{media.get('file_path') or media.get('uid','')}: {fe}")
                                    results["media_missing"] += 1
                                    continue
                                ct = media.get("content_type") or "application/dicom"
                            else:
                                file_path = _find_file(media.get("file_path", ""))
                                if not file_path:
                                    self.logger.warning(f"        Image not migrated — no file: {media.get('file_path','')}")
                                    results["media_missing"] += 1
                                    continue
                                ct = (media.get("content_type") or _mt.guess_type(file_path)[0]
                                      or "application/octet-stream")
                                data_obj, data_desc = open(file_path, "rb"), os.path.basename(file_path)
                                file_tags = _dicom_pixel_tags(path=file_path)

                            # 4a-bis. DTX stores 2D images as PNG (the original TPIS
                            #     migrator always PUT image/png). DICOM / JPEG data
                            #     is re-encoded losslessly; DTX's viewer coped with
                            #     raw DICOM but its exporter and Diagnose did not.
                            if (_map_media_type(media.get("image_class", "")) == "IMAGE"
                                    and (media.get("image_class") or "") not in _NO_PNG_CLASSES
                                    and not ct.startswith("image/png")):
                                try:
                                    if fetch:
                                        raw = data
                                    else:
                                        with open(file_path, "rb") as fh:
                                            raw = fh.read()
                                    png, png_tags = _image_to_png(raw)
                                except Exception as ce:
                                    png, png_tags = None, None
                                    self.logger.warning(f"        PNG conversion failed, uploading original: {ce}")
                                if png:
                                    try:
                                        data_obj.close()
                                    except Exception:
                                        pass
                                    data_obj = _io.BytesIO(png)
                                    data_desc = f"{data_desc} → PNG {len(png):,} bytes"
                                    ct, file_tags = "image/png", png_tags
                                else:
                                    self.logger.warning(f"        Kept original format ({ct}) — could not decode it to PNG")

                            try:
                                if reuse_media_id:
                                    new_media_id = reuse_media_id
                                    self.logger.info(f"        Re-uploading into existing media {new_media_id[:8]}…")
                                else:
                                    # 4b. Create the media record
                                    media_type = _map_media_type(media.get("image_class", ""))
                                    payload = {"mediaType": media_type}
                                    modality = (
                                        _map_modality_from_sidecar({"ImageClass": media.get("image_class", ""),
                                                                    "Modality": media.get("modality", "")})
                                        or _map_modality_from_sidecar(media.get("dicom_tags") or {})
                                    )
                                    if modality:
                                        payload["modality"] = modality
                                    acq_dt = (_parse_acq_datetime(media.get("dicom_tags") or {}) or
                                              _normalise_datetime(media.get("acq_datetime") or ""))
                                    if acq_dt:
                                        payload["acquisitionDate"] = acq_dt
                                    new_media = client.create_series_media(new_series_id, payload)
                                    new_media_id = new_media.get("id", "")
                                    self.logger.info(f"        Created media {new_media_id[:8]}… "
                                                     f"type={new_media.get('mediaType')} "
                                                     f"modality={new_media.get('modality')} "
                                                     f"acqDate={str(new_media.get('acquisitionDate',''))[:10]} ")

                                # DICOM tags: the source's values, with the pixel
                                # layout taken from the file actually uploaded (DTX
                                # needs Rows/Columns/bits to export / decode it)
                                tags = _merge_pixel_tags(media.get("dicom_tags"), file_tags)
                                if tags:
                                    self._put_dicom_tags(client, new_media_id, tags)

                                # 5. Upload the image (streamed)
                                client.put_media_data(new_media_id, data_obj, ct)
                                self.logger.info(f"        Uploaded: {data_desc} ({ct})")
                            finally:
                                try:
                                    data_obj.close()
                                except Exception:
                                    pass

                            # 6. Preview — a ready-made file, or one the source renders
                            #    on demand. Never fails the patient.
                            try:
                                preview_path = media.get("preview_path")
                                if preview_path and os.path.isfile(preview_path):
                                    pct = _mt.guess_type(preview_path)[0] or "image/jpeg"
                                    with open(preview_path, "rb") as f:
                                        client.put_media_preview_data(new_media_id, f, pct)
                                    self.logger.info(f"        Preview:  {os.path.basename(preview_path)}")
                                elif callable(media.get("_fetch_preview")):
                                    pdata = media["_fetch_preview"]()
                                    if pdata:
                                        pct = "image/png" if pdata[:4] == b"\x89PNG" else "image/jpeg"
                                        client.put_media_preview_data(new_media_id, _io.BytesIO(pdata), pct)
                                        self.logger.info("        Preview:  (generated)")
                            except Exception as pe:
                                self.logger.warning(f"        Preview failed: {pe}")

                            results["media_uploaded"] += 1

                results["success"] += 1
                if progress_callback:
                    progress_callback(i + 1, len(patients), f"Imported: {name}")

            except Exception as exc:
                results["failed"] += 1
                results["errors"].append(str(exc))
                self.logger.error(f"Failed to write patient {name}: {exc}")
                if progress_callback:
                    progress_callback(i + 1, len(patients), f"Failed: {name} — {exc}")

        return results