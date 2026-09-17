"""Shared GPIO primitives: errors, pin tables and lenient value parsing.

This module exists so the switch manager (:mod:`jimboled.gpio.manager`) and the
emergency-stop controller (:mod:`jimboled.gpio.estop`) can share the pin tables
and error types without importing each other.  ``manager`` re-exports every
name here, so ``from jimboled.gpio.manager import GPIOError`` keeps working.
"""
from __future__ import annotations

from typing import Any, Dict


class GPIOError(Exception):
    """Raised for invalid switch configuration or disallowed actions."""


class EStopEngaged(GPIOError):
    """Raised when an action is refused because an emergency stop is latched.

    Carries the zone so the UI can point at the right reset button.
    """

    def __init__(self, message: str, zone_id: str = "", zone_name: str = ""):
        super().__init__(message)
        self.zone_id = zone_id
        self.zone_name = zone_name


def as_bool(value: Any, default: bool = False) -> bool:
    """Lenient boolean: accepts JSON bools, 0/1 and 'true'/'false'/'on'/'off' strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "t", "y"):
        return True
    if text in ("0", "false", "no", "off", "f", "n", ""):
        return False
    raise GPIOError(f"'{value}' is not a valid yes/no value")


# BCM pins exposed on the 40-pin header, with notes for the pin picker.
PIN_NOTES: Dict[int, str] = {
    0: "Reserved: HAT ID EEPROM (do not use)",
    1: "Reserved: HAT ID EEPROM (do not use)",
    2: "I2C SDA – has a fixed pull-up resistor",
    3: "I2C SCL – has a fixed pull-up resistor",
    4: "General purpose (1-Wire by default if enabled)",
    5: "General purpose",
    6: "General purpose",
    7: "SPI CE1 (fine if SPI is disabled)",
    8: "SPI CE0 (fine if SPI is disabled)",
    9: "SPI MISO (fine if SPI is disabled)",
    10: "SPI MOSI (fine if SPI is disabled)",
    11: "SPI SCLK (fine if SPI is disabled)",
    12: "General purpose (PWM0)",
    13: "General purpose (PWM1)",
    14: "UART TX – avoid if the serial console is enabled",
    15: "UART RX – avoid if the serial console is enabled",
    16: "General purpose",
    17: "General purpose – recommended",
    18: "General purpose (PCM/PWM)",
    19: "General purpose (PCM)",
    20: "General purpose (PCM)",
    21: "General purpose (PCM)",
    22: "General purpose – recommended",
    23: "General purpose – recommended",
    24: "General purpose – recommended",
    25: "General purpose – recommended",
    26: "General purpose",
    27: "General purpose – recommended",
}
RECOMMENDED_PINS = (17, 27, 22, 23, 24, 25, 5, 6, 12, 13, 16, 19, 20, 21, 26)
RESERVED_PINS = (0, 1)
# Power-on default pulls (BCM2835/6/7): GPIO0-8 pull UP, GPIO9-27 pull DOWN.
# Until the firmware applies config.txt (a few seconds) the pin sits at this
# level, so an active-LOW relay board is safest on a pull-up pin (4, 5, 6) and
# an active-HIGH board on a pull-down pin (9-27).
PULL_UP_AT_BOOT = frozenset(range(0, 9))

# Physical header position for each BCM pin (40-pin header, J8).
PHYSICAL_PIN: Dict[int, int] = {
    2: 3, 3: 5, 4: 7, 14: 8, 15: 10, 17: 11, 18: 12, 27: 13, 22: 15, 23: 16,
    24: 18, 10: 19, 9: 21, 25: 22, 11: 23, 8: 24, 7: 26, 0: 27, 1: 28, 5: 29,
    6: 31, 12: 32, 13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40,
}


def check_pin(pin: Any, label: str = "pin") -> int:
    """Validate a BCM pin number for use as an input or output."""
    try:
        value = int(pin)
    except (TypeError, ValueError):
        raise GPIOError(f"{label} must be a BCM number (e.g. 17)")
    if value not in PIN_NOTES:
        raise GPIOError(f"{label}: GPIO{value} is not on the 40-pin header")
    if value in RESERVED_PINS:
        raise GPIOError(f"{label}: GPIO{value} is reserved for the HAT EEPROM")
    return value
