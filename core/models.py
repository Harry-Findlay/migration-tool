"""
core/models.py
==============
Shared data models and enums used across the entire application.
Extended from the original to support web API serialisation and PMS CSV handling.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import json


class ConfigurationType(Enum):
    TEXT             = "text"
    PASSWORD         = "password"
    PATH             = "path"
    SELECT           = "select"
    SWITCH           = "switch"
    READ_ONLY        = "readonly"
    CONNECTION_STRING = "connection_string"


@dataclass
class ConfigurationItem:
    key:          str
    name:         str
    config_type:  ConfigurationType
    value:        str  = ""
    description:  str  = ""
    placeholder:  str  = ""
    advanced:     bool = False
    hidden:       bool = False
    options:      dict = field(default_factory=dict)
    group:        str  = ""

    def to_dict(self) -> dict:
        return {
            "key":         self.key,
            "name":        self.name,
            "type":        self.config_type.value,
            "value":       self.value,
            "description": self.description,
            "placeholder": self.placeholder,
            "advanced":    self.advanced,
            "hidden":      self.hidden,
            "options":     self.options,
            "group":       self.group,
        }


@dataclass
class MigrationConfig:
    source_configs: dict = field(default_factory=dict)
    target_configs: dict = field(default_factory=dict)


class MigrationStatus(Enum):
    IDLE       = "idle"
    VALIDATING = "validating"
    RUNNING    = "running"
    PAUSED     = "paused"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class PatientMigrationStatus(Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    FAILED      = "failed"
    SKIPPED     = "skipped"


@dataclass
class MigrationResult:
    status:              MigrationStatus
    patients_processed:  int  = 0
    patients_skipped:    int  = 0
    patients_failed:     int  = 0
    media_uploaded:      int  = 0
    media_missing:       int  = 0
    errors:              list = field(default_factory=list)
    warnings:            list = field(default_factory=list)
    message:             str  = ""
    session_id:          Optional[str] = None
    patients_remaining:  int  = 0

    def to_dict(self) -> dict:
        return {
            "status":             self.status.value,
            "patients_processed": self.patients_processed,
            "patients_skipped":   self.patients_skipped,
            "patients_failed":    self.patients_failed,
            "media_uploaded":     self.media_uploaded,
            "media_missing":      self.media_missing,
            "errors":             self.errors,
            "warnings":           self.warnings,
            "message":            self.message,
            "session_id":         self.session_id,
            "patients_remaining": self.patients_remaining,
        }


# ── PMS CSV models ────────────────────────────────────────────────────────────

@dataclass
class PmsPatient:
    """Normalised patient record parsed from a PMS CSV export."""
    surname:     str = ""
    first_name:  str = ""
    dob:         str = ""
    nhs_number:  str = ""
    patient_ref: str = ""
    address1:    str = ""
    postcode:    str = ""
    # resolved fields — set after comparison/mapping
    resolved:    dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "surname":     self.surname,
            "first_name":  self.first_name,
            "dob":         self.dob,
            "nhs_number":  self.nhs_number,
            "patient_ref": self.patient_ref,
            "address1":    self.address1,
            "postcode":    self.postcode,
            "resolved":    self.resolved,
        }


class PmsDataSource(Enum):
    """Which data takes precedence during migration when PMS CSV is loaded."""
    PMS    = "pms"     # PMS demographics, source imaging
    SOURCE = "source"  # Everything from source system
    MERGED = "merged"  # Source primary, PMS fills empty fields
