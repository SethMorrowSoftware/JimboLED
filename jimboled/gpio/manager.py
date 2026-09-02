"""GPIO switch manager.

Each configured *switch* maps to one output pin (BCM numbering) that drives a
relay.  Three modes are supported:

``toggle``     stays on until turned off (or until ``max_on_seconds`` expires)
``momentary``  hold-to-run: the browser must send a heartbeat while the button
               is held; if heartbeats stop the relay is released automatically
``pulse``      energise for ``pulse_ms`` then release (emulates a button tap)

Safety rules (all enforced here, never in the browser):

* switches in the same ``interlock_group`` can never be on simultaneously; the
  opposing relay is turned off and a dead-time elapses before the new one
  energises
* ``max_on_seconds`` caps any on-time (0 disables the cap for toggle switches)
* every relay is driven off at start-up, at shutdown and on process exit
* a watchdog thread re-checks the rules every 100 ms
"""
from __future__ import annotations

import atexit
import collections
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from .backend import pi_model, select_pin_factory

log = logging.getLogger(__name__)

MODES = ("toggle", "momentary", "pulse")
WATCHDOG_TICK_S = 0.1
DEFAULT_PULSE_MS = 500
DEFAULT_MAX_ON = {"toggle": 0, "momentary": 60, "pulse": 0}

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


class GPIOError(Exception):
    """Raised for invalid switch configuration or disallowed actions."""


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


@dataclass
class SwitchConfig:
    id: str
    name: str
    pin: int
    mode: str = "toggle"
    active_high: bool = True
    pulse_ms: int = DEFAULT_PULSE_MS
    max_on_seconds: float = 0
    interlock_group: str = ""
    icon: str = "power"
    color: str = ""
    confirm: bool = False
    enabled: bool = True
    notes: str = ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SwitchConfig":
        if not isinstance(raw, dict):
            raise GPIOError("switch must be an object")
        sid = str(raw.get("id") or "").strip()
        if not sid:
            raise GPIOError("switch id is required")
        name = str(raw.get("name") or sid).strip()[:60]
        try:
            pin = int(raw.get("pin"))
        except (TypeError, ValueError):
            raise GPIOError(f"{name}: pin must be a BCM number (e.g. 17)")
        if pin not in PIN_NOTES:
            raise GPIOError(f"{name}: GPIO{pin} is not on the 40-pin header")
        if pin in RESERVED_PINS:
            raise GPIOError(f"{name}: GPIO{pin} is reserved for the HAT EEPROM")
        mode = str(raw.get("mode") or "toggle")
        if mode not in MODES:
            raise GPIOError(f"{name}: mode must be one of {', '.join(MODES)}")
        try:
            pulse_ms = int(raw.get("pulse_ms", DEFAULT_PULSE_MS))
        except (TypeError, ValueError):
            raise GPIOError(f"{name}: pulse_ms must be a number")
        pulse_ms = max(20, min(pulse_ms, 60_000))
        try:
            max_on = float(raw.get("max_on_seconds", DEFAULT_MAX_ON[mode]))
        except (TypeError, ValueError):
            raise GPIOError(f"{name}: max_on_seconds must be a number")
        max_on = max(0.0, min(max_on, 24 * 3600))
        if mode == "momentary" and max_on == 0:
            # A held button always gets a cap; a stuck client must not run a motor forever.
            max_on = DEFAULT_MAX_ON["momentary"]
        return cls(
            id=sid,
            name=name,
            pin=pin,
            mode=mode,
            active_high=as_bool(raw.get("active_high"), True),
            pulse_ms=pulse_ms,
            max_on_seconds=max_on,
            interlock_group=str(raw.get("interlock_group") or "").strip()[:40],
            icon=str(raw.get("icon") or "power")[:32],
            color=str(raw.get("color") or "")[:16],
            confirm=as_bool(raw.get("confirm"), False),
            enabled=as_bool(raw.get("enabled"), True),
            notes=str(raw.get("notes") or "")[:200],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "pin": self.pin,
            "mode": self.mode,
            "active_high": self.active_high,
            "pulse_ms": self.pulse_ms,
            "max_on_seconds": self.max_on_seconds,
            "interlock_group": self.interlock_group,
            "icon": self.icon,
            "color": self.color,
            "confirm": self.confirm,
            "enabled": self.enabled,
            "notes": self.notes,
        }


def validate_switches(raw_list: List[Dict[str, Any]]) -> List[SwitchConfig]:
    """Validate a full switch list (unique ids and pins)."""
    if not isinstance(raw_list, list):
        raise GPIOError("switches must be a list")
    seen_ids, seen_pins = set(), {}
    out: List[SwitchConfig] = []
    for raw in raw_list:
        cfg = SwitchConfig.from_dict(raw)
        if cfg.id in seen_ids:
            raise GPIOError(f"duplicate switch id {cfg.id}")
        if cfg.pin in seen_pins:
            raise GPIOError(f"GPIO{cfg.pin} is used by both '{seen_pins[cfg.pin]}' and '{cfg.name}'")
        seen_ids.add(cfg.id)
        seen_pins[cfg.pin] = cfg.name
        out.append(cfg)
    return out


@dataclass
class _Runtime:
    cfg: SwitchConfig
    device: Any = None            # gpiozero OutputDevice (or None if failed)
    error: str = ""
    on: bool = False
    since: float = 0.0          # monotonic
    since_wall: float = 0.0     # wall clock, for display only
    last_heartbeat: float = 0.0  # monotonic
    hold_token: str = ""
    auto_off_at: float = 0.0
    source: str = ""
    last_off_reason: str = ""
    pulse_timer: Optional[threading.Timer] = None
    total_on_time: float = 0.0
    activations: int = 0


class GPIOManager:
    """Owns all output pins.  One instance per process."""

    def __init__(self, config_store, *, on_event=None):
        self.store = config_store
        self._lock = threading.RLock()
        self._switches: Dict[str, _Runtime] = {}
        self._group_block_until: Dict[str, float] = {}
        self._events: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        self._factory = None
        self.factory_name = "unavailable"
        self.simulated = True
        self._stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._on_event = on_event
        self.started = False

    # ---------------------------------------------------------------- setup
    def start(self) -> None:
        cfg = self.store.section("gpio") or {}
        self._factory, self.factory_name, self.simulated = select_pin_factory(cfg.get("backend", "auto"))
        self._dead_time = float(cfg.get("interlock_dead_time_ms", 250)) / 1000.0
        self._hold_timeout = float(cfg.get("hold_timeout_s", 1.5))
        self._apply_config(cfg.get("switches", []))
        self._stop.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="gpio-watchdog", daemon=True)
        self._watchdog.start()
        self.store.on_change(self._on_config_change)
        atexit.register(self.stop)
        self.started = True
        log.info("GPIO manager started (%s%s) with %d switch(es)",
                 self.factory_name, " / simulated" if self.simulated else "", len(self._switches))

    def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        self._stop.set()
        with self._lock:
            for rt in self._switches.values():
                self._set_off(rt, "shutdown")
                self._close_device(rt)
            self._switches.clear()
        log.info("GPIO manager stopped; all relays released")

    def _on_config_change(self, before: Dict[str, Any], after: Dict[str, Any]) -> None:
        g_before, g_after = before.get("gpio", {}), after.get("gpio", {})
        if g_before == g_after:
            return
        self._dead_time = float(g_after.get("interlock_dead_time_ms", 250)) / 1000.0
        self._hold_timeout = float(g_after.get("hold_timeout_s", 1.5))
        if g_before.get("backend") != g_after.get("backend"):
            with self._lock:
                for rt in self._switches.values():
                    self._set_off(rt, "reconfigure")
                    self._close_device(rt)
                self._switches.clear()
                old = self._factory
                self._factory = None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self._factory, self.factory_name, self.simulated = select_pin_factory(g_after.get("backend", "auto"))
        self._apply_config(g_after.get("switches", []))

    def _apply_config(self, raw_switches: List[Dict[str, Any]]) -> None:
        try:
            configs = validate_switches(raw_switches)
        except GPIOError as exc:
            log.error("Invalid GPIO configuration ignored: %s", exc)
            return
        with self._lock:
            wanted = {c.id: c for c in configs}
            # Remove or recreate switches whose hardware settings changed.
            for sid in list(self._switches):
                rt = self._switches[sid]
                new = wanted.get(sid)
                if new is None or (new.pin, new.active_high, new.enabled) != (rt.cfg.pin, rt.cfg.active_high, rt.cfg.enabled):
                    self._set_off(rt, "reconfigure")
                    self._close_device(rt)
                    del self._switches[sid]
                else:
                    if new.mode != rt.cfg.mode and rt.on:
                        self._set_off(rt, "reconfigure")
                    rt.cfg = new
            for sid, cfg in wanted.items():
                if sid not in self._switches:
                    self._switches[sid] = self._create_runtime(cfg)
            # A changed interlock group must never leave two members energised.
            groups: Dict[str, List[_Runtime]] = {}
            for rt in self._switches.values():
                if rt.on and rt.cfg.interlock_group:
                    groups.setdefault(rt.cfg.interlock_group, []).append(rt)
            for members in groups.values():
                if len(members) > 1:
                    for rt in members:
                        self._set_off(rt, "reconfigure-interlock")

    def _create_runtime(self, cfg: SwitchConfig) -> _Runtime:
        rt = _Runtime(cfg=cfg)
        if not cfg.enabled:
            rt.error = "disabled"
            return rt
        if self._factory is None:
            rt.error = "GPIO library unavailable"
            return rt
        try:
            from gpiozero import OutputDevice

            rt.device = OutputDevice(cfg.pin, active_high=cfg.active_high, initial_value=False,
                                     pin_factory=self._factory)
        except Exception as exc:  # pin busy, permission denied, bad pin
            rt.error = f"{type(exc).__name__}: {exc}"
            log.error("Could not open GPIO%s for '%s': %s", cfg.pin, cfg.name, rt.error)
        return rt

    def _close_device(self, rt: _Runtime) -> None:
        if rt.pulse_timer:
            rt.pulse_timer.cancel()
            rt.pulse_timer = None
        if rt.device is not None:
            try:
                rt.device.off()
                rt.device.close()
            except Exception:
                pass
            rt.device = None

    # -------------------------------------------------------------- helpers
    def _get(self, switch_id: str) -> _Runtime:
        rt = self._switches.get(switch_id)
        if rt is None:
            raise GPIOError(f"unknown switch '{switch_id}'")
        if not rt.cfg.enabled:
            raise GPIOError(f"'{rt.cfg.name}' is disabled")
        if rt.device is None:
            raise GPIOError(f"'{rt.cfg.name}' is unavailable: {rt.error or 'no pin'}")
        return rt

    def _record(self, rt: _Runtime, action: str, source: str, reason: str = "") -> None:
        evt = {"ts": time.time(), "switch": rt.cfg.id, "name": rt.cfg.name,
               "action": action, "source": source, "reason": reason}
        self._events.append(evt)
        if self._on_event:
            try:
                self._on_event(evt)
            except Exception:
                log.exception("gpio event callback failed")

    def _energise(self, switch_id: str, source: str) -> _Runtime:
        """Turn a switch on, honouring interlocks and the group dead time.

        The dead-time wait happens *outside* the lock so heartbeats, releases
        and the watchdog keep running for every other switch meanwhile.
        """
        deadline = time.monotonic() + 10.0
        while True:
            with self._lock:
                rt = self._get(switch_id)
                if rt.on:
                    return rt
                group = rt.cfg.interlock_group
                wait = 0.0
                if group:
                    for other in self._switches.values():
                        if other is not rt and other.cfg.interlock_group == group and other.on:
                            self._set_off(other, f"interlock:{rt.cfg.id}")
                    wait = self._group_block_until.get(group, 0.0) - time.monotonic()
                if wait <= 0:
                    self._set_on(rt, source)
                    return rt
            if time.monotonic() > deadline:
                raise GPIOError(f"'{switch_id}' could not be energised: interlock dead time never elapsed")
            time.sleep(min(wait, 0.25))

    def _set_on(self, rt: _Runtime, source: str) -> None:
        """Energise ``rt``.  Caller holds the lock and has cleared interlocks."""
        if rt.on:
            return
        rt.device.on()
        now = time.monotonic()
        rt.on = True
        rt.since = now
        rt.since_wall = time.time()
        rt.last_heartbeat = now
        rt.source = source
        rt.activations += 1
        rt.last_off_reason = ""
        self._record(rt, "on", source)

    def _set_off(self, rt: _Runtime, reason: str) -> None:
        """De-energise ``rt``.  Caller holds the lock.  Always safe to call."""
        if rt.pulse_timer:
            rt.pulse_timer.cancel()
            rt.pulse_timer = None
        if rt.device is not None:
            try:
                rt.device.off()
            except Exception as exc:
                log.error("failed to turn off GPIO%s: %s", rt.cfg.pin, exc)
        if rt.on:
            rt.total_on_time += time.monotonic() - rt.since
            rt.on = False
            rt.hold_token = ""
            rt.auto_off_at = 0.0
            rt.last_off_reason = reason
            if rt.cfg.interlock_group:
                self._group_block_until[rt.cfg.interlock_group] = time.monotonic() + self._dead_time
            self._record(rt, "off", rt.source, reason)

    # -------------------------------------------------------------- actions
    def turn_on(self, switch_id: str, source: str = "api") -> Dict[str, Any]:
        with self._lock:
            rt = self._get(switch_id)
            if rt.cfg.mode == "momentary":
                raise GPIOError(f"'{rt.cfg.name}' is a hold-to-run switch; use press/heartbeat/release")
            if rt.cfg.mode == "pulse":
                return self.pulse(switch_id, source=source)
        rt = self._energise(switch_id, source)
        with self._lock:
            return self._snapshot_one(rt)

    def turn_off(self, switch_id: str, source: str = "api") -> Dict[str, Any]:
        with self._lock:
            rt = self._switches.get(switch_id)
            if rt is None:
                raise GPIOError(f"unknown switch '{switch_id}'")
            self._set_off(rt, source)
            return self._snapshot_one(rt)

    def toggle(self, switch_id: str, source: str = "api") -> Dict[str, Any]:
        with self._lock:
            rt = self._get(switch_id)
            if rt.on:
                return self.turn_off(switch_id, source)
            return self.turn_on(switch_id, source)

    def pulse(self, switch_id: str, duration_ms: Optional[int] = None, source: str = "api") -> Dict[str, Any]:
        with self._lock:
            rt = self._get(switch_id)
            try:
                ms = int(duration_ms) if duration_ms else rt.cfg.pulse_ms
            except (TypeError, ValueError):
                raise GPIOError("duration_ms must be a number")
            ms = max(20, min(ms, 60_000))
        self._energise(switch_id, source)
        with self._lock:
            rt = self._get(switch_id)
            rt.auto_off_at = time.monotonic() + ms / 1000.0
            if rt.pulse_timer:
                rt.pulse_timer.cancel()
            timer = threading.Timer(ms / 1000.0, self._pulse_done, args=(switch_id,))
            timer.daemon = True
            rt.pulse_timer = timer
            timer.start()
            return self._snapshot_one(rt)

    def _pulse_done(self, switch_id: str) -> None:
        with self._lock:
            rt = self._switches.get(switch_id)
            if rt and rt.on:
                self._set_off(rt, "pulse-complete")

    def press(self, switch_id: str, source: str = "api") -> Dict[str, Any]:
        """Start holding a momentary switch.  Returns snapshot incl. hold token."""
        with self._lock:
            rt = self._get(switch_id)
            if rt.cfg.mode != "momentary":
                raise GPIOError(f"'{rt.cfg.name}' is not a hold-to-run switch")
        self._energise(switch_id, source)
        with self._lock:
            rt = self._get(switch_id)
            token = secrets.token_urlsafe(12)
            rt.hold_token = token
            rt.last_heartbeat = time.monotonic()
            snap = self._snapshot_one(rt)
            snap["token"] = token
            return snap

    def heartbeat(self, switch_id: str, token: str) -> Dict[str, Any]:
        with self._lock:
            rt = self._get(switch_id)
            if not rt.on or not token or token != rt.hold_token:
                snap = self._snapshot_one(rt)
                snap["held"] = False
                return snap
            rt.last_heartbeat = time.monotonic()
            snap = self._snapshot_one(rt)
            snap["held"] = True
            return snap

    def release(self, switch_id: str, token: str = "", source: str = "api") -> Dict[str, Any]:
        with self._lock:
            rt = self._switches.get(switch_id)
            if rt is None:
                raise GPIOError(f"unknown switch '{switch_id}'")
            # A release is always honoured, even with a stale token: safety first.
            self._set_off(rt, f"release:{source}")
            return self._snapshot_one(rt)

    def all_off(self, source: str = "api") -> Dict[str, Any]:
        with self._lock:
            for rt in self._switches.values():
                self._set_off(rt, f"all-off:{source}")
            return self.snapshot()

    def test_pin(self, pin: int, active_high: bool, duration_ms: int = 300) -> bool:
        """Briefly pulse a pin so the user can identify the relay."""
        duration_ms = max(20, min(int(duration_ms), 2000))
        with self._lock:
            for rt in self._switches.values():
                if rt.cfg.pin == pin and rt.device is not None:
                    configured = rt.cfg.id
                    break
            else:
                configured = None
            if configured is None:
                if self._factory is None:
                    raise GPIOError("GPIO library unavailable")
                if pin not in PIN_NOTES or pin in RESERVED_PINS:
                    raise GPIOError(f"GPIO{pin} cannot be used")
                from gpiozero import OutputDevice

                dev = OutputDevice(pin, active_high=active_high, initial_value=False, pin_factory=self._factory)
                dev.on()
        if configured is not None:
            self.pulse(configured, duration_ms, source="test")
            return True
        try:
            time.sleep(duration_ms / 1000.0)
        finally:
            with self._lock:
                try:
                    dev.off()
                finally:
                    dev.close()
        return True

    # ------------------------------------------------------------- watchdog
    def _watchdog_loop(self) -> None:
        while not self._stop.wait(WATCHDOG_TICK_S):
            try:
                self._watchdog_tick()
            except Exception:
                log.exception("gpio watchdog error")

    def _watchdog_tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            for rt in self._switches.values():
                cfg = rt.cfg
                if rt.on:
                    if cfg.mode == "momentary" and now - rt.last_heartbeat > self._hold_timeout:
                        log.warning("'%s' released: no heartbeat for %.1fs", cfg.name, now - rt.last_heartbeat)
                        self._set_off(rt, "heartbeat-timeout")
                    elif cfg.max_on_seconds and now - rt.since > cfg.max_on_seconds:
                        log.warning("'%s' released: max on-time %.1fs reached", cfg.name, cfg.max_on_seconds)
                        self._set_off(rt, "max-on-time")
                    elif cfg.mode == "pulse" and rt.auto_off_at and now > rt.auto_off_at + 0.5:
                        self._set_off(rt, "pulse-complete")
                # Sanity: hardware must agree with our state.  A switch we believe
                # is OFF but whose pin is still driving the relay is forced off.
                if rt.device is not None:
                    try:
                        hw_on = bool(rt.device.value)
                        if hw_on != rt.on:
                            if rt.on:
                                rt.device.on()
                            else:
                                log.error("GPIO%s ('%s') was still energised while marked off; forcing off", cfg.pin, cfg.name)
                                rt.device.off()
                    except Exception as exc:
                        log.error("watchdog could not verify GPIO%s: %s", cfg.pin, exc)

    # ------------------------------------------------------------- reporting
    def _snapshot_one(self, rt: _Runtime) -> Dict[str, Any]:
        now = time.monotonic()
        cfg = rt.cfg
        remaining = None
        if rt.on and cfg.max_on_seconds:
            remaining = max(0.0, cfg.max_on_seconds - (now - rt.since))
        return {
            "id": cfg.id,
            "name": cfg.name,
            "pin": cfg.pin,
            "mode": cfg.mode,
            "on": rt.on,
            "since": rt.since_wall if rt.on else None,
            "on_for": round(now - rt.since, 1) if rt.on else 0,
            "remaining": round(remaining, 1) if remaining is not None else None,
            "available": rt.device is not None,
            "error": rt.error,
            "enabled": cfg.enabled,
            "icon": cfg.icon,
            "color": cfg.color,
            "confirm": cfg.confirm,
            "interlock_group": cfg.interlock_group,
            "max_on_seconds": cfg.max_on_seconds,
            "pulse_ms": cfg.pulse_ms,
            "last_off_reason": rt.last_off_reason,
            "activations": rt.activations,
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "backend": self.factory_name,
                "simulated": self.simulated,
                "pi_model": pi_model(),
                "hold_timeout_s": self._hold_timeout if self.started else None,
                "switches": [self._snapshot_one(rt) for rt in self._switches.values()],
            }

    def events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def pin_map(self) -> List[Dict[str, Any]]:
        with self._lock:
            in_use = {rt.cfg.pin: rt.cfg.name for rt in self._switches.values()}
        out = []
        for bcm, note in sorted(PIN_NOTES.items()):
            out.append({
                "bcm": bcm,
                "physical": PHYSICAL_PIN.get(bcm),
                "note": note,
                "recommended": bcm in RECOMMENDED_PINS,
                "reserved": bcm in RESERVED_PINS,
                "boot_pull": "up" if bcm in PULL_UP_AT_BOOT else "down",
                "in_use_by": in_use.get(bcm),
            })
        return out
