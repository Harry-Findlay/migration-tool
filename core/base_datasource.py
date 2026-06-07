"""
core/base_datasource.py
=======================
Abstract base class for all datasources (sources and targets).
Unchanged from original except for to_dict() helpers needed by the REST API.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Callable
from .models import ConfigurationItem, MigrationResult, MigrationStatus
import logging


class BaseDatasource(ABC):
    def __init__(self):
        self.configuration: List[ConfigurationItem] = []
        self.logger = logging.getLogger(self.__class__.__name__)
        self._load_data: dict = {}
        self._setup_configuration()

    @abstractmethod
    def _setup_configuration(self):
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        pass

    @property
    @abstractmethod
    def role(self) -> str:
        """'source' or 'target'"""
        pass

    def get_config_value(self, key: str) -> Optional[str]:
        for item in self.configuration:
            if item.key == key:
                return item.value
        return None

    def set_config_value(self, key: str, value: str):
        for item in self.configuration:
            if item.key == key:
                item.value = value
                return

    def apply_config_dict(self, config: dict):
        """Bulk-apply a {key: value} dict — called from the API layer."""
        for key, value in config.items():
            self.set_config_value(key, str(value) if value is not None else "")

    def load_async(self, progress_callback: Optional[Callable[[str], None]] = None) -> dict:
        return {}

    def validate(self) -> tuple[bool, str]:
        for item in self.configuration:
            if not item.advanced and not item.hidden and item.config_type.value not in ("readonly",):
                if not item.value and not item.placeholder:
                    return False, f"Required field '{item.name}' is empty."
        return True, "Configuration valid."

    def get_visible_config(self, show_advanced: bool = False) -> List[ConfigurationItem]:
        return [
            c for c in self.configuration
            if not c.hidden and (show_advanced or not c.advanced)
        ]

    def config_to_dict(self, show_advanced: bool = True) -> list:
        """Serialise configuration items for the REST API."""
        return [item.to_dict() for item in self.get_visible_config(show_advanced)]
