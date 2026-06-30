"""Config parser registry and generic fallback.

Maps a Nautobot platform ``network_driver`` (e.g. ``cisco_ios``, ``cisco_xe``)
to a platform-specific parser for the config's last-change metadata. When no
driver-specific parser is registered, ``GenericParser`` tries every registered
parser in turn so an unknown/blank driver still degrades gracefully.
"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from nautobot_golden_config.utilities.config_parsers.base import ConfigParser
from nautobot_golden_config.utilities.config_parsers.cisco import CiscoParser

_PARSER_REGISTRY: Dict[str, ConfigParser] = {}


def register_parser(driver_name: str, parser: ConfigParser) -> None:
    """Register a parser for a network driver name."""
    _PARSER_REGISTRY[driver_name] = parser


class GenericParser(ConfigParser):
    """Fallback parser that tries all registered parsers in sequence."""

    def parse(self, config_text: str) -> Tuple[Optional[str], Optional[datetime]]:
        """Try each registered parser until one returns non-None results."""
        for parser in _PARSER_REGISTRY.values():
            result = parser.parse(config_text)
            if result != (None, None):
                return result
        return None, None


def get_parser(network_driver: Optional[str]) -> ConfigParser:
    """Retrieve a parser for the given network driver.

    Args:
        network_driver (str | None): Network driver name (e.g., "cisco_ios").

    Returns:
        ConfigParser: A driver-specific parser if registered; otherwise GenericParser.
    """
    if network_driver and network_driver in _PARSER_REGISTRY:
        return _PARSER_REGISTRY[network_driver]
    return GenericParser()


# Register default parsers. Cisco Catalyst 9800 WLCs run IOS-XE and report as
# ``cisco_xe``; legacy AireOS controllers report as ``cisco_wlc``.
_cisco_parser = CiscoParser()
register_parser("cisco_ios", _cisco_parser)
register_parser("cisco_xe", _cisco_parser)
register_parser("cisco_wlc", _cisco_parser)
