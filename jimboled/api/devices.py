"""WLED device endpoints."""
from __future__ import annotations

from typing import Any, Dict, Optional

from flask import Blueprint

from .. import get_ctx
from ..config import new_id
from ..wled.client import WLEDClient, WLEDError
from ..wled.discovery import _ipv4_part, _same_subnet
from ..wled.manager import _LEGACY_ID, DeviceError, PutBackError, is_controller, normalise_device, normalise_mac, sanitise_state
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
    new["mac"] = normalise_mac(mac) or new.get("mac", "")

    followed: Dict[str, Any] = {}

    def mutate(cfg):
        devices = [d for d in cfg["devices"] if isinstance(d, dict)]
        # The same controller as a light already added (by its MAC, or the
        # MAC in an older light's id) – looked for first: after addresses
        # were reshuffled, the address may still be another light's.
        same = next((d for d in devices if new["mac"] and is_controller(d, new["mac"])), None)
        if same is not None:
            if same.get("host") == new["host"] or (ctx.devices.has(same["id"]) and ctx.devices.summary(same["id"]).get("online")):
                raise APIError(f"that controller is already added as '{same.get('name')}' (at {same.get('host')})", 409)
            holder = next((d for d in devices if d is not same and d.get("host") == new["host"] and _holds_address(ctx, d)), None)
            if holder is not None:
                raise APIError(f"{new['host']} is already added as '{holder.get('name')}'", 409)
            # It moved: point that light at the new address, keeping its
            # groups, scenes and schedules, rather than adding it twice.
            same["host"] = new["host"]
            followed.update(id=same["id"], name=same.get("name"), enabled=same.get("enabled", True))
            same["enabled"] = True
            return
        for d in devices:
            if d.get("host") == new["host"] and _holds_address(ctx, d):
                raise APIError(f"{new['host']} is already added as '{d.get('name')}'", 409)
        if new["mac"] and not data.get("separate"):
            _ask_if_replacement(ctx, devices, new)
        for d in devices:
            if d.get("id") == new["id"]:
                new["id"] = new_id("wled")
        cfg["devices"].append(new)
        ensure_tile(cfg, "device", new["id"], new["name"])

    ctx.store.update(mutate, backup_reason="add-device")
    did = followed.get("id") or new["id"]
    try:
        ctx.devices.refresh(did)
    except DeviceError:
        pass
    if followed:
        extra = "" if followed["enabled"] else " and it is enabled again"
        return ok({"device": ctx.devices.summary(did), "followed": True,
                   "message": f"That is “{followed['name']}”, which had moved: its address is updated{extra}"})
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
    # {"replace": true}: the person was told another controller answers at
    # this address and saved anyway – its controller was replaced, so the new
    # one is this light now (its MAC is learned afresh).  Never a controller
    # that is another configured light's, though: that one has moved, and
    # is found again by its MAC.
    replace = (bool(data.get("replace")) and ctx.devices.has(device_id)
               and ctx.devices.wrong_device(device_id))
    theirs = ctx.devices.wrong_mac(device_id) if replace else ""
    # A new address is asked first what answers there.
    current_cfg = next((d for d in ctx.store.section("devices") or [] if isinstance(d, dict) and d.get("id") == device_id), None)
    probed: Optional[str] = None
    same_host = True
    if current_cfg is not None and "host" in data:
        target = normalise_device({**current_cfg, "host": data.get("host")}, existing_id=device_id)["host"]
        if target != current_cfg.get("host"):
            same_host = False
            probed = _probe_mac(target)
    outcome = {"replaced": False, "moved": False, "warning": ""}
    if replace and same_host and current_cfg is not None and _probe_mac(str(current_cfg.get("host") or "")) != theirs:
        # The other controller isn't (or no longer) what answers there.
        replace, theirs = False, ""
        outcome["warning"] = ("Saved. The light was not switched to another controller: nothing, or yet another "
                              "one, answers at its address right now.")

    def mutate(cfg):
        current = _device_cfg(cfg, device_id)
        merged = {**current, **{k: v for k, v in data.items() if k in ("name", "host", "enabled", "icon", "color", "notes", "poll_interval_s")}}
        cleaned = normalise_device(merged, existing_id=device_id)
        devices = [d for d in cfg.get("devices", []) if isinstance(d, dict) and d.get("id") != device_id]
        moved = cleaned["host"] != current.get("host")
        mine = normalise_mac(current.get("mac"))
        known = bool(mine) or bool(_LEGACY_ID.match(str(device_id)))  # can be recognised by its MAC
        if moved:
            hint = ("If that controller replaced this light's, remove '{0}' first, then save this address again: "
                    "this light then uses it and keeps its scenes, groups and schedules.")
            other = next((d for d in devices if d.get("host") == cleaned["host"] and _holds_address(ctx, d)), None)
            if other:
                raise APIError(f"{cleaned['host']} is already added as '{other.get('name')}'. " + hint.format(other.get("name")), 409)
            owner = next((d for d in devices if probed and is_controller(d, probed)), None)
            if owner:
                raise APIError(f"the controller at {cleaned['host']} is your light '{owner.get('name')}', not this one. "
                               + hint.format(owner.get("name")), 409)
            if probed is None:
                # Nothing answers there (yet): keep the MAC, so the light is
                # still recognised – and followed – wherever it turns up.
                findable = known and _ipv4_part(cleaned["host"]) and _same_subnet(cleaned["host"], [current.get("host") or ""])
                outcome["warning"] = f"Saved, but nothing answers at {cleaned['host']} right now. " + (
                    "If that address is wrong, JimboLED still finds the light once it is back on the network." if findable else
                    "Check the address: if it is wrong, JimboLED can't find this light by itself (Settings → Find controllers "
                    "lists every controller's address).")
            elif probed and known and not is_controller(current, probed) and not data.get("new_controller"):
                # Another controller than this light's own: ask first (a
                # neighbouring address picked from the router's list, say).
                raise APIError(f"The WLED at {cleaned['host']} is not this light's own controller. If you replaced it, "
                               "use that one; if the light only moved, enter its own address instead.", 409,
                               code="different_controller")
            elif probed != mine:
                cleaned["mac"] = probed  # this controller is the light now
        takeover = replace and not moved
        owner = next((d for d in devices if theirs and is_controller(d, theirs)), None)
        if takeover and owner:
            takeover = False
            tail = ("Both lights are found again once they answer at their new addresses." if known else
                    "This light has never answered, so it can't be recognised elsewhere: enter its controller's address.")
            outcome["warning"] = f"The controller at this address is “{owner.get('name')}”, so this light was not switched to it. {tail}"
        if takeover:
            cleaned["mac"] = theirs  # set now: a light with an old-style id would refuse it otherwise
        outcome["replaced"], outcome["moved"] = takeover, moved
        current.update(cleaned)
        for tile in cfg["dashboard"].get("tiles", []):
            if tile.get("type") == "device" and tile.get("ref") == device_id and not tile.get("name"):
                tile["title"] = cleaned["name"]

    ctx.store.update(mutate, backup_reason="edit-device")
    if outcome["replaced"] or outcome.get("moved"):
        ctx.devices.refresh(device_id)  # usable straight away, not after the next poll
    out: Dict[str, Any] = {"device": ctx.devices.summary(device_id)}
    if outcome["warning"]:
        out["warning"] = outcome["warning"]
    return ok(out)


@bp.delete("/devices/<device_id>")
def delete_device(device_id):
    ctx = get_ctx()

    def mutate(cfg):
        _device_cfg(cfg, device_id)
        cfg["devices"] = [d for d in cfg["devices"] if d.get("id") != device_id]
        remove_tile(cfg, "device", device_id)
        for group in cfg.get("groups", []):
            if isinstance(group, dict) and isinstance(group.get("devices"), list):
                group["devices"] = [d for d in group["devices"] if d != device_id]
        # Scenes and schedules that named this controller stop doing so,
        # rather than failing with "unknown device" every time they run.
        for scene in cfg["dashboard"].get("scenes", []):
            if isinstance(scene, dict) and isinstance(scene.get("actions"), list):
                scene["actions"] = [a for a in scene["actions"] if not (a.get("type") == "device" and a.get("ref") == device_id)]
                for a in scene["actions"]:
                    _forget_in_target(a, device_id)
        for sched in cfg.get("schedules", []):
            if isinstance(sched, dict) and isinstance(sched.get("action"), dict):
                _forget_in_target(sched["action"], device_id)

    ctx.store.update(mutate, backup_reason="remove-device")
    return ok()


def _holds_address(ctx, d: Dict[str, Any]) -> bool:
    """Whether light ``d`` still has its address: it is enabled (a disabled
    light isn't polled, so nothing says the address is still its) and no
    other controller answers there."""
    if not d.get("enabled", True):
        return False
    try:
        return not ctx.devices.wrong_device(d.get("id"))
    except DeviceError:
        return True


def _ask_if_replacement(ctx, devices, new: Dict[str, Any]) -> None:
    """Before a new controller is added as a light of its own: is it the
    replacement for one of the lights?  The person says which (409 with
    ``code: "replacement"`` and the ``candidates``; ``"separate": true``
    adds it anyway).

    * a light that sees exactly this controller instead of its own (at its
      address, or behind its host name): it is taken on there ("replace");
    * otherwise lights that are offline and have a known controller – a new
      board usually gets a new address ("move").
    """
    here = [d for d in devices if d.get("enabled", True) and _wrong_mac(ctx, d) == new["mac"]
            and (d.get("host") == new["host"] or not _ipv4_part(str(d.get("host") or "")))]
    if here:
        d = here[0]
        raise APIError(f"{d.get('host')} is where your light '{d.get('name')}' was. If this controller replaced "
                       f"its old one, '{d.get('name')}' can use it, keeping its scenes, groups and schedules. "
                       "Or add it as a separate new light.", 409, code="replacement", device_id=d.get("id"),
                       light_name=d.get("name"), candidates=[{"id": d.get("id"), "name": d.get("name"), "how": "replace"}])
    offline = []
    for d in devices:
        if not d.get("enabled", True) or not (normalise_mac(d.get("mac")) or _LEGACY_ID.match(str(d.get("id") or ""))):
            continue
        try:
            summary = ctx.devices.summary(d.get("id"))
        except DeviceError:
            continue
        if not summary.get("online") and not summary.get("wrong_device"):
            offline.append({"id": d.get("id"), "name": d.get("name"), "how": "move"})
    if offline:
        names = ", ".join(f"'{c['name']}'" for c in offline[:5]) + (" …" if len(offline) > 5 else "")
        raise APIError(f"Is this controller the replacement for a light that is offline ({names})? That light can "
                       "use it, keeping its scenes, groups and schedules. Or add it as a separate new light.", 409,
                       code="replacement", device_id=offline[0]["id"], light_name=offline[0]["name"], candidates=offline)


def _wrong_mac(ctx, d: Dict[str, Any]) -> str:
    """The MAC of the controller answering at light ``d``'s address instead of its own."""
    try:
        return ctx.devices.wrong_mac(d.get("id"))
    except DeviceError:
        return ""


def _probe_mac(host: str) -> Optional[str]:
    """The MAC of the WLED answering at ``host``: ``""`` if it gives none,
    ``None`` if nothing answers."""
    try:
        client = WLEDClient(host, timeout=4.0)
    except Exception:  # not a usable address
        return None
    try:
        info = client.get_info()
    except WLEDError:
        return None
    finally:
        client.close()
    return normalise_mac(info.get("mac")) if isinstance(info, dict) else None


def _forget_in_target(action, device_id: str) -> None:
    target = action.get("target") if isinstance(action, dict) else None
    if isinstance(target, dict) and isinstance(target.get("devices"), list) and len(target["devices"]) > 1:
        target["devices"] = [d for d in target["devices"] if d != device_id]


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
    if data.get("rename"):
        # Rename only: keep what the preset does.  (A plain save would store
        # whatever the lights show right now in it, and wipe a playlist.)
        try:
            presets = ctx.devices.rename_preset(device_id, slot, name, quick_label=str(data.get("quick_label") or "")[:2])
        except PutBackError as exc:
            return ok({"presets": exc.presets, "slot": slot, "warning": str(exc)})
        return ok({"presets": presets, "slot": slot})
    state = None
    if isinstance(data.get("state"), dict) and data["state"]:
        state = sanitise_state(data["state"])
        for key in ("psave", "pdel", "ps", "pd", "np", "live", "time", "tb"):
            state.pop(key, None)
        if not state:
            raise APIError("state contains nothing that can be saved in a preset")
    try:
        presets = ctx.devices.save_preset(
            device_id, slot, name,
            include_brightness=bool(data.get("include_brightness", True)),
            save_segment_bounds=bool(data.get("save_segment_bounds", True)),
            quick_label=str(data.get("quick_label") or "")[:2],
            state=state,
        )
    except PutBackError as exc:
        return ok({"presets": exc.presets, "slot": slot, "warning": str(exc)})
    return ok({"presets": presets, "slot": slot})


@bp.delete("/devices/<device_id>/presets/<int:slot>")
def delete_preset(device_id, slot):
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    return ok({"presets": ctx.devices.delete_preset(device_id, slot)})


@bp.post("/devices/<device_id>/identify")
def identify_device(device_id):
    """Blink this controller for a few seconds so you can see which one it is."""
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    ctx.devices.identify(device_id)
    return ok({"message": "Look for the blinking light"})


@bp.post("/discover/identify")
def identify_found():
    """The same for a controller that was found on the network but not added yet."""
    from ..wled.manager import identify_client

    host = str(body().get("host") or "").strip()
    if not host:
        raise APIError("which controller? (host)")
    try:
        identify_client(WLEDClient(host, timeout=3.0))
    except (DeviceError, WLEDError) as exc:
        raise APIError(f"{host}: {exc}", 502)
    return ok({"message": "Look for the blinking light"})


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
