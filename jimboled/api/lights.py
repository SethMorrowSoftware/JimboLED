"""Controlling many WLED controllers at once, and groups (rooms) of them.

Every endpoint here takes a *target*:

* ``"all"`` (default) – every enabled controller
* ``"group:<id>"`` or ``{"group": "<id>"}`` – the controllers in one group
* ``"device:<id>"``, ``{"devices": [...]}`` or a plain list – chosen controllers

and a device-agnostic *intent* (see :mod:`jimboled.wled.intents`), so "all
lights red" or "the preset called Movie everywhere" means the same thing on
every controller even though each numbers its effects, palettes and presets
differently.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Tuple

from flask import Blueprint, request

from .. import get_ctx
from ..config import new_id
from ..wled import intents as I
from . import APIError, body, ok

bp = Blueprint("lights", __name__)

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_GROUPS = 50


# ----------------------------------------------------------------- targets
def normalise_target(raw: Any) -> Dict[str, Any]:
    """Canonical target: ``{"all": True}``, ``{"group": id}`` or ``{"devices": [...]}``."""
    if raw in (None, "", "all") or (isinstance(raw, dict) and raw.get("all")):
        return {"all": True}
    if isinstance(raw, str):
        if raw.startswith("group:") and len(raw) > 6:
            return {"group": raw[6:]}
        if raw.startswith("device:") and len(raw) > 7:
            return {"devices": [raw[7:]]}
        raise APIError("target must be \"all\", \"group:<id>\" or \"device:<id>\"")
    if isinstance(raw, list):
        raw = {"devices": raw}
    if isinstance(raw, dict):
        if raw.get("group"):
            return {"group": str(raw["group"])[:64]}
        devices = raw.get("devices")
        if isinstance(devices, list) and devices:
            return {"devices": [str(d)[:64] for d in devices if isinstance(d, (str, int))][:200]}
    raise APIError("target must be \"all\", a group or a list of controllers")


def resolve_target(cfg: Dict[str, Any], raw: Any) -> Tuple[str, List[str]]:
    """``(label, device ids)`` for a target, enabled controllers only, in dashboard order."""
    target = normalise_target(raw)
    devices = [d for d in cfg.get("devices", []) if isinstance(d, dict) and d.get("id")]
    enabled = [d["id"] for d in devices if d.get("enabled", True)]
    if target.get("all"):
        return "All lights", enabled
    if "group" in target:
        group = next((g for g in cfg.get("groups", []) if isinstance(g, dict) and g.get("id") == target["group"]), None)
        if group is None:
            raise APIError("that group no longer exists", 404)
        members = set(group.get("devices") or [])
        return str(group.get("name") or "Group"), [did for did in enabled if did in members]
    wanted = set(target["devices"])
    ids = [did for did in enabled if did in wanted]
    if not ids:
        raise APIError("none of those controllers exist (or they are disabled)", 404)
    names = [d.get("name") for d in devices if d.get("id") in wanted]
    return (names[0] if len(names) == 1 else f"{len(ids)} lights"), ids


def summarise(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    changed = sum(1 for r in results if r["status"] in ("ok", "partial"))
    problems = [r for r in results if r["status"] not in ("ok",)]
    bits = [f"{r['name']}: {r.get('message') or r['status']}" for r in problems]
    if not results:
        message = "There are no lights to change."
    elif changed == len(results):
        message = f"Changed {changed} light{'s' if changed != 1 else ''}." + (" " + "; ".join(bits) if bits else "")
    else:
        message = f"Changed {changed} of {len(results)}. " + "; ".join(bits)
    return {"results": results, "changed": changed, "total": len(results), "message": message,
            "ok": changed == len(results) and not any(r["status"] in ("error", "offline") for r in results)}


# ---------------------------------------------------------------- endpoints
@bp.get("/lights/catalog")
def lights_catalog():
    """Effects, palettes and presets the target's controllers share, by name."""
    ctx = get_ctx()
    target = request.args.get("target") or "all"
    if request.args.get("devices"):
        target = {"devices": [d for d in request.args["devices"].split(",") if d]}
    label, ids = resolve_target(ctx.store.get(), target)
    data = I.union_catalog(ctx.devices.views(ids))
    data.update({"label": label, "device_ids": ids})
    return ok(data)


@bp.post("/lights/apply")
def lights_apply():
    """Apply one intent to every controller in the target, in parallel."""
    ctx = get_ctx()
    data = body()
    raw_intent = data.get("intent") if isinstance(data.get("intent"), dict) else {k: v for k, v in data.items() if k != "target"}
    try:
        intent = I.normalise_intent(raw_intent)
    except I.IntentError as exc:
        raise APIError(str(exc))
    label, ids = resolve_target(ctx.store.get(), data.get("target"))
    # Remember how the lights looked, so the change can be undone – but only
    # for the lights it actually went to: undoing must not touch a light this
    # change skipped (and that somebody may have changed since).
    before = ctx.devices.capture(ids, for_undo=True)
    rows = ctx.devices.apply_intent(ids, intent)
    out = summarise(rows)
    out.update({"label": label, "devices": ctx.devices.snapshot()})
    touched = {r["id"] for r in rows if r["status"] in ("ok", "partial", "error")}
    before = [snap for snap in before if snap["id"] in touched]
    if before and out["changed"]:
        out["undo"] = _remember_undo(before)
    return ok(out)


# --------------------------------------------------------------------- undo
UNDO_KEEP_S = 600
UNDO_MAX = 20
_undo: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_undo_lock = threading.Lock()


def _remember_undo(snapshots: List[Dict[str, Any]]) -> str:
    import secrets
    import time as _time

    key = secrets.token_urlsafe(9)
    now = _time.monotonic()
    with _undo_lock:
        for k in [k for k, (t, _) in _undo.items() if now - t > UNDO_KEEP_S]:
            _undo.pop(k, None)
        while len(_undo) >= UNDO_MAX:
            _undo.pop(min(_undo, key=lambda k: _undo[k][0]), None)
        _undo[key] = (now, snapshots)
    return key


@bp.post("/lights/undo/<key>")
def lights_undo(key):
    """Put the lights back as they were before one /lights/apply."""
    ctx = get_ctx()
    with _undo_lock:
        entry = _undo.pop(key, None)
    if entry is None:
        raise APIError("that change can no longer be undone", 404)
    results = ctx.devices.restore([s for s in entry[1] if ctx.devices.has(s["id"])])
    names = {s["id"]: s["name"] for s in entry[1]}
    rows = [{"id": did, "name": names.get(did, did), "status": "ok" if res == "ok" else "error",
             "message": "" if res == "ok" else res, "notes": []} for did, res in results.items()]
    out = summarise(rows)
    out["message"] = "Put back as it was." if out["ok"] else out["message"]
    out["devices"] = ctx.devices.snapshot()
    return ok(out)


@bp.post("/lights/capture")
def lights_capture():
    """Snapshot the target's current look as scene actions (one per controller)."""
    ctx = get_ctx()
    data = body()
    label, ids = resolve_target(ctx.store.get(), data.get("target"))
    snaps = ctx.devices.capture(ids)
    if not snaps:
        raise APIError("none of those lights are online, so there is nothing to capture", 409)
    actions = [{"type": "device", "ref": s["id"], "state": s["state"], "label": s["name"]} for s in snaps]
    return ok({"label": label, "actions": actions, "captured": len(snaps), "of": len(ids)})


@bp.post("/devices/<device_id>/copy")
def copy_look(device_id):
    """Make other lights look like this one (colours, effect, palette, brightness)."""
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    data = body()
    view = ctx.devices.view(device_id)
    if not view.online:
        raise APIError(f"{view.name} is offline, so its look can't be read", 409)
    intent = I.look_intent(view, include_power=bool(data.get("include_brightness", True)))
    if not any(k != "best_effort" for k in intent):
        raise APIError(f"{view.name} hasn't reported its look yet; try again in a moment", 409)
    label, ids = resolve_target(ctx.store.get(), data.get("target"))
    ids = [i for i in ids if i != device_id]
    if not ids:
        raise APIError("choose at least one other light to copy to")
    out = summarise(ctx.devices.apply_intent(ids, intent))
    out.update({"label": label, "intent": intent, "devices": ctx.devices.snapshot()})
    return ok(out)


@bp.post("/devices/<device_id>/presets/<int:slot>/copy")
def copy_preset(device_id, slot):
    """Store one of this controller's presets on other controllers, by name."""
    ctx = get_ctx()
    if not ctx.devices.has(device_id):
        raise APIError(f"unknown device '{device_id}'", 404)
    data = body()
    _label, ids = resolve_target(ctx.store.get(), data.get("target"))
    try:
        rows = ctx.devices.copy_preset(device_id, slot, ids, overwrite=bool(data.get("overwrite", True)))
    except I.IntentError as exc:
        raise APIError(str(exc))
    return ok(summarise(rows))


# ------------------------------------------------------------------ groups
def clean_group(raw: Any, known_devices: List[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIError("group must be an object")
    name = str(raw.get("name") or "").strip()[:40]
    if not name:
        raise APIError("Give the group a name, e.g. Bedroom")
    members = raw.get("devices") if isinstance(raw.get("devices"), list) else []
    known = set(known_devices)
    devices: List[str] = []
    for did in members:
        did = str(did)
        if did in known and did not in devices:
            devices.append(did)
    color = str(raw.get("color") or "")
    return {
        "id": str(raw.get("id") or new_id("grp"))[:64],
        "name": name,
        "icon": str(raw.get("icon") or "home")[:32],
        "color": color if HEX_RE.match(color) else "",
        "devices": devices,
    }


def _device_ids(cfg: Dict[str, Any]) -> List[str]:
    return [d["id"] for d in cfg.get("devices", []) if isinstance(d, dict) and d.get("id")]


@bp.get("/groups")
def list_groups():
    return ok({"groups": get_ctx().store.section("groups") or []})


@bp.post("/groups")
def add_group():
    from .dashboard import ensure_tile

    ctx = get_ctx()
    data = body()
    created: Dict[str, Any] = {}

    def mutate(cfg):
        groups = cfg.setdefault("groups", [])
        if len(groups) >= MAX_GROUPS:
            raise APIError(f"You can have up to {MAX_GROUPS} groups")
        group = clean_group({**data, "id": None}, _device_ids(cfg))
        if any(g.get("name", "").lower() == group["name"].lower() for g in groups):
            raise APIError(f"There is already a group called {group['name']}", 409)
        groups.append(group)
        ensure_tile(cfg, "group", group["id"], group["name"])
        created.update(group)

    ctx.store.update(mutate, backup_reason="group")
    return ok({"group": created, "groups": ctx.store.section("groups")}, 201)


@bp.put("/groups/<group_id>")
@bp.patch("/groups/<group_id>")
def update_group(group_id):
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        groups = cfg.setdefault("groups", [])
        for i, g in enumerate(groups):
            if g.get("id") == group_id:
                merged = {**g, **{k: v for k, v in data.items() if k in ("name", "icon", "color", "devices")}, "id": group_id}
                groups[i] = clean_group(merged, _device_ids(cfg))
                if any(o.get("name", "").lower() == groups[i]["name"].lower() for j, o in enumerate(groups) if j != i):
                    raise APIError(f"There is already a group called {groups[i]['name']}", 409)
                for tile in cfg["dashboard"].get("tiles", []):
                    if tile.get("type") == "group" and tile.get("ref") == group_id:
                        tile["title"] = groups[i]["name"]
                return
        raise APIError("unknown group", 404)

    ctx.store.update(mutate, backup_reason="group")
    return ok({"groups": ctx.store.section("groups")})


@bp.delete("/groups/<group_id>")
def delete_group(group_id):
    from .dashboard import remove_tile

    ctx = get_ctx()

    def mutate(cfg):
        groups = cfg.get("groups", [])
        if not any(g.get("id") == group_id for g in groups):
            raise APIError("unknown group", 404)
        cfg["groups"] = [g for g in groups if g.get("id") != group_id]
        remove_tile(cfg, "group", group_id)

    ctx.store.update(mutate, backup_reason="group")
    return ok({"groups": ctx.store.section("groups")})
