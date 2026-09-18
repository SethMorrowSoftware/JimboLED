"""WLED device endpoints."""
from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint

from .. import get_ctx
from ..config import new_id
from ..wled.client import WLEDClient, WLEDError
from ..wled.manager import DeviceError, normalise_device, sanitise_state
from . import APIError, body, ok
from .dashboard import ensure_tile, remove_tile

bp = Blueprint("devices", __name__)


def _device_cfg(cfg: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    for d in cfg.get("devices", []):
        if d.get("id") == device_id:
            return d
    raise APIError(f"unknown device '{device_id}'", 404)


@bp.get("/devices")
def list_devices():
    return ok({"devices": get_ctx().devices.snapshot()})


@bp.post("/devices/probe")
def probe_device():
    data = body()
    host = str(data.get("host") or "").strip()
    if not host:
        raise APIError("Enter the controller's IP address or hostname")
    try:
        client = WLEDClient(host, timeout=4.0)
    except WLEDError as exc:
        raise APIError(f"'{host}' is not a valid address ({exc})")
    try:
        info = client.get_info()
    except WLEDError as exc:
        raise APIError(f"Could not reach a WLED controller at {host} ({exc})", 502)
    finally:
        client.close()
    if not isinstance(info, dict) or "ver" not in info:
        raise APIError(f"{host} answered, but it doesn't look like WLED", 502)
    leds = info.get("leds") if isinstance(info.get("leds"), dict) else {}
    return ok({"host": client.host, "name": info.get("name"), "ver": info.get("ver"), "mac": info.get("mac"),
               "led_count": leds.get("count"), "arch": info.get("arch")})


@bp.post("/devices")
def add_device():
    ctx = get_ctx()
    data = body()
    new = normalise_device(data)
    # Confirm it really is WLED before saving (unless explicitly skipped, e.g. device is off right now).
    mac = None
    if not data.get("skip_probe"):
        client = WLEDClient(new["host"], timeout=4.0)
        try:
            info = client.get_info()
        except WLEDError as exc:
            raise APIError(f"Could not reach a WLED controller at {new['host']} ({exc}). "
                           "Check the address, or tick 'add anyway' if it is switched off right now.", 502)
        finally:
            client.close()
        if not isinstance(info, dict) or "ver" not in info:
            raise APIError(f"{new['host']} answered, but it doesn't look like a WLED controller", 502)
        mac = info.get("mac")
        if not new.get("name") or new["name"] == new["host"]:
            new["name"] = str(info.get("name") or new["host"])[:60]
    # The MAC is whatever the device said it was, so it is coerced, not indexed.
    mac_suffix = "".join(ch for ch in str(mac or "") if ch.isalnum())[-6:].lower()
    new["id"] = f"wled-{mac_suffix}" if len(mac_suffix) == 6 else new_id("wled")

    def mutate(cfg):
        for d in cfg["devices"]:
            if d.get("host") == new["host"]:
                raise APIError(f"{new['host']} is already added as '{d.get('name')}'", 409)
            if d.get("id") == new["id"]:
                new["id"] = new_id("wled")
        cfg["devices"].append(new)
        ensure_tile(cfg, "device", new["id"], new["name"])

    ctx.store.update(mutate, backup_reason="add-device")
    try:
        ctx.devices.refresh(new["id"])
    except DeviceError:
        pass
    return ok({"device": ctx.devices.summary(new["id"])}, 201)


@bp.get("/devices/<device_id>")
def get_device(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    return ok({"device": ctx.devices.full(device_id)})


@bp.put("/devices/<device_id>")
@bp.patch("/devices/<device_id>")
def update_device(device_id):
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        current = _device_cfg(cfg, device_id)
        merged = {**current, **{k: v for k, v in data.items() if k in ("name", "host", "enabled", "icon", "color", "notes", "poll_interval_s")}}
        cleaned = normalise_device(merged, existing_id=device_id)
        current.update(cleaned)
        for tile in cfg["dashboard"].get("tiles", []):
            if tile.get("type") == "device" and tile.get("ref") == device_id and not tile.get("name"):
                tile["title"] = cleaned["name"]

    ctx.store.update(mutate, backup_reason="edit-device")
    return ok({"device": ctx.devices.summary(device_id)})


@bp.delete("/devices/<device_id>")
def delete_device(device_id):
    ctx = get_ctx()

    def mutate(cfg):
        _device_cfg(cfg, device_id)
        cfg["devices"] = [d for d in cfg["devices"] if d.get("id") != device_id]
        remove_tile(cfg, "device", device_id)

    ctx.store.update(mutate, backup_reason="remove-device")
    return ok()


@bp.post("/devices/<device_id>/state")
def set_device_state(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    data = body()
    payload = data.get("state", data)
    summary = ctx.devices.set_state(device_id, payload)
    return ok({"device": summary})


@bp.post("/devices/all/state")
def set_all_state():
    ctx = get_ctx()
    data = body()
    payload = data.get("state", data)
    results = ctx.devices.set_state_all(payload)
    failures = {k: v for k, v in results.items() if v != "ok"}
    return ok({"results": results, "failed": failures, "devices": ctx.devices.snapshot()})


@bp.post("/devices/<device_id>/refresh")
def refresh_device(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    ctx.devices.refresh(device_id)
    return ok({"device": ctx.devices.full(device_id)})


@bp.post("/devices/<device_id>/catalog")
def reload_catalog(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    return ok({"device": ctx.devices.reload_catalog(device_id)})


@bp.get("/devices/<device_id>/presets")
def list_presets(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    return ok({"presets": ctx.devices.presets(device_id)})


@bp.post("/devices/<device_id>/presets")
def save_preset(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    data = body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise APIError("Give the preset a name")
    slot = data.get("slot")
    existing = {p["id"] for p in ctx.devices.presets(device_id)}
    if slot in (None, "", 0, "0", "auto"):
        slot = next((i for i in range(1, 251) if i not in existing), None)
        if slot is None:
            raise APIError("All 250 preset slots on this controller are in use; delete one first", 409)
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        raise APIError("Preset slot must be a number between 1 and 250")
    if not 1 <= slot <= 250:
        raise APIError("Preset slot must be between 1 and 250")
    state = None
    if isinstance(data.get("state"), dict) and data["state"]:
        state = sanitise_state(data["state"])
        for key in ("psave", "pdel", "ps", "pd", "np", "live", "time", "tb"):
            state.pop(key, None)
        if not state:
            raise APIError("state contains nothing that can be saved in a preset")
    presets = ctx.devices.save_preset(
        device_id, slot, name,
        include_brightness=bool(data.get("include_brightness", True)),
        save_segment_bounds=bool(data.get("save_segment_bounds", True)),
        quick_label=str(data.get("quick_label") or "")[:2],
        state=state,
    )
    return ok({"presets": presets, "slot": slot})


@bp.delete("/devices/<device_id>/presets/<int:slot>")
def delete_preset(device_id, slot):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    return ok({"presets": ctx.devices.delete_preset(device_id, slot)})


@bp.post("/devices/<device_id>/reboot")
def reboot_device(device_id):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    ctx.devices.reboot(device_id)
    return ok({"message": "Reboot requested; the controller will be back in a few seconds."})


# ------------------------------------------------------------- discovery
@bp.post("/discover")
def start_discovery():
    ctx = get_ctx()
    data = body()
    methods = data.get("methods")
    if methods is not None and not isinstance(methods, list):
        raise APIError("methods must be a list")
    subnet = data.get("subnet") or None
    return ok(ctx.discovery.start(methods=methods, subnet=subnet))


@bp.get("/discover")
def discovery_status():
    return ok(get_ctx().discovery.status())
