"""Pin-factory selection for gpiozero.

Raspberry Pi OS Bookworm (and newer) ships ``lgpio`` as the supported way to
drive GPIO; the older ``RPi.GPIO`` no longer works on the 6.x kernels.  gpiozero
abstracts over both, so we just need to pick a working factory and fall back
to the ``mock`` factory when running on a laptop or a Pi without the libraries.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

log = logging.getLogger(__name__)

PREFERRED_FACTORIES = ("lgpio", "rpigpio", "pigpio", "native")


def is_raspberry_pi() -> bool:
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as fh:
            return "raspberry pi" in fh.read().lower()
    except OSError:
        return False


def pi_model() -> str:
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read().strip("\x00\n ")
    except OSError:
        return ""


def select_pin_factory(preference: str = "auto") -> Tuple[object, str, bool]:
    """Return ``(factory, name, simulated)``.

    ``preference`` is ``auto``, ``mock`` or an explicit gpiozero factory name.
    ``simulated`` is True when the mock factory is in use.
    """
    try:
        from gpiozero import Device  # noqa: F401
        from gpiozero.pins.mock import MockFactory
    except ImportError as exc:  # gpiozero missing entirely
        log.error("gpiozero is not installed (%s); GPIO will be unavailable", exc)
        return None, "unavailable", True

    env_pref = os.environ.get("GPIOZERO_PIN_FACTORY")
    if preference in (None, "", "auto") and env_pref:
        preference = env_pref

    if preference == "mock":
        return MockFactory(), "mock", True

    candidates = [preference] if preference not in (None, "", "auto") else list(PREFERRED_FACTORIES)
    if not is_raspberry_pi() and preference in (None, "", "auto"):
        log.info("Not running on a Raspberry Pi; using simulated GPIO")
        return MockFactory(), "mock", True

    for name in candidates:
        factory = _try_factory(name)
        if factory is not None:
            log.info("Using gpiozero pin factory: %s", name)
            return factory, name, False
    log.warning("No working GPIO pin factory found (tried %s); using simulation", candidates)
    return MockFactory(), "mock", True


def _atomic_lgpio_factory():
    """LGPIOFactory whose pins claim outputs *with* their initial level.

    Stock gpiozero claims the line as an output (which drives it LOW) and only
    then writes the requested level.  On an active-low relay board that LOW
    blip energises the relay for a few microseconds every time the service
    starts.  Claiming with the level in one ioctl avoids it.
    """
    import lgpio
    from gpiozero.pins.lgpio import LGPIOFactory, LGPIOPin

    class AtomicLGPIOPin(LGPIOPin):
        def output_with_state(self, state):
            if getattr(self, "_callback", None) is not None:
                self._callback.cancel()
                self._callback = None
            lgpio.gpio_claim_output(self.factory._handle, self._number, int(bool(state)))

    class AtomicLGPIOFactory(LGPIOFactory):
        def __init__(self, chip=None):
            super().__init__(chip)
            self.pin_class = AtomicLGPIOPin

    return AtomicLGPIOFactory()


def _try_factory(name: str) -> Optional[object]:
    try:
        if name == "lgpio":
            try:
                return _atomic_lgpio_factory()
            except Exception as exc:  # pragma: no cover - depends on gpiozero internals
                log.debug("atomic lgpio factory unavailable (%s); using stock", exc)
            from gpiozero.pins.lgpio import LGPIOFactory as F
        elif name == "rpigpio":
            from gpiozero.pins.rpigpio import RPiGPIOFactory as F
        elif name == "pigpio":
            from gpiozero.pins.pigpio import PiGPIOFactory as F
        elif name == "native":
            from gpiozero.pins.native import NativeFactory as F
        else:
            log.warning("Unknown pin factory %r", name)
            return None
        return F()
    except Exception as exc:  # ImportError, OSError (permissions), RuntimeError
        log.debug("pin factory %s unavailable: %s", name, exc)
        return None
