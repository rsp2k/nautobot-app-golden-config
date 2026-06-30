"""Abstract base class for config parsers."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Tuple


class ConfigParser(ABC):
    """Base interface for platform-specific last-change parsers."""

    @abstractmethod
    def parse(self, config_text: str) -> Tuple[Optional[str], Optional[datetime]]:
        """Parse a device config for last-change metadata.

        Args:
            config_text (str): The device configuration text.

        Returns:
            tuple[str | None, datetime | None]: (author, changed_at).
            Either may be None if the config lacks that data.
        """
