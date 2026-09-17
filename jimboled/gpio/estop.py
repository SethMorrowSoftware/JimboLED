"""Emergency stop: latching lock-out for relay outputs.

``All off`` is a convenience – it releases everything, and the next tap of a
button turns it straight back on.  An **emergency stop** is different: it
*latches*.  While a zone is engaged every relay it covers is held off and every
attempt to energise one – from any phone, any scene, the REST API – is refused
until somebody explicitly resets it.

Three things can engage a stop:

* the big red button in the dashboard header (a *zone* chosen by the user),
* a physical button wired to a GPIO **input** (see :class:`EStopInput`),
* the software itself, if a hardware input becomes unreadable.

Zones
-----
A zone says *what* a stop covers:

``all``      every switch (the built-in master stop, always present)
``group``    every switch whose ``interlock_group`` is listed in ``refs``
``switch``   the switches whose ids are listed in ``refs``

So an RV can have one master stop plus a "Bed" zone and an "Awning" zone, and
stopping the awning does not lock out the bed.

Latch state lives in ``estop.json`` beside ``config.json`` rather than in the
configuration itself: it is runtime state, it must survive a crash or a restart
(a lock-out that quietly clears itself when the service restarts is not a
lock-out), and it must not churn the configuration backups.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..atomicio import write_json_atomic
from .common import GPIOError, as_bool, check_pin

log = logging.getLogger(__name__)

MASTER_ZONE_ID = "all"
SCOPES = ("all", "group", "switch")
PULLS = ("up", "down")
STATE_VERSION = 1
# How long a hardware input must read "tripped" before we latch.  Two
# consecutive watchdog ticks (~200 ms) debounce contact bounce without making
# a real press feel sluggish.
INPUT_DEBOUNCE_TICKS = 2


def _new_input_state(tripped: bool = False) -> Dict[str, Any]:
    """Per-input bookkeeping.

    ``tripped`` is what the pin currently says and decides whether a reset is
    allowed; ``reported`` is whether that trip has already been handed to the
    manager, so a button that is *already* held when we open the pin still
    latches on the first poll.
    """
    return {"tripped": tripped, "reported": False, "error": "",
            "streak": INPUT_DEBOUNCE_TICKS if tripped else 0, "value": None}


@dataclass
class EStopZone:
    """A named group of relays that one emergency stop covers."""

    id: str
    name: str
    scope: str = "switch"
    refs: List[str] = field(default_factory=list)
    icon: str = "stop"
    color: str = ""
    builtin: bool = False

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EStopZone":
        if not isinstance(raw, dict):
            raise GPIOError("emergency stop zone must be an object")
        zid = str(raw.get("id") or "").strip()
        if not zid:
            raise GPIOError("emergency stop zone id is required")
        if zid == MASTER_ZONE_ID:
            raise GPIOError(f"'{MASTER_ZONE_ID}' is the built-in master stop and cannot be redefined")
        scope = str(raw.get("scope") or "switch")
        if scope not in SCOPES:
            raise GPIOError(f"emergency stop scope must be one of {', '.join(SCOPES)}")
        refs = raw.get("refs") or []
        if not isinstance(refs, list):
            raise GPIOError("emergency stop refs must be a list")
        refs = [str(r).strip() for r in refs if str(r).strip()]
        if scope in ("group", "switch") and not refs:
            raise GPIOError("pick at least one switch or interlock group for this stop")
        return cls(
            id=zid,
            name=str(raw.get("name") or zid).strip()[:60],
            scope=scope,
            refs=refs[:64],
            icon=str(raw.get("icon") or "stop")[:32],
            color=str(raw.get("color") or "")[:16],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "scope": self.scope, "refs": list(self.refs),
                "icon": self.icon, "color": self.color}

    def covers(self, switch_cfg) -> bool:
        """True when ``switch_cfg`` (a :class:`~jimboled.gpio.manager.SwitchConfig`) is locked out by this zone."""
        if self.scope == "all":
            return True
        if self.scope == "group":
            return bool(switch_cfg.interlock_group) and switch_cfg.interlock_group in self.refs
        return switch_cfg.id in self.refs


@dataclass
class EStopInput:
    """A physical emergency-stop button wired to a GPIO input.

    The safe wiring is a **normally-closed** mushroom button between the pin and
    ground, with the internal pull-up enabled: the closed contact holds the pin
    at 0 V, and *anything* that breaks the loop – a press, a pulled plug, a
    chewed cable – lets the pull-up take the pin high and latches the stop.
    """

    id: str
    name: str
    pin: int
    zone: str = MASTER_ZONE_ID
    normally_closed: bool = True
    pull: str = "up"
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EStopInput":
        if not isinstance(raw, dict):
            raise GPIOError("emergency stop input must be an object")
        iid = str(raw.get("id") or "").strip()
        if not iid:
            raise GPIOError("emergency stop input id is required")
        name = str(raw.get("name") or "Emergency stop").strip()[:60]
        pin = check_pin(raw.get("pin"), name)
        pull = str(raw.get("pull") or "up")
        if pull not in PULLS:
            raise GPIOError(f"{name}: pull must be 'up' or 'down'")
        return cls(
            id=iid,
            name=name,
            pin=pin,
            zone=str(raw.get("zone") or MASTER_ZONE_ID).strip()[:40] or MASTER_ZONE_ID,
            normally_closed=as_bool(raw.get("normally_closed"), True),
            pull=pull,
            enabled=as_bool(raw.get("enabled"), True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "pin": self.pin, "zone": self.zone,
                "normally_closed": self.normally_closed, "pull": self.pull, "enabled": self.enabled}

    @property
    def healthy_value(self) -> int:
        """The ``gpiozero`` input value seen when the button is *not* tripped.

        gpiozero reports ``value == 1`` for "active", which is a LOW pin when a
        pull-up is used and a HIGH pin with a pull-down.  Either way a closed
        normally-closed loop reads 1, so a single rule covers both wirings.
        """
        return 1 if self.normally_closed else 0


def validate_zones(raw_list: Any) -> List[EStopZone]:
    if not isinstance(raw_list, list):
        raise GPIOError("emergency stop zones must be a list")
    seen: Set[str] = set()
    out: List[EStopZone] = []
    for raw in raw_list:
        zone = EStopZone.from_dict(raw)
        if zone.id in seen:
            raise GPIOError(f"duplicate emergency stop zone id {zone.id}")
        seen.add(zone.id)
        out.append(zone)
    return out


def validate_inputs(raw_list: Any, *, taken_pins: Optional[Dict[int, str]] = None) -> List[EStopInput]:
    if not isinstance(raw_list, list):
        raise GPIOError("emergency stop inputs must be a list")
    taken = dict(taken_pins or {})
    seen_ids: Set[str] = set()
    out: List[EStopInput] = []
    for raw in raw_list:
        item = EStopInput.from_dict(raw)
        if item.id in seen_ids:
            raise GPIOError(f"duplicate emergency stop input id {item.id}")
        if item.pin in taken:
            raise GPIOError(f"GPIO{item.pin} is already used by '{taken[item.pin]}'")
        seen_ids.add(item.id)
        taken[item.pin] = item.name
        out.append(item)
    return out


class LatchStore:
    """Tiny atomic JSON file holding which zones are currently latched."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                log.error("emergency stop state unreadable (%s); assuming nothing latched", exc)
                return {}
            engaged = raw.get("engaged") if isinstance(raw, dict) else None
            if not isinstance(engaged, dict):
                return {}
            out: Dict[str, Dict[str, Any]] = {}
            for zone_id, info in engaged.items():
                if not isinstance(zone_id, str) or not isinstance(info, dict):
                    continue
                out[zone_id[:40]] = {
                    "since": float(info.get("since") or time.time()),
                    "reason": str(info.get("reason") or "")[:200],
                    "source": str(info.get("source") or "")[:40],
                }
            return out

    def save(self, engaged: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            try:
                # Durable, not merely atomic: a latch that a power cut can undo
                # is not a latch, and a power cut is the likeliest thing to
                # happen while an emergency is being written down.
                write_json_atomic(self.path, {"version": STATE_VERSION, "engaged": engaged}, indent=2)
            except OSError as exc:
                # Losing the file must never stop us latching in memory.
                log.error("could not persist emergency stop state: %s", exc)


class EStopController:
    """Latch state, zone lookup and hardware-input polling.

    The controller never touches relays – :class:`~jimboled.gpio.manager.GPIOManager`
    owns the output pins and does that itself.  Every method here is called with
    the manager's lock held, so the controller needs no lock of its own beyond
    the one inside :class:`LatchStore`.
    """

    def __init__(self, state_path: Path):
        self.store = LatchStore(state_path)
        self.zones: List[EStopZone] = []
        self.inputs: List[EStopInput] = []
        self.master_name = "All relays"
        self.confirm_engage = False
        self._engaged: Dict[str, Dict[str, Any]] = {}
        self._devices: Dict[str, Any] = {}
        self._input_state: Dict[str, Dict[str, Any]] = {}
        self._factory = None
        self.simulated = False
        self._loaded = False

    # ------------------------------------------------------------- lifecycle
    def load(self) -> None:
        """Restore latches persisted by a previous run (once, at start-up)."""
        if self._loaded:
            return
        self._engaged = self.store.load()
        self._loaded = True
        if self._engaged:
            log.warning("Emergency stop still latched from a previous run: %s",
                        ", ".join(sorted(self._engaged)))

    def configure(self, cfg: Dict[str, Any], factory: Any, simulated: bool = False) -> None:
        """Apply the ``gpio.estop`` configuration section and (re)open inputs."""
        cfg = cfg or {}
        self.master_name = str(cfg.get("master_name") or "All relays")[:60]
        self.confirm_engage = bool(cfg.get("confirm_engage", False))
        # A section that will not validate keeps whatever was already working.
        # Throwing the inputs away instead would answer "this configuration is
        # wrong" by disarming the physical emergency stop, which is the one
        # outcome worse than refusing the edit.
        try:
            self.zones = validate_zones(cfg.get("zones", []))
        except GPIOError as exc:
            log.error("Invalid emergency stop zones ignored, keeping the previous ones: %s", exc)
        try:
            self.inputs = validate_inputs(cfg.get("inputs", []))
        except GPIOError as exc:
            log.error("Invalid emergency stop inputs ignored, keeping the previous ones: %s", exc)
        # Drop latches for zones that no longer exist so the UI cannot show a
        # stop nobody is able to reset.
        known = {z.id for z in self.all_zones()}
        for zone_id in [z for z in self._engaged if z not in known]:
            log.warning("Emergency stop zone '%s' was removed; clearing its latch", zone_id)
            self._engaged.pop(zone_id, None)
            self._persist()
        self._open_inputs(factory, simulated)

    def close(self) -> None:
        for dev in self._devices.values():
            try:
                dev.close()
            except Exception:
                pass
        self._devices.clear()
        self._input_state.clear()

    def _open_inputs(self, factory: Any, simulated: bool = False) -> None:
        self.close()
        self._factory = factory
        self.simulated = bool(simulated)
        for item in self.inputs:
            # Start every input at "tripped": until we have actually read the
            # pin we do not know whether a button is being held, and a reset
            # that slips through the gap is a lock-out somebody cleared while
            # standing on the emergency stop.
            state = _new_input_state(tripped=True)
            self._input_state[item.id] = state
            if not item.enabled:
                # Disabled on purpose, so it blocks nothing.
                state.update(tripped=False, streak=0, error="disabled")
                continue
            if factory is None:
                state["error"] = "GPIO library unavailable"
                continue
            try:
                from gpiozero import DigitalInputDevice

                dev = DigitalInputDevice(item.pin, pull_up=(item.pull == "up"), pin_factory=factory)
                if self.simulated:
                    # A simulated pin floats at the pull level, which for the
                    # recommended normally-closed wiring reads as "tripped" –
                    # i.e. a laptop would boot straight into a latch nobody can
                    # clear.  Start the mock pin at its healthy level instead,
                    # so simulation behaves like a correctly wired button and
                    # tests can drive it the other way to fake a press.
                    self._drive_mock(dev, item)
                self._devices[item.id] = dev
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                log.error("Could not open emergency stop input GPIO%s ('%s'): %s",
                          item.pin, item.name, state["error"])
        # One read now, so the state above is the truth rather than a guess by
        # the time anybody asks whether a reset is allowed.  Trips found here
        # are reported by the next poll(), which is what latches them.
        self._prime_inputs()

    def _prime_inputs(self) -> None:
        """Read every freshly opened input once so its state is real, not assumed."""
        for item in self.inputs:
            dev = self._devices.get(item.id)
            state = self._input_state.get(item.id)
            if dev is None or state is None or not item.enabled:
                continue
            try:
                value = int(dev.value)
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                continue
            state["value"] = value
            state["error"] = ""
            tripped = value != item.healthy_value
            state["tripped"] = tripped
            state["reported"] = False
            state["streak"] = INPUT_DEBOUNCE_TICKS if tripped else 0

    @staticmethod
    def _drive_mock(dev: Any, item: EStopInput) -> None:
        """Park a mock pin at the level a healthy button would hold it at."""
        pin = getattr(dev, "pin", None)
        # gpiozero reports value 1 for "active", which is a LOW pin under a
        # pull-up and a HIGH pin under a pull-down; combine that with the level
        # this button reads when healthy to get the physical level to park at.
        park_high = (item.pull == "up") != (item.healthy_value == 1)
        drive = getattr(pin, "drive_high" if park_high else "drive_low", None)
        if callable(drive):
            try:
                drive()
            except Exception:  # pragma: no cover - depends on gpiozero internals
                log.debug("could not park simulated e-stop pin %s", item.pin, exc_info=True)

    # ----------------------------------------------------------------- zones
    def all_zones(self) -> List[EStopZone]:
        master = EStopZone(id=MASTER_ZONE_ID, name=self.master_name, scope="all",
                           refs=[], icon="estop", builtin=True)
        return [master] + list(self.zones)

    def zone(self, zone_id: str) -> EStopZone:
        for z in self.all_zones():
            if z.id == zone_id:
                return z
        raise GPIOError(f"unknown emergency stop zone '{zone_id}'")

    def covering(self, switch_cfg) -> Optional[EStopZone]:
        """The engaged zone locking out ``switch_cfg``, or ``None``."""
        for z in self.all_zones():
            if z.id in self._engaged and z.covers(switch_cfg):
                return z
        return None

    def any_engaged(self) -> bool:
        return bool(self._engaged)

    def is_engaged(self, zone_id: str) -> bool:
        return zone_id in self._engaged

    # --------------------------------------------------------------- latches
    def engage(self, zone_id: str, reason: str = "", source: str = "web") -> bool:
        """Latch ``zone_id``.  Returns True when this call is what latched it."""
        zone = self.zone(zone_id)
        if zone.id in self._engaged:
            return False
        self._engaged[zone.id] = {"since": time.time(), "reason": str(reason or "")[:200],
                                  "source": str(source or "")[:40]}
        self._persist()
        log.warning("EMERGENCY STOP engaged: %s (%s)", zone.name, reason or source)
        return True

    def blocking_inputs(self, zone_id: str) -> List[str]:
        """Names of hardware buttons that must be cleared before a reset.

        A *disabled* input is not one of them – it is switched off on purpose,
        and letting it block would make the stop impossible to reset.
        """
        blocking = []
        for item in self.inputs:
            if not item.enabled or self._input_zone_id(item) != zone_id:
                continue
            state = self._input_state.get(item.id) or {}
            if state.get("tripped") or state.get("error"):
                blocking.append(item.name)
        return blocking

    def blocking_reason(self, zone_id: str) -> str:
        """A sentence naming what is holding ``zone_id`` down, or ''."""
        held, broken = [], []
        for item in self.inputs:
            if not item.enabled or self._input_zone_id(item) != zone_id:
                continue
            state = self._input_state.get(item.id) or {}
            if state.get("error"):
                broken.append(f"{item.name} ({state['error']})")
            elif state.get("tripped"):
                held.append(item.name)
        parts = []
        if held:
            parts.append(f"release the physical emergency stop first ({', '.join(held)})")
        if broken:
            parts.append(f"JimboLED cannot read {', '.join(broken)}")
        return "; ".join(parts)

    def reset(self, zone_id: str, source: str = "web") -> bool:
        """Clear ``zone_id``.  Raises if a hardware button is still holding it."""
        zone = self.zone(zone_id)
        if zone.id not in self._engaged:
            return False
        # blocking_reason() is empty exactly when blocking_inputs() is, so one
        # pass answers both "may I?" and "why not?".
        reason = self.blocking_reason(zone.id)
        if reason:
            raise GPIOError(f"Cannot reset yet \u2013 {reason}.")
        self._engaged.pop(zone.id, None)
        self._persist()
        log.warning("Emergency stop reset: %s (by %s)", zone.name, source)
        return True

    def _persist(self) -> None:
        self.store.save(dict(self._engaged))

    def _input_zone_id(self, item: EStopInput) -> str:
        known = {z.id for z in self.all_zones()}
        return item.zone if item.zone in known else MASTER_ZONE_ID

    # ---------------------------------------------------------------- polling
    def poll(self) -> List[Dict[str, str]]:
        """Read every hardware input.  Returns the trips that should latch now.

        A missing or unreadable input counts as tripped: an emergency stop that
        cannot be read is not an emergency stop you may rely on.
        """
        trips: List[Dict[str, str]] = []
        for item in self.inputs:
            if not item.enabled:
                continue
            state = self._input_state.setdefault(item.id, _new_input_state(tripped=True))
            dev = self._devices.get(item.id)
            if dev is None:
                # Record *why* as well as latching: "cannot read the button" and
                # "the button is pressed" need different things from the user.
                state["error"] = state.get("error") or "input unavailable"
                reason = state["error"]
                tripped = True
            else:
                try:
                    value = int(dev.value)
                    state["value"] = value
                    state["error"] = ""
                    tripped = value != item.healthy_value
                    reason = "button pressed" if item.normally_closed else "button closed"
                except Exception as exc:
                    state["error"] = f"{type(exc).__name__}: {exc}"
                    tripped = True
                    reason = f"input unreadable: {exc}"
            if tripped:
                state["streak"] = int(state.get("streak", 0)) + 1
            else:
                state["streak"] = 0
            # Latch after a short debounce; clear immediately so the UI shows a
            # released button at once (the latch itself stays until a reset).
            if tripped and state["streak"] >= INPUT_DEBOUNCE_TICKS:
                state["tripped"] = True
            elif not tripped:
                state["tripped"] = False
            if state["tripped"]:
                # ``reported`` rather than the previous value of ``tripped``:
                # a button already held when the pin was opened starts out
                # tripped (so it blocks a reset straight away) and must still
                # be handed over once, so it latches.
                if not state.get("reported"):
                    state["reported"] = True
                    trips.append({"zone": self._input_zone_id(item), "name": item.name, "reason": reason})
            else:
                state["reported"] = False
        return trips

    # -------------------------------------------------------------- reporting
    def snapshot(self, switch_configs=()) -> Dict[str, Any]:
        by_zone: Dict[str, List[str]] = {}
        for cfg in switch_configs:
            for z in self.all_zones():
                if z.covers(cfg):
                    by_zone.setdefault(z.id, []).append(cfg.id)
        zones = []
        for z in self.all_zones():
            latch = self._engaged.get(z.id)
            zones.append({
                "id": z.id,
                "name": z.name,
                "scope": z.scope,
                "refs": list(z.refs),
                "icon": z.icon,
                "color": z.color,
                "builtin": z.builtin,
                "engaged": latch is not None,
                "since": latch["since"] if latch else None,
                "reason": latch["reason"] if latch else "",
                "source": latch["source"] if latch else "",
                "switches": by_zone.get(z.id, []),
                "blocked_by": self.blocking_inputs(z.id) if latch else [],
                "blocked_reason": self.blocking_reason(z.id) if latch else "",
            })
        inputs = []
        for item in self.inputs:
            state = self._input_state.get(item.id) or {}
            inputs.append({**item.to_dict(),
                           "blocking": item.enabled and bool(state.get("tripped") or state.get("error")),
                           "tripped": bool(state.get("tripped")),
                           "error": state.get("error", ""),
                           "available": item.id in self._devices,
                           "simulated": self.simulated})
        return {
            "confirm_engage": self.confirm_engage,
            "master_name": self.master_name,
            "engaged": self.any_engaged(),
            "engaged_zones": sorted(self._engaged),
            "zones": zones,
            "inputs": inputs,
        }
