"""
VistaSoft — TARGET datasource.
Writes patients and imaging data into VistaSoft (Firebird DB + disk files).

Storage facts from VistaSoft C# source (VisiquickFBDbReaderExtensions):
  - CHAR(16) UUIDs stored as STRAIGHT big-endian hex: X'uuid_no_dashes'
  - FirebirdClient GetGuid() reads bytes straight → returns new_uuid unchanged
  - ToMediaPath: folder = partial_reverse(GetGuid(PATIENT.UID)) = partial_reverse(new_patient_uid)
                 file   = partial_reverse(GetGuid(IMAGE.UID)).dcm = partial_reverse(new_image_uid).dcm
  - VistaSoftUid in sidecar = GetGuid(IMAGE.UID) = new_image_uid
  - InstitutionUID from Institutions.json (auto-detected)
"""
import os
import sys
import logging
from typing import Optional, Callable

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from core.base_datasource import BaseDatasource
from core.models import ConfigurationItem, ConfigurationType


def _jpeg_to_dcm(jpeg_bytes: bytes, sop_uid: str = "",
                  patient_name: str = "", acq_date: str = "") -> bytes:
    """
    Convert JPEG/PNG bytes to a 16-bit greyscale uncompressed DICOM file.

    VistaSoft stores intraoral images as 16-bit greyscale MONOCHROME2 DICOM
    (Explicit VR Little Endian, uncompressed). This matches what VistaSoft's
    own import pipeline produces and is what the sidecar expects:
      OriginalColorDepth: Gray16
      BitsAllocated: 16, BitsStored: 16, HighBit: 15
      SamplesPerPixel: 1
      PhotometricInterpretation: MONOCHROME2
      SOPClassUID: 1.2.840.10008.5.1.4.1.1.1 (Digital X-Ray)
    """
    import io as _io, struct as _st, uuid as _uuid
    import numpy as _np
    from PIL import Image as _PIL

    # Open image and convert to 8-bit greyscale
    img = _PIL.open(_io.BytesIO(jpeg_bytes)).convert('L')
    w, h = img.size

    # Scale 8-bit (0-255) to 16-bit (0-65535)
    arr = _np.array(img, dtype=_np.uint16) * 257
    pixel_data = arr.tobytes()

    sop_instance = sop_uid or str(_uuid.uuid4())
    # Use Digital X-Ray IOD — matches what VistaSoft uses for intraoral images
    sop_class    = '1.2.840.10008.5.1.4.1.1.1'
    ts_uid       = '1.2.840.10008.1.2.1'  # Explicit VR Little Endian

    def _tag(grp, elm, vr, val):
        hdr = _st.pack('<HH', grp, elm) + vr.encode('ascii')
        if vr in ('OB','OW','SQ','UC','UR','UT','UN'):
            return hdr + b'\x00\x00' + _st.pack('<I', len(val)) + val
        return hdr + _st.pack('<H', len(val)) + val

    def _ui(s):
        b = s.encode('ascii'); return b + (b'\x00' if len(b) % 2 else b'')
    def _cs(s):
        b = s.encode('ascii'); return b + (b' '  if len(b) % 2 else b'')
    def _us(v):  return _st.pack('<H', v)
    def _ul(v):  return _st.pack('<I', v)
    def _ds(v):
        b = str(v).encode('ascii'); return b + (b' ' if len(b) % 2 else b'')
    def _lo(s):
        b = s.encode('ascii'); return b + (b' ' if len(b) % 2 else b'')

    # File meta group (0002)
    meta = (
        _tag(0x0002, 0x0002, 'UI', _ui(sop_class)) +
        _tag(0x0002, 0x0003, 'UI', _ui(sop_instance)) +
        _tag(0x0002, 0x0010, 'UI', _ui(ts_uid)) +
        _tag(0x0002, 0x0012, 'UI', _ui('1.2.3.999.1')) +
        _tag(0x0002, 0x0013, 'SH', _cs('ITInfinity'))
    )
    meta = _tag(0x0002, 0x0000, 'UL', _ul(len(meta))) + meta

    # Build acquisition date/time strings
    acq_d = acq_date[:10].replace('-', '')[:8] if acq_date else ''
    acq_t = ''
    if acq_date and 'T' in acq_date:
        acq_t = acq_date.split('T')[1].replace(':', '')[:6]

    # Dataset
    ds = (
        _tag(0x0008, 0x0016, 'UI', _ui(sop_class)) +
        _tag(0x0008, 0x0018, 'UI', _ui(sop_instance)) +
        _tag(0x0008, 0x0020, 'DA', _cs(acq_d)) +
        _tag(0x0008, 0x0023, 'DA', _cs(acq_d)) +
        _tag(0x0008, 0x0030, 'TM', _cs(acq_t)) +
        _tag(0x0008, 0x0033, 'TM', _cs(acq_t)) +
        _tag(0x0008, 0x0060, 'CS', _cs('IO')) +
        _tag(0x0010, 0x0010, 'PN', _lo(patient_name)) +
        _tag(0x0028, 0x0002, 'US', _us(1)) +           # SamplesPerPixel
        _tag(0x0028, 0x0004, 'CS', _cs('MONOCHROME2')) +
        _tag(0x0028, 0x0010, 'US', _us(h)) +            # Rows
        _tag(0x0028, 0x0011, 'US', _us(w)) +            # Columns
        _tag(0x0028, 0x0100, 'US', _us(16)) +           # BitsAllocated
        _tag(0x0028, 0x0101, 'US', _us(16)) +           # BitsStored
        _tag(0x0028, 0x0102, 'US', _us(15)) +           # HighBit
        _tag(0x0028, 0x0103, 'US', _us(0)) +            # PixelRepresentation
        _tag(0x0028, 0x1052, 'DS', _ds(0.0)) +          # RescaleIntercept
        _tag(0x0028, 0x1053, 'DS', _ds(1.0)) +          # RescaleSlope
        _tag(0x0028, 0x1054, 'LO', _lo('US')) +         # RescaleType
        # Pixel data
        _tag(0x7FE0, 0x0010, 'OW', pixel_data)
    )

    return b'\x00' * 128 + b'DICM' + meta + ds


def _read_dicom_pixel_info(data: bytes) -> dict:
    """
    Parse DICOM tag group 0028 to extract pixel geometry.
    Returns dict with rows, cols, spp, photo, bits.
    Works on any valid DICOM file produced by dcmtk or pydicom.
    """
    import struct as _st
    result = {"rows": 0, "cols": 0, "spp": 1, "photo": "", "bits": 8}
    try:
        i = 132  # skip 128-byte preamble + 'DICM'
        while i < len(data) - 12:
            grp = _st.unpack('<H', data[i:i+2])[0]
            elm = _st.unpack('<H', data[i+2:i+4])[0]
            vr  = data[i+4:i+6].decode('ascii', errors='?')
            if vr in ('OB', 'OW', 'SQ', 'UC', 'UR', 'UT', 'UN'):
                length = _st.unpack('<I', data[i+8:i+12])[0]
                vs = i + 12
            else:
                length = _st.unpack('<H', data[i+6:i+8])[0]
                vs = i + 8
            if grp == 0x0028:
                if elm == 0x0002:  # SamplesPerPixel
                    result["spp"]   = _st.unpack('<H', data[vs:vs+2])[0]
                elif elm == 0x0004:  # PhotometricInterpretation
                    result["photo"] = data[vs:vs+length].rstrip(b'\x00 ').decode('ascii', 'replace').strip()
                elif elm == 0x0010:  # Rows
                    result["rows"]  = _st.unpack('<H', data[vs:vs+2])[0]
                elif elm == 0x0011:  # Columns
                    result["cols"]  = _st.unpack('<H', data[vs:vs+2])[0]
                elif elm == 0x0100:  # BitsAllocated
                    result["bits"]  = _st.unpack('<H', data[vs:vs+2])[0]
            if grp > 0x0028:
                break
            i = vs + length
    except Exception:
        pass
    return result


def _uuid_to_ms_hex(uid: str) -> str:
    """Store UUID as MS mixed-endian bytes so GetGuid() returns the original UUID."""
    b = bytes.fromhex(uid.replace("-", ""))
    ms = bytes([b[3],b[2],b[1],b[0], b[5],b[4], b[7],b[6],
                b[8],b[9],b[10],b[11],b[12],b[13],b[14],b[15]])
    return ms.hex()


def _partial_reverse_uuid(uid: str) -> str:
    parts = uid.lower().strip().split("-")
    def rev(h: str) -> str:
        return bytes.fromhex(h)[::-1].hex()
    return f"{rev(parts[0])}-{rev(parts[1])}-{rev(parts[2])}-{parts[3]}-{parts[4]}"


# ImageClass mappings — only values present in AcquisitionTypes.s3db are valid:
# Intra, Pano, Ceph, Dvt, Video
# Snapshot/Proof/Xray are NOT registered and cause PopulateInfo to crash
_MODALITY_TO_CLASS = {
    "PANORAMIC":   "Pano",
    "CEPHALOGRAM": "Ceph",
    "INTRAORAL":   "Intra",
    "VOLUME":      "Dvt",
    "PICTURE":     "Snapshot",  # photos stored as Snapshot
    "MOVIE":       "Video",
}

_CLASS_TO_ACQ_TYPE = {
    "Pano":     "Panoramic",
    "Ceph":     "Cephalometric",
    "Intra":    "Intraoral",
    "Dvt":      "3D",
    "Video":    "Video",
    "Snapshot": "Video",  # stored as Video acquisition type
}


def _modality_to_class(modality: str) -> str:
    return _MODALITY_TO_CLASS.get((modality or "").upper(), "Intra")


def _class_to_acq_type(image_class: str) -> str:
    return _CLASS_TO_ACQ_TYPE.get(image_class, "Intraoral")


class VistaSoftTarget(BaseDatasource):

    @property
    def name(self) -> str:
        return "vistasoft_target"

    @property
    def display_name(self) -> str:
        return "VistaSoft"

    @property
    def role(self) -> str:
        return "target"

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
                key="InstitutionUID",
                name="Institution UID",
                description="Auto-detected from Institutions.json",
                value="",
                advanced=False,
                config_type=ConfigurationType.TEXT,
                group="Connection",
            ),
            ConfigurationItem(
                key="InstitutionName",
                name="Institution Name",
                description="Auto-detected from Institutions.json",
                value="",
                advanced=True,
                config_type=ConfigurationType.TEXT,
                group="Connection",
            ),
            ConfigurationItem(
                key="SkipEmptyPatients",
                name="Skip Patients Without Media",
                value="On",
                advanced=True,
                config_type=ConfigurationType.SWITCH,
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
        if not self.get_config_value("InstitutionUID"):
            self._auto_detect_institution()
        return True, "VistaSoft target configuration is valid."

    def _auto_detect_institution(self):
        """Read InstitutionUID/Name from Institutions.json by searching the filesystem."""
        import json

        media_path = self.get_config_value("MediaPath") or ""

        def _try(path):
            if not os.path.isfile(path):
                return False
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                inst = data.get("Institutions", [{}])[0].get("Institution", {})
                uid  = inst.get("Uid", "")
                name = inst.get("Name", "")
                if uid:
                    self.set_config_value("InstitutionUID",  uid)
                    self.set_config_value("InstitutionName", name)
                    self.logger.info(f"Auto-detected InstitutionUID: {uid} ({name}) from {path}")
                    return True
            except Exception:
                pass
            return False

        d = media_path
        for _ in range(6):
            for sub in [
                os.path.join("ServerService", "ImageManagement", "Institutions.json"),
                "Institutions.json",
            ]:
                if _try(os.path.join(d, sub)):
                    return
            d = os.path.dirname(d)
            if d == os.path.dirname(d):
                break

        drive = os.path.splitdrive(media_path)[0] + os.sep
        skip = {"Windows", "System32", "SysWOW64", "Program Files", "ProgramData",
                "Users", "temp", "Temp", "$Recycle.Bin"}
        try:
            for dirpath, dirnames, filenames in os.walk(drive):
                depth = dirpath.replace(drive, "").count(os.sep)
                if depth > 6:
                    dirnames.clear()
                    continue
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d not in skip]
                if "Institutions.json" in filenames:
                    if _try(os.path.join(dirpath, "Institutions.json")):
                        return
        except PermissionError:
            pass

        self.logger.warning(
            "Could not auto-detect InstitutionUID. "
            "Please enter it manually in the Target config tab."
        )

    def test_connection(self) -> tuple[bool, str]:
        if not os.path.isfile(self._get_db_path()):
            return False, f"Database not found: {self._get_db_path()}"
        return True, f"VistaSoft DB found at {self._get_db_path()}"

    def write_patients(self, patients: list, progress_callback: Optional[Callable] = None,
                       cancel_flag=None, incremental: bool = False) -> dict:
        import uuid as _uuid_mod
        import json as _json
        import re as _re
        from datasources.fb_client import _run

        if cancel_flag is None:
            cancel_flag = lambda: False

        db_path_remote  = self._get_db_path()
        images_path     = self._get_images_path()
        institution_uid  = self.get_config_value("InstitutionUID") or ""
        institution_name = self.get_config_value("InstitutionName") or ""

        if not os.path.isfile(db_path_remote):
            raise FileNotFoundError(f"VistaSoft DB not found: {db_path_remote}")

        # FbBridge embedded can only open local paths — copy to temp if UNC/network
        from datasources.fb_client import _get_db_copy
        import shutil as _shutil

        _is_network = db_path_remote.startswith("\\\\") or (
            len(db_path_remote) > 1 and db_path_remote[1] != ":"
        )
        if _is_network:
            self.logger.info("  Network path detected — copying DB to local temp for FbBridge")
            db_path    = _get_db_copy(db_path_remote)
            _db_is_temp = True
        else:
            db_path    = db_path_remote
            _db_is_temp = False

        written = skipped = errors = images_written = images_missing = 0
        total = len(patients)

        def _q(s, n=1020):
            return (s or "").replace("'", "''")[:n]

        def _dt(s):
            if not s:
                return "NULL"
            s2 = _re.sub(r'T', ' ', s).rstrip('Z').strip()
            s2 = _re.sub(r'\.\d+.*$', '', s2).strip()
            return f"'{s2}'" if s2 else "NULL"

        for i, patient in enumerate(patients):
            if cancel_flag():
                break

            given  = (patient.get("given_names") or "").strip()
            family = (patient.get("family_name") or "").strip()
            name   = f"{given} {family}".strip()
            dtx_id = patient.get("dtx_id") or patient.get("uid", "")

            try:
                # ── Dedup check ───────────────────────────────────────────
                if incremental:
                    found = _run("find_patient", db_path, dtx_id)
                    if found.get("uid"):
                        skipped += 1
                        if progress_callback:
                            progress_callback(i+1, total, f"Skipped: {name}")
                        continue

                # ── Insert PATIENT row ────────────────────────────────────
                p_uid = str(_uuid_mod.uuid4())
                ph    = _uuid_to_ms_hex(p_uid)
                sex   = {"MALE":77,"FEMALE":70,"OTHER":79}.get(
                          (patient.get("gender") or "").upper(), "NULL")
                bd    = (f"'{patient.get('date_of_birth') or patient.get('birth_date')}'"
                         if (patient.get("date_of_birth") or patient.get("birth_date"))
                         else "NULL")
                pms   = _q(patient.get("pms_id") or patient.get("id") or "")
                src_i = _q(dtx_id)

                _run("execute", db_path,
                     f"INSERT INTO PATIENT"
                     f"(UID,GIVENNAMES,FAMILYNAME,BIRTHDATE,SEX,"
                     f"ID,SOURCEINSTANCEID,ISHIDDEN,NAMEMAPPINGPOLICY,SOURCE)"
                     f"VALUES(X'{ph}',"
                     f"'{_q(given)}','{_q(family)}',{bd},{sex},"
                     f"'{pms}','{src_i}',0,0,1)")
                self.logger.info(f"  Wrote patient {name} → {p_uid[:8]}…")

                # ── Create patient image folder ───────────────────────────
                folder = os.path.join(images_path, p_uid)
                os.makedirs(folder, exist_ok=True)

                # ── Write Patient.json ────────────────────────────────────
                patient_json = {
                    "PatientUID":    p_uid,
                    "PatientID":     patient.get("pms_id") or patient.get("id") or "",
                    "PatientName":   f"{family}^{given}",
                    "PatientSource": "Import",
                    "PatientSourceSystemID": dtx_id,
                    "NameMappingPolicy": "Standard",
                    "Address": {},
                    "IssuerOfPatientIDQualifiersSequence": [],
                    "OtherPatientIDsSequence": [],
                }
                with open(os.path.join(folder, "Patient.json"), "w", encoding="utf-8-sig") as fh:
                    _json.dump(patient_json, fh, indent=2)

                # ── Process studies ───────────────────────────────────────
                for study in patient.get("studies", {}).values():
                    s_uid = str(_uuid_mod.uuid4())
                    sh    = _uuid_to_ms_hex(s_uid)
                    siuid = _q(study.get("study_instance") or str(_uuid_mod.uuid4()), 256)

                    sdt = study.get("study_datetime") or ""
                    if not sdt:
                        fm = next((m for ser in study.get("series", {}).values()
                                   for m in ser.get("media", [])), None)
                        if fm:
                            sdt = fm.get("acq_datetime") or ""

                    _run("execute", db_path,
                         f"INSERT INTO STUDY(UID,PATIENTUID,STUDYINSTANCEUID,STUDYDATETIME)"
                         f"VALUES(X'{sh}',X'{ph}','{siuid}',{_dt(sdt)})")

                    # ── Write Study JSON ──────────────────────────────────
                    study_dt   = sdt or ""
                    study_date = _re.sub(r'[-T :].*', '', study_dt).replace('-', '')[:8]
                    study_time = ""
                    if "T" in study_dt:
                        study_time = study_dt.split("T")[1].rstrip("Z").replace(":", "")[:6]
                    study_json = {
                        "StudyInstanceUID": siuid,
                        "StudyDate":        study_date,
                        "StudyTime":        study_time,
                    }
                    with open(os.path.join(folder, f"Study_{s_uid}.json"),
                              "w", encoding="utf-8-sig") as fh:
                        _json.dump(study_json, fh, indent=2)

                    # ── Process media ─────────────────────────────────────
                    for series in study.get("series", {}).values():
                        for media in series.get("media", []):
                            if cancel_flag():
                                break

                            m_uid   = str(_uuid_mod.uuid4())
                            mh      = _uuid_to_ms_hex(m_uid)
                            dtx_mid = media.get("uid", "")

                            dtx_modality = (media.get("modality") or "")
                            iclass       = (media.get("image_class") or
                                            _modality_to_class(dtx_modality))
                            acq_dt       = media.get("acq_datetime") or ""
                            sop          = media.get("sop_instance") or str(_uuid_mod.uuid4())

                            _dtx_to_dicom_code = {
                                "PANORAMIC": "PX", "CEPHALOGRAM": "DX", "INTRAORAL": "IO",
                                "VOLUME": "DX", "PICTURE": "XC", "MOVIE": "XC",
                            }
                            dicom_modality = (
                                (media.get("dicom_tags") or {}).get("modality")
                                or _dtx_to_dicom_code.get(dtx_modality.upper(), "XC")
                            )

                            # ── Fetch raw file data ───────────────────────
                            data = media.get("_file_data")
                            if data is None:
                                fn = media.get("_fetch")
                                if fn:
                                    try:
                                        data = fn()
                                    except Exception as fe:
                                        self.logger.warning(f"    fetch failed for {dtx_mid[:8]}: {fe}")
                            if data is None:
                                fp = media.get("file_path", "")
                                if fp and os.path.isfile(fp):
                                    with open(fp, "rb") as _f:
                                        data = _f.read()
                            if not data:
                                self.logger.warning(f"    no file for {dtx_mid[:8]}… skipping")
                                images_missing += 1
                                continue

                            disk_uid  = m_uid
                            dcm_path  = os.path.join(folder, disk_uid + ".dcm")
                            json_path = os.path.join(folder, disk_uid + ".json")

                            acq_e = _re.sub(r'T', ' ', acq_dt).rstrip('Z').strip()
                            if not acq_e:
                                acq_e = "2000-01-01 00:00:00"
                            at_e = _q(_class_to_acq_type(iclass), 256)
                            mv   = 1 if (dtx_modality.upper() == "MOVIE" and
                                         media.get("content_type", "").startswith("video/")) else 0

                            # ── Convert JPEG/PNG to DICOM first ───────────
                            # Must happen before building the sidecar so we can
                            # read the actual pixel dimensions from the DICOM.
                            ct = media.get("content_type") or "application/dicom"
                            _is_jpeg = (("jpeg" in ct or "jpg" in ct)
                                        or (len(data) >= 2
                                            and data[0] == 0xFF and data[1] == 0xD8))
                            _is_png  = (("png" in ct)
                                        or (len(data) >= 4
                                            and data[:4] == b'\x89PNG'))
                            if _is_jpeg or _is_png:
                                data = _jpeg_to_dcm(data, sop_uid=sop,
                                                    patient_name=name,
                                                    acq_date=acq_dt)

                            # ── Read actual pixel geometry from DICOM ─────
                            pinfo = _read_dicom_pixel_info(data)

                            # ── Build sidecar metadata ────────────────────
                            dtags = media.get("dicom_tags") or {}
                            # JPEG/PNG sources are converted to 16-bit greyscale DICOM
                            # to match VistaSoft's native import format exactly.
                            if _is_jpeg or _is_png:
                                bits   = 16
                                spp    = 1
                                photo  = "MONOCHROME2"
                                cdepth = "Gray16"
                            else:
                                bits  = dtags.get("bitsAllocated") or dtags.get("bitsStored") or pinfo["bits"]
                                spp   = dtags.get("samplesPerPixel") or pinfo["spp"] or 1
                                photo = (dtags.get("photometricInterpretation")
                                         or pinfo["photo"] or "MONOCHROME2")
                                if photo in ("RGB", "YBR_FULL", "YBR_FULL_422") or spp == 3:
                                    cdepth = f"Rgb{bits * spp}"
                                elif bits == 16:
                                    cdepth = "Gray16"
                                else:
                                    cdepth = f"Gray{bits}"

                            acq_date = ""
                            acq_time = ""
                            if acq_dt:
                                dt_clean = _re.sub(r'\.\d+.*$', '', acq_dt).rstrip('Z').strip()
                                if 'T' in dt_clean:
                                    _dp, _tp = dt_clean.split('T', 1)
                                    acq_date = _dp.replace('-', '')[:8]
                                    acq_time = _tp.replace(':', '')[:6]
                                elif ' ' in dt_clean:
                                    _dp, _tp = dt_clean.split(' ', 1)
                                    acq_date = _dp.replace('-', '')[:8]
                                    acq_time = _tp.replace(':', '')[:6]
                                else:
                                    acq_date = dt_clean.replace('-', '')[:8]

                            sidecar = {
                                "SpecificCharacterSet": ["ISO_IR 192"],
                                "SOPClassUID":       ("1.2.840.10008.5.1.4.1.1.1"
                                                      if (_is_jpeg or _is_png)
                                                      else dtags.get("sopClassUid", "1.2.840.10008.5.1.4.1.1.7")),
                                "SOPInstanceUID":    sop,
                                "AcquisitionDate":   acq_date,
                                "AcquisitionDateTime": acq_date + acq_time,
                                "AcquisitionTime":   acq_time,
                                "Modality":          dicom_modality,
                                "Manufacturer":      dtags.get("manufacturer") or "DUERR DENTAL",
                                "InstitutionName":   institution_name,
                                "InstitutionUID":    institution_uid,
                                "VistaSoftUid":      m_uid,
                                "ImageClass":        iclass,
                                "ImageSource":       "FileImport",
                                "AcquisitionTypeName": _class_to_acq_type(iclass),
                                "OriginalCodec":     "DICOM",
                                "OriginalColorDepth": cdepth,
                                "ImageCompression":  "Lossless",
                                "XrayStationUID":    "00000000-0000-0000-0000-000000000000",
                                "XrayParametersConfirmed": True,
                                "PresentationStates": [
                                    {
                                        "PresentationStateType": "Last",
                                        "Warnings": "None",
                                        "Brightness": 0.5, "Contrast": 0.5, "Gamma": 1.0,
                                        "Invert": False,
                                        "LimitWhite": 0.0, "LimitBlack": 0.0,
                                        "WindowingMode": "Static",
                                        "WindowCenterRelative": 0.5, "WindowWidthRelative": 1.0,
                                        "WindowFunction": "Linear",
                                        "WindowingAlgorithm": "VISTACONNECT_V1",
                                        "CoordinateSystem": "World",
                                        "FilterChain": [], "AfterLoadFilters": [],
                                        "Annotations": [],
                                        "MirrorMode": "None", "Rotation": 0,
                                        "CariesDetection": {"DetectedCariesPredictions": []},
                                    }
                                ],
                                "IsProofBoosted":    False,
                                "PreferredPresentationStateRole": "BurnedIn",
                                "FilterResult":      0,
                                "RisWorkflowEnabled": False,
                                "AdditionalScanInfo": {},
                                "StudyInstanceUID":  siuid,
                                "SeriesInstanceUID": dtags.get("seriesInstanceUid"),
                                "SeriesNumber":      dtags.get("seriesNumber") or 1,
                                "InstanceNumber":    1,
                                "SamplesPerPixel":   spp,
                                "PhotometricInterpretation": photo,
                                "Rows":    dtags.get("rows") or pinfo["rows"],
                                "Columns": dtags.get("columns") or pinfo["cols"],
                                "BitsAllocated":     bits,
                                "BitsStored":        bits,
                                "HighBit":           bits - 1,
                                "PixelRepresentation": dtags.get("pixelRepresentation") or 0,
                                "ImageLaterality":   "B",
                                "RescaleIntercept":  0.0,
                                "RescaleSlope":      1.0,
                                "RescaleType":       "US",
                                "LossyImageCompression": "00",
                                "ContributingEquipmentSequence": [],
                                "ResponsiblePerson": None,
                                "PatientUID":        p_uid,
                                "PatientName":       f"{family}^{given}",
                                "PatientID":         patient.get("pms_id") or patient.get("id") or "",
                                "PatientBirthDate":  (patient.get("date_of_birth") or patient.get("birth_date") or "").replace("-", "")[:8],
                                "PatientSex":        {"MALE":"M","FEMALE":"F","OTHER":"O"}.get(
                                    (patient.get("gender") or "").upper(), ""),
                            }
                            jsondata_str = _json.dumps(sidecar, ensure_ascii=False)

                            # ── Insert IMAGE row ──────────────────────────
                            try:
                                _run("execute", db_path,
                                     f"INSERT INTO IMAGE"
                                     f"(UID,STUDYUID,SOPINSTANCEUID,IMAGECLASS,"
                                     f"ACQUISITIONDATETIME,ACQUISITIONTECHNOLOGY,"
                                     f"ACQUISITIONTYPENAME,ISMOVIE,ISEXTERNAL,ISHIDDEN,"
                                     f"SOURCE,XRAYSTATIONUID,JSONDATA)"
                                     f"VALUES(X'{mh}',X'{sh}',"
                                     f"'{_q(sop,256)}','{_q(iclass,256)}',"
                                     f"'{acq_e}',1,'{at_e}',{mv},1,0,1,"
                                     f"X'00000000000000000000000000000000',"
                                     f"'{_q(jsondata_str, 65000)}')")
                            except Exception as dbe:
                                self.logger.error(f"    IMAGE insert failed: {dbe}")
                                continue

                            # ── Write DICOM file to disk ──────────────────
                            with open(dcm_path, "wb") as fh:
                                fh.write(data)

                            # ── Write preview (.jpg alongside the image) ──
                            preview_fn = media.get("_fetch_preview")
                            if preview_fn:
                                try:
                                    preview_data = preview_fn()
                                    if preview_data:
                                        with open(os.path.join(folder, disk_uid + ".jpg"), "wb") as fh:
                                            fh.write(preview_data)
                                except Exception as pe:
                                    self.logger.debug(f"      Preview fetch failed: {pe}")

                            # ── Write sidecar JSON to disk ────────────────
                            with open(json_path, "w", encoding="utf-8-sig") as fh:
                                _json.dump(sidecar, fh, indent=2, ensure_ascii=False)

                            # ── Insert PRESENTATIONSTATE rows ─────────────
                            if iclass == "Dvt":
                                _processing_obj = {
                                    "$type": "Duerr.Imaging.Contracts.ImageProcessing3DInfo, Duerr.Imaging.Contracts",
                                    "Filters": [], "FilterScriptName": None,
                                    "Brightness": 0.5, "Contrast": 0.5, "Gamma": 1.0,
                                    "AutoWindowingEnabled": False,
                                    "LimitWhite": 0.0, "LimitBlack": 0.0,
                                    "WindowCenter": 0.5, "WindowWidth": 1.0, "WindowFunction": "Linear",
                                    "CoordinateSystem": "World", "AfterLoadFilters": [],
                                }
                            else:
                                _processing_obj = {
                                    "$type": "Duerr.Imaging.Contracts.ImageProcessing2DInfo, Duerr.Imaging.Contracts",
                                    "Rotation": 0.0, "MirrorMode": "None", "CariesDetection": None,
                                    "CariesConfidenceThreshold": None, "Filters": [], "FilterScriptName": None,
                                    "Brightness": 0.5, "Contrast": 0.5, "Gamma": 1.0, "Invert": False,
                                    "AutoWindowingEnabled": False, "LimitWhite": 0.0, "LimitBlack": 0.0,
                                    "WindowCenter": 0.5, "WindowWidth": 1.0, "WindowFunction": "Linear",
                                    "BlackMask": {"IsEnabled": False, "GrayScaleValue": 2570},
                                    "Annotations": [], "CoordinateSystem": "World", "AfterLoadFilters": [],
                                }
                            processing_json = _json.dumps(_processing_obj, ensure_ascii=False)
                            now_str = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            for ps_type, ps_sop, ps_series in [
                                (1, _q(sop, 256), _q(dtags.get("seriesInstanceUid") or "", 256)),
                                (2, None, None),
                                (3, None, None),
                            ]:
                                ps_uid = str(_uuid_mod.uuid4())
                                ps_h   = _uuid_to_ms_hex(ps_uid)
                                if ps_sop:
                                    sop_col = "SOPINSTANCEUID,"
                                    sop_val = f"'{ps_sop}',"
                                    ser_col = "SERIESINSTANCEUID,"
                                    ser_val = f"'{ps_series}',"
                                else:
                                    sop_col = sop_val = ser_col = ser_val = ""
                                try:
                                    _run("execute", db_path,
                                         f"INSERT INTO PRESENTATIONSTATE"
                                         f"(UID,IMAGEUID,TYPE,CREATIONDATETIME,PROCESSING,WARNINGS,"
                                         f"{sop_col}{ser_col}NAME)"
                                         f"VALUES(X'{ps_h}',X'{mh}',{ps_type},'{now_str}',"
                                         f"'{_q(processing_json, 65000)}',0,"
                                         f"{sop_val}{ser_val}NULL)")
                                except Exception as pe:
                                    self.logger.warning(f"      PRESENTATIONSTATE insert failed (type={ps_type}): {pe}")

                            images_written += 1
                            self.logger.info(f"      ✓ {iclass} → {disk_uid[:8]}….dcm")

                written += 1
                if progress_callback:
                    progress_callback(i+1, total, f"Imported: {name}")

            except Exception as exc:
                errors += 1
                self.logger.error(f"  Failed {name}: {exc}")
                if progress_callback:
                    progress_callback(i+1, total, f"Failed: {name}")

        # Copy temp DB back to remote if we used a local copy
        if _db_is_temp and os.path.isfile(db_path):
            try:
                _shutil.copy2(db_path, db_path_remote)
                self.logger.info(f"  Copied temp DB back to {db_path_remote}")
            except Exception as _ce:
                self.logger.error(f"  Failed to copy DB back: {_ce}")
            finally:
                try:
                    os.remove(db_path)
                except Exception:
                    pass

        self.logger.info(
            f"VistaSoft write complete — {written} written, {skipped} skipped, "
            f"{errors} errors, {images_written} images")
        return {
            "success":        written,
            "failed":         errors,
            "media_uploaded": images_written,
            "media_missing":  images_missing,
            "errors":         [],
        }