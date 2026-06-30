"""Cisco IOS/IOS-XE/WLC parser.

Cisco Catalyst 9800 wireless controllers run IOS-XE (netmiko driver ``cisco_xe``)
and emit the same ``! Last configuration change at ...`` line as IOS / IOS-XE
routers and switches, so they share this parser.
"""

import re
from datetime import datetime
from typing import Optional, Tuple

from django.utils.timezone import make_aware

from nautobot_golden_config.utilities.config_parsers.base import ConfigParser

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# `! Last configuration change at 21:32:14 UTC Tue Jun 24 2025 by jdoe`
# The timezone abbreviation is captured but intentionally ignored (Python can't
# resolve arbitrary tz abbreviations reliably); the naive datetime is made aware
# with Django's active timezone.
_LAST_CHANGE_FULL_RE = re.compile(
    r"Last configuration change at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
    r"\S+\s+"  # tz abbreviation, ignored
    r"\w{3}\s+"  # day-of-week, ignored
    r"(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})"
    r"(?:\s+by\s+(?P<user>\S+))?",
    re.IGNORECASE,
)
# Author-only fallback for platforms that omit the full timestamp.
_AUTHOR_ONLY_RE = re.compile(r"Last configuration change.*\bby\s+(?P<user>\S+)", re.IGNORECASE)


def _build_datetime(match):
    """Build a timezone-aware datetime from a full ``_LAST_CHANGE_FULL_RE`` match."""
    month = _MONTHS.get(match.group("mon").lower())
    if not month:
        return None
    try:
        naive = datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
    except ValueError:
        return None
    return make_aware(naive)


class CiscoParser(ConfigParser):
    """Parser for Cisco IOS, IOS-XE, WLC configurations."""

    def parse(self, config_text: str) -> Tuple[Optional[str], Optional[datetime]]:
        """Parse the ``Last configuration change`` line of a Cisco config.

        Args:
            config_text (str): the (possibly cleaned) device configuration.

        Returns:
            tuple[str | None, datetime | None]: ``(author, changed_at)``. Either
            may be None when the line is missing or only partially present.
        """
        if not config_text:
            return None, None
        match = _LAST_CHANGE_FULL_RE.search(config_text)
        if match:
            return match.group("user"), _build_datetime(match)
        author_match = _AUTHOR_ONLY_RE.search(config_text)
        if author_match:
            return author_match.group("user"), None
        return None, None
