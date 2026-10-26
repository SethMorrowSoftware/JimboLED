"""Bedside and wall buttons: a GPIO input that runs an action.

A tap runs one action and holding the button for a second runs another –
"Goodnight" on a tap, "everything off" on a hold, say.  Wire an ordinary
push button (normally open) between the pin and a ground pin and leave the
pull-up on; or between the pin and 3.3 V with the pull-down.

This is a convenience, not a safety device: nothing latches, and the actions
go through exactly the same code as scenes and schedules, so interlocks,
emergency stops and the hold-to-run rule all still apply.  A button can never
run a bed motor (hold-to-run switches only move while somebody holds the
button *on the dashboard*); use an emergency-stop input for stopping things.

Buttons are read by the GPIO watchdog every 100 ms, which is also what
debounces them: a contact has to read "pressed" on a tick to count.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .common import GPIOError, as_bool, check_pin

log = logging.getLogger(__name__)

HOLD_S = 1.0            # held this long = a "hold" rather than a "tap"
MAX_BUTTONS = 16
PULLS = ("up", "down")


@dataclass
class ButtonInput:
    id: str
    name: str
    pin: int
    pull: str = "up"
    enabled: bool = True
    press: Optional[Dict[str, Any]] = None
    hold: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, raw: Any, clean_action: Callable[[Any], Dict[str, Any]]) -> "ButtonInput":
        if not isinstance(raw, dict):
            raise GPIOError("a button must be an object")
        bid = str(raw.get("id") or "").strip()[:40]
        if not bid:
            raise GPIOError("button id is required")
        name = str(raw.get("name") or "Button").strip()[:60] or "Button"
        pin = check_pin(raw.get("pin"), name)
        pull = str(raw.get("pull") or "up")
        if pull not in PULLS:
            raise GPIOError(f"{name}: pull must be 'up' or 'down'")
        actions = {}
        for key in ("press", "hold"):
            value = raw.get(key)
            if value in (None, {}, ""):
                actions[key] = None
                continue
            try:
                actions[key] = clean_action(value)
            except Exception as exc:  # ScheduleError / APIError: say which button and which action
                raise GPIOError(f"{name}: {'tap' if key == 'press' else 'hold'} action – {exc}")
        if not actions["press"] and not actions["hold"]:
            raise GPIOError(f"{name}: choose what a tap or a hold should do")
        return cls(id=bid, name=name, pin=pin, pull=pull, enabled=as_bool(raw.get("enabled"), True),
                   press=actions["press"], hold=actions["hold"])

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "pin": self.pin, "pull": self.pull,
                "enabled": self.enabled, "press": self.press, "hold": self.hold}


def validate_buttons(raw_list: Any, *, taken_pins: Dict[int, str],
                     clean_action: Callable[[Any], Dict[str, Any]]) -> List[ButtonInput]:
    """Check a whole button list, including pins already used by relays or stops."""
    if not isinstance(raw_list, list):
        raise GPIOError("buttons must be a list")
    if len(raw_list) > MAX_BUTTONS:
        raise GPIOError(f"at most {MAX_BUTTONS} buttons")
    taken = dict(taken_pins)
    seen = set()
    out: List[ButtonInput] = []
    for raw in raw_list:
        item = ButtonInput.from_dict(raw, clean_action)
        if item.id in seen:
            raise GPIOError(f"duplicate button id {item.id}")
        if item.pin in taken:
            raise GPIOError(f"GPIO{item.pin} is already used by '{taken[item.pin]}'")
        seen.add(item.id)
        taken[item.pin] = item.name
        out.append(item)
    return out


@dataclass
class _State:
    down: bool = False
    since: float = 0.0
    held_fired: bool = False
    presses: int = 0
    last_event: str = ""
    last_event_at: float = 0.0
    error: str = ""


class ButtonInputs:
    """Opens the button pins and turns their levels into tap/hold events."""

    def __init__(self) -> None:
        self.buttons: List[ButtonInput] = []
        self._devices: Dict[str, Any] = {}
        self._state: Dict[str, _State] = {}

    def configure(self, raw_list: Any, factory: Any, *, clean_action: Callable[[Any], Dict[str, Any]],
                  taken_pins: Dict[int, str]) -> None:
        """(Re)open every button.  A bad entry is skipped and logged, never fatal."""
        self.close()
        self.buttons = []
        taken = dict(taken_pins)
        for raw in raw_list if isinstance(raw_list, list) else []:
            try:
                item = ButtonInput.from_dict(raw, clean_action)
                if item.pin in taken:
                    raise GPIOError(f"GPIO{item.pin} is already used by '{taken[item.pin]}'")
            except Exception as exc:
                log.error("ignoring a button: %s", exc)
                continue
            taken[item.pin] = item.name
            self.buttons.append(item)
            state = self._state[item.id] = _State()
            if not item.enabled:
                state.error = "disabled"
                continue
            if factory is None:
                state.error = "GPIO library unavailable"
                continue
            try:
                from gpiozero import DigitalInputDevice

                self._devices[item.id] = DigitalInputDevice(item.pin, pull_up=(item.pull == "up"), pin_factory=factory)
            except Exception as exc:
                state.error = f"{type(exc).__name__}: {exc}"
                log.error("could not open button GPIO%s ('%s'): %s", item.pin, item.name, state.error)

    def close(self) -> None:
        for dev in self._devices.values():
            try:
                dev.close()
            except Exception:
                pass
        self._devices.clear()

    def poll(self, now: Optional[float] = None) -> List[Tuple[ButtonInput, str]]:
        """Read every button once; return ``(button, "press"|"hold")`` events."""
        now = time.monotonic() if now is None else now
        events: List[Tuple[ButtonInput, str]] = []
        for item in self.buttons:
            dev = self._devices.get(item.id)
            state = self._state.setdefault(item.id, _State())
            if dev is None:
                continue
            try:
                # gpiozero reports 1 for "active": a LOW pin with the pull-up,
                # a HIGH one with the pull-down – i.e. "pressed" either way.
                pressed = int(dev.value) == 1
                state.error = ""
            except Exception as exc:
                state.error = f"{type(exc).__name__}: {exc}"
                continue
            if pressed and not state.down:
                state.down, state.since, state.held_fired = True, now, False
            elif pressed and state.down:
                if not state.held_fired and now - state.since >= HOLD_S:
                    state.held_fired = True
                    if item.hold:
                        events.append((item, "hold"))
            elif not pressed and state.down:
                state.down = False
                if not state.held_fired or not item.hold:
                    # A tap; or a long press on a button that has no hold action.
                    if item.press:
                        events.append((item, "press"))
        for item, kind in events:
            st = self._state[item.id]
            st.presses += 1
            st.last_event, st.last_event_at = kind, time.time()
        return events

    def snapshot(self) -> List[Dict[str, Any]]:
        out = []
        for item in self.buttons:
            st = self._state.get(item.id) or _State()
            out.append({**item.to_dict(), "pressed": st.down, "available": item.id in self._devices,
                        "error": st.error, "presses": st.presses, "last_event": st.last_event,
                        "last_event_at": st.last_event_at or None})
        return out

    def simulate(self, button_id: str, kind: str) -> Tuple[ButtonInput, str]:
        """The dashboard's "Try it" for a button: the same event a press makes."""
        item = next((b for b in self.buttons if b.id == button_id), None)
        if item is None:
            raise GPIOError("unknown button")
        if kind not in ("press", "hold") or not getattr(item, kind):
            raise GPIOError(f"'{item.name}' has nothing to do on a {'tap' if kind == 'press' else 'hold'}")
        return item, kind
