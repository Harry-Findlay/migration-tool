"""
datasources/dicom_header.py
===========================
Minimal, dependency-free DICOM header reader.

Reads only the handful of tags the sources need (demographics, UIDs, dates,
modality / SOP class, image geometry) and stops at Pixel Data, so a 500 MB
CBCT volume costs the same as a 2 MB intraoral. Values are skipped with
seek(), never read into memory.

Supports:
  - Part 10 files (128-byte preamble + "DICM") and preamble-less legacy files
  - Explicit VR Little Endian, Implicit VR Little Endian, Explicit VR Big Endian
  - Encapsulated (JPEG / JPEG 2000 / RLE) transfer syntaxes — header only
  - Undefined-length sequences (skipped, including nested items)

Not supported (returns None / meta only):
  - Deflated Explicit VR (1.2.840.10008.1.2.1.99) — effectively never seen in dental

Usage:
    tags = read_dicom_header(path, fallback_encoding="windows-1252")
    if tags is None: not a DICOM file
"""

import os
import struct
from typing import BinaryIO, Optional

# keyword → (group, element)
WANTED = {
    "SpecificCharacterSet":      (0x0008, 0x0005),
    "ImageType":                 (0x0008, 0x0008),
    "SOPClassUID":               (0x0008, 0x0016),
    "SOPInstanceUID":            (0x0008, 0x0018),
    "StudyDate":                 (0x0008, 0x0020),
    "SeriesDate":                (0x0008, 0x0021),
    "AcquisitionDate":           (0x0008, 0x0022),
    "ContentDate":               (0x0008, 0x0023),
    "StudyTime":                 (0x0008, 0x0030),
    "SeriesTime":                (0x0008, 0x0031),
    "AcquisitionTime":           (0x0008, 0x0032),
    "ContentTime":               (0x0008, 0x0033),
    "AccessionNumber":           (0x0008, 0x0050),
    "Modality":                  (0x0008, 0x0060),
    "PresentationIntentType":    (0x0008, 0x0068),
    "Manufacturer":              (0x0008, 0x0070),
    "ReferringPhysicianName":    (0x0008, 0x0090),
    "StationName":               (0x0008, 0x1010),
    "StudyDescription":          (0x0008, 0x1030),
    "SeriesDescription":         (0x0008, 0x103E),
    "ManufacturerModelName":     (0x0008, 0x1090),
    "PatientName":               (0x0010, 0x0010),
    "PatientID":                 (0x0010, 0x0020),
    "PatientBirthDate":          (0x0010, 0x0030),
    "PatientSex":                (0x0010, 0x0040),
    "BodyPartExamined":          (0x0018, 0x0015),
    "DeviceSerialNumber":        (0x0018, 0x1000),
    "ProtocolName":              (0x0018, 0x1030),
    "ImagerPixelSpacing":        (0x0018, 0x1164),
    "ViewPosition":              (0x0018, 0x5101),
    "StudyInstanceUID":          (0x0020, 0x000D),
    "SeriesInstanceUID":         (0x0020, 0x000E),
    "InstanceNumber":            (0x0020, 0x0013),
    "PhotometricInterpretation": (0x0028, 0x0004),
    "NumberOfFrames":            (0x0028, 0x0008),
    "Rows":                      (0x0028, 0x0010),
    "Columns":                   (0x0028, 0x0011),
    "BitsStored":                (0x0028, 0x0101),
}
_BY_TAG = {v: k for k, v in WANTED.items()}
_US_TAGS = {(0x0028, 0x0010), (0x0028, 0x0011), (0x0028, 0x0101)}

# Person-name / free-text VRs are decoded with the character set; UIDs etc. are ASCII
_TEXT_TAGS = {(0x0010, 0x0010), (0x0008, 0x0090), (0x0008, 0x1030),
              (0x0008, 0x103E), (0x0008, 0x1010), (0x0018, 0x1030),
              (0x0008, 0x0070), (0x0008, 0x1090)}

_LONG_VRS = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV",
             b"UC", b"UN", b"UR", b"UT", b"UV"}
_UNDEFINED = 0xFFFFFFFF

_ITEM       = (0xFFFE, 0xE000)
_ITEM_DELIM = (0xFFFE, 0xE00D)
_SEQ_DELIM  = (0xFFFE, 0xE0DD)
_PIXEL_DATA = (0x7FE0, 0x0010)

_CHARSETS = {
    "ISO_IR 6":   "ascii",
    "ISO_IR 100": "latin-1",
    "ISO_IR 101": "iso-8859-2",
    "ISO_IR 144": "iso-8859-5",
    "ISO_IR 148": "iso-8859-9",
    "ISO_IR 192": "utf-8",
    "GB18030":    "gb18030",
}

_TS_IMPLICIT_LE = "1.2.840.10008.1.2"
_TS_EXPLICIT_BE = "1.2.840.10008.1.2.2"
_TS_DEFLATED    = "1.2.840.10008.1.2.1.99"


class _Stop(Exception):
    pass


def is_dicom_file(path: str) -> bool:
    """Cheap check: Part 10 magic, or a legacy file starting with group 0008."""
    try:
        with open(path, "rb") as f:
            head = f.read(132)
    except OSError:
        return False
    if len(head) >= 132 and head[128:132] == b"DICM":
        return True
    return len(head) >= 8 and head[:2] in (b"\x08\x00", b"\x02\x00") and head[2:4] == b"\x00\x00"


def read_dicom_header(path: str, fallback_encoding: str = "windows-1252") -> Optional[dict]:
    """
    Return {keyword: str} for the WANTED tags present in the file, plus
    "TransferSyntaxUID". Rows/Columns/BitsStored are returned as str(int).
    Returns None if the file is not DICOM.
    """
    try:
        with open(path, "rb") as f:
            return _parse(f, fallback_encoding)
    except _Stop:
        return None
    except (OSError, struct.error):
        return None


def _parse(f: BinaryIO, fallback_encoding: str) -> Optional[dict]:
    raw: dict = {}
    head = f.read(132)
    if len(head) >= 132 and head[128:132] == b"DICM":
        ts = _read_meta(f)
    elif len(head) >= 8 and head[:2] in (b"\x08\x00", b"\x02\x00"):
        f.seek(0)
        # Legacy (no preamble). Guess explicit vs implicit from bytes 4–5.
        ts = "1.2.840.10008.1.2.1" if head[4:6].isalpha() and head[4:6].isupper() else _TS_IMPLICIT_LE
    else:
        return None

    out = {"TransferSyntaxUID": ts}
    if ts == _TS_DEFLATED:
        return out

    explicit = ts != _TS_IMPLICIT_LE
    endian   = ">" if ts == _TS_EXPLICIT_BE else "<"
    _walk(f, explicit, endian, raw, top_level=True, end=None)

    cs = (raw.get((0x0008, 0x0005)) or b"").decode("ascii", "replace")
    cs = cs.split("\\")[-1].strip().strip("\x00")
    enc = _CHARSETS.get(cs, fallback_encoding)

    for tag, value in raw.items():
        key = _BY_TAG[tag]
        if tag in _US_TAGS:
            if len(value) >= 2:
                out[key] = str(struct.unpack(endian + "H", value[:2])[0])
            continue
        codec = enc if tag in _TEXT_TAGS else "latin-1"
        try:
            text = value.decode(codec, "replace")
        except LookupError:
            text = value.decode("latin-1", "replace")
        out[key] = text.strip("\x00 ").strip()
    return out


def _read_meta(f: BinaryIO) -> str:
    """Read group 0002 (always explicit VR LE). Returns transfer syntax UID."""
    ts = "1.2.840.10008.1.2.1"
    while True:
        pos = f.tell()
        hdr = f.read(6)
        if len(hdr) < 6:
            return ts
        group, elem = struct.unpack("<HH", hdr[:4])
        if group != 0x0002:
            f.seek(pos)
            return ts
        vr = hdr[4:6]
        if vr in _LONG_VRS:
            f.read(2)
            length = struct.unpack("<I", f.read(4))[0]
        else:
            length = struct.unpack("<H", f.read(2))[0]
        value = f.read(length)
        if elem == 0x0010:
            ts = value.decode("ascii", "replace").strip("\x00 ")


def _read_element_header(f: BinaryIO, explicit: bool, endian: str):
    hdr = f.read(4)
    if len(hdr) < 4:
        return None
    group, elem = struct.unpack(endian + "HH", hdr)
    tag = (group, elem)
    if group == 0xFFFE:                      # item / delimiters: never have a VR
        length = struct.unpack(endian + "I", f.read(4))[0]
        return tag, None, length
    if explicit:
        vr = f.read(2)
        if vr in _LONG_VRS:
            f.read(2)
            length = struct.unpack(endian + "I", f.read(4))[0]
        else:
            length = struct.unpack(endian + "H", f.read(2))[0]
    else:
        vr = None
        length = struct.unpack(endian + "I", f.read(4))[0]
    return tag, vr, length


def _walk(f: BinaryIO, explicit: bool, endian: str, raw: dict,
          top_level: bool, end: Optional[int]):
    """Walk elements until EOF, `end` offset, or an item delimiter."""
    while end is None or f.tell() < end:
        el = _read_element_header(f, explicit, endian)
        if el is None:
            return
        tag, vr, length = el

        if tag == _ITEM_DELIM:
            return
        if top_level and tag == _PIXEL_DATA:
            return

        if length == _UNDEFINED:
            # Undefined length ⇒ sequence (SQ or UN containing SQ) or
            # encapsulated pixel data. Either way: a list of items.
            _skip_items(f, explicit, endian)
            continue

        if top_level and tag in _BY_TAG:
            raw[tag] = f.read(length)
        else:
            f.seek(length, os.SEEK_CUR)


def _skip_items(f: BinaryIO, explicit: bool, endian: str):
    """Skip a list of items terminated by a sequence delimiter."""
    while True:
        el = _read_element_header(f, explicit, endian)
        if el is None:
            return
        tag, _vr, length = el
        if tag == _SEQ_DELIM:
            return
        if tag != _ITEM:
            raise _Stop()   # malformed — give up rather than misparse
        if length == _UNDEFINED:
            _walk(f, explicit, endian, {}, top_level=False, end=None)
        else:
            f.seek(length, os.SEEK_CUR)
