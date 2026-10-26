"""GPIO switch endpoints."""
from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint

from .. import get_ctx
from ..config import new_id
from ..gpio.manager import SwitchConfig, validate_switches
from . import APIError, body, ok
from .dashboard import ensure_tile, remove_tile

bp = Blueprint("gpio", __name__)


def _check_estop_pin(cfg: Dict[str, Any], pin: int) -> None:
    """Refuse a relay pin that an emergency-stop *input* or a button already owns.

    Driving that pin as an output would fight the button's pull resistor and
    silently disarm the stop, so the clash is rejected rather than resolved.
    """
    for item in (cfg.get("gpio", {}).get("estop", {}) or {}).get("inputs", []):
        if item.get("pin") == pin:
            raise APIError(f"GPIO{pin} is used by the emergency stop button '{item.get('name') or item.get('id')}'")
    for item in cfg.get("gpio", {}).get("buttons", []) or []:
        if isinstance(item, dict) and item.get("pin") == pin:
            raise APIError(f"GPIO{pin} is used by the button '{item.get('name') or item.get('id')}'")


def taken_pins(cfg: Dict[str, Any], skip_button: str = "") -> Dict[int, str]:
    """Pins used by relays and emergency-stop inputs (and other buttons)."""
    out: Dict[int, str] = {}
    g = cfg.get("gpio", {}) or {}
    for s in g.get("switches", []) or []:
        if isinstance(s, dict) and isinstance(s.get("pin"), int):
            out[s["pin"]] = str(s.get("name") or s.get("id"))
    for i in (g.get("estop") or {}).get("inputs", []) or []:
        if isinstance(i, dict) and isinstance(i.get("pin"), int):
            out[i["pin"]] = f"{i.get('name') or 'emergency stop'} (emergency stop input)"
    for b in g.get("buttons", []) or []:
        if isinstance(b, dict) and isinstance(b.get("pin"), int) and b.get("id") != skip_button:
            out[b["pin"]] = f"{b.get('name') or 'button'} (button)"
    return out


@bp.get("/gpio")
def gpio_status():
    return ok(get_ctx().gpio.snapshot())


@bp.get("/gpio/pins")
def gpio_pins():
    return ok({"pins": get_ctx().gpio.pin_map()})


@bp.get("/gpio/events")
def gpio_events():
    return ok({"events": get_ctx().gpio.events(100)})


@bp.get("/gpio/switches")
def list_switches():
    return ok({"switches": (get_ctx().store.section("gpio") or {}).get("switches", [])})


@bp.post("/gpio/switches")
def add_switch():
    ctx = get_ctx()
    data = body()
    data = dict(data)
    data["id"] = new_id("sw")
    cfg = SwitchConfig.from_dict(data)

    def mutate(c):
        switches = c["gpio"].setdefault("switches", [])
        _check_estop_pin(c, cfg.pin)
        validate_switches(switches + [cfg.to_dict()])
        switches.append(cfg.to_dict())
        ensure_tile(c, "switch", cfg.id, cfg.name)

    ctx.store.update(mutate, backup_reason="add-switch")
    _apply_boot_pins(ctx)
    return ok({"switch": cfg.to_dict(), "gpio": ctx.gpio.snapshot()}, 201)


@bp.put("/gpio/switches/<switch_id>")
@bp.patch("/gpio/switches/<switch_id>")
def update_switch(switch_id):
    ctx = get_ctx()
    data = body()

    def mutate(c):
        switches = c["gpio"].setdefault("switches", [])
        for i, s in enumerate(switches):
            if s.get("id") == switch_id:
                merged = {**s, **{k: v for k, v in data.items() if k != "id"}}
                cfg = SwitchConfig.from_dict(merged)
                _check_estop_pin(c, cfg.pin)
                candidate = switches[:i] + [cfg.to_dict()] + switches[i + 1:]
                validate_switches(candidate)
                switches[i] = cfg.to_dict()
                for tile in c["dashboard"].get("tiles", []):
                    if tile.get("type") == "switch" and tile.get("ref") == switch_id:
                        tile["title"] = cfg.name
                return
        raise APIError("unknown switch", 404)

    ctx.store.update(mutate, backup_reason="edit-switch")
    _apply_boot_pins(ctx)
    return ok({"gpio": ctx.gpio.snapshot()})


@bp.delete("/gpio/switches/<switch_id>")
def delete_switch(switch_id):
    ctx = get_ctx()

    def mutate(c):
        switches = c["gpio"].get("switches", [])
        if not any(s.get("id") == switch_id for s in switches):
            raise APIError("unknown switch", 404)
        c["gpio"]["switches"] = [s for s in switches if s.get("id") != switch_id]
        remove_tile(c, "switch", switch_id)

    ctx.store.update(mutate, backup_reason="remove-switch")
    _apply_boot_pins(ctx)
    return ok({"gpio": ctx.gpio.snapshot()})


@bp.post("/gpio/switches/<switch_id>/action")
def switch_action(switch_id):
    ctx = get_ctx()
    data = body()
    action = str(data.get("action") or "").lower()
    token = str(data.get("token") or "")
    source = "web"
    gpio = ctx.gpio
    if action == "on":
        result = gpio.turn_on(switch_id, source)
    elif action == "off":
        result = gpio.turn_off(switch_id, source)
    elif action == "toggle":
        result = gpio.toggle(switch_id, source)
    elif action == "pulse":
        result = gpio.pulse(switch_id, data.get("duration_ms"), source)
    elif action == "press":
        result = gpio.press(switch_id, source)
    elif action == "heartbeat":
        result = gpio.heartbeat(switch_id, token)
    elif action == "release":
        result = gpio.release(switch_id, token, source)
    else:
        raise APIError("action must be on, off, toggle, pulse, press, heartbeat or release")
    return ok({"switch": result})


# ------------------------------------------------------------------ buttons
def _clean_button(cfg: Dict[str, Any], raw: Dict[str, Any], skip: str = ""):
    from ..gpio.buttons import ButtonInput
    from ..gpio.common import GPIOError

    try:
        item = ButtonInput.from_dict(raw, get_ctx().gpio.action_cleaner)
    except GPIOError as exc:
        raise APIError(str(exc))
    others = taken_pins(cfg, skip_button=skip)
    if item.pin in others:
        raise APIError(f"GPIO{item.pin} is already used by '{others[item.pin]}'")
    return item


@bp.get("/gpio/buttons")
def list_buttons():
    return ok({"buttons": get_ctx().gpio.snapshot().get("buttons", [])})


@bp.post("/gpio/buttons")
def add_button():
    from ..gpio.buttons import MAX_BUTTONS

    ctx = get_ctx()
    data = dict(body())
    data["id"] = new_id("btn")
    created: Dict[str, Any] = {}

    def mutate(c):
        buttons = c["gpio"].setdefault("buttons", [])
        if len(buttons) >= MAX_BUTTONS:
            raise APIError(f"You can have at most {MAX_BUTTONS} buttons")
        item = _clean_button(c, data)
        buttons.append(item.to_dict())
        created.update(item.to_dict())

    ctx.store.update(mutate, backup_reason="add-button")
    return ok({"button": created, "buttons": ctx.gpio.snapshot().get("buttons", [])}, 201)


@bp.put("/gpio/buttons/<button_id>")
@bp.patch("/gpio/buttons/<button_id>")
def update_button(button_id):
    ctx = get_ctx()
    data = body()

    def mutate(c):
        buttons = c["gpio"].setdefault("buttons", [])
        for i, b in enumerate(buttons):
            if b.get("id") == button_id:
                merged = {**b, **{k: v for k, v in data.items() if k != "id"}, "id": button_id}
                buttons[i] = _clean_button(c, merged, skip=button_id).to_dict()
                return
        raise APIError("unknown button", 404)

    ctx.store.update(mutate, backup_reason="edit-button")
    return ok({"buttons": ctx.gpio.snapshot().get("buttons", [])})


@bp.delete("/gpio/buttons/<button_id>")
def delete_button(button_id):
    ctx = get_ctx()

    def mutate(c):
        buttons = c["gpio"].get("buttons", []) or []
        if not any(b.get("id") == button_id for b in buttons):
            raise APIError("unknown button", 404)
        c["gpio"]["buttons"] = [b for b in buttons if b.get("id") != button_id]

    ctx.store.update(mutate, backup_reason="remove-button")
    return ok({"buttons": ctx.gpio.snapshot().get("buttons", [])})


@bp.post("/gpio/buttons/<button_id>/try")
def try_button(button_id):
    """Do what a tap (or a hold) of this button does, from the dashboard."""
    kind = str(body().get("kind") or "press")
    get_ctx().gpio.press_button(button_id, kind)
    return ok({"message": "Done – the action is running"})


@bp.post("/gpio/all-off")
def all_off():
    return ok(get_ctx().gpio.all_off("web"))


@bp.post("/gpio/test")
def test_pin():
    ctx = get_ctx()
    data = body()
    try:
        pin = int(data.get("pin"))
    except (TypeError, ValueError):
        raise APIError("pin must be a number")
    active_high = bool(data.get("active_high", True))
    duration = int(data.get("duration_ms") or 400)
    ctx.gpio.test_pin(pin, active_high, duration)
    return ok({"message": f"Pulsed GPIO{pin} for {duration} ms"})


def _apply_boot_pins(ctx) -> None:
    """Ask the privileged helper to record boot-time pin states (best effort)."""
    try:
        from .system import apply_boot_pins

        apply_boot_pins(ctx)
    except Exception:  # never let this break a config save
        import logging

        logging.getLogger(__name__).debug("boot pin update skipped", exc_info=True)
