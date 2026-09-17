"""Emergency stop endpoints.

Engaging is deliberately cheap – one POST, no confirmation required by the
server – because in an emergency every extra step costs seconds.  *Resetting*
is the guarded direction: it names the zone explicitly and is refused while a
physical button is still held down.
"""
from __future__ import annotations

from typing import Any, Dict, List

from flask import Blueprint

from .. import get_ctx
from ..config import new_id
from ..gpio.estop import MASTER_ZONE_ID, EStopInput, EStopZone, validate_inputs, validate_zones
from . import APIError, body, ok

bp = Blueprint("estop", __name__)


def _zone_id(data: Dict[str, Any]) -> str:
    return str(data.get("zone") or data.get("zone_id") or MASTER_ZONE_ID).strip() or MASTER_ZONE_ID


@bp.get("/estop")
def estop_status():
    return ok(get_ctx().gpio.estop_snapshot())


@bp.post("/estop/engage")
def estop_engage():
    """Latch a stop.  Safe to call repeatedly; already-engaged zones stay put."""
    ctx = get_ctx()
    data = body()
    reason = str(data.get("reason") or "")[:200]
    return ok(ctx.gpio.engage_estop(_zone_id(data), reason=reason, source="web"))


@bp.post("/estop/reset")
def estop_reset():
    ctx = get_ctx()
    return ok(ctx.gpio.reset_estop(_zone_id(body()), source="web"))


# ------------------------------------------------------------------- zones
@bp.get("/estop/zones")
def list_zones():
    return ok({"zones": get_ctx().gpio.estop_snapshot()["zones"]})


@bp.post("/estop/zones")
def add_zone():
    ctx = get_ctx()
    data = dict(body())
    data["id"] = new_id("stop")
    zone = EStopZone.from_dict(data)

    def mutate(cfg):
        estop = cfg["gpio"].setdefault("estop", {})
        zones = estop.setdefault("zones", [])
        validate_zones(zones + [zone.to_dict()])
        zones.append(zone.to_dict())

    ctx.store.update(mutate, backup_reason="add-estop-zone")
    return ok({"zone": zone.to_dict(), "estop": ctx.gpio.estop_snapshot()}, 201)


@bp.put("/estop/zones/<zone_id>")
@bp.patch("/estop/zones/<zone_id>")
def update_zone(zone_id):
    ctx = get_ctx()
    data = body()
    if zone_id == MASTER_ZONE_ID:
        raise APIError("The master stop is renamed under Settings → Emergency stop")

    def mutate(cfg):
        zones = cfg["gpio"].setdefault("estop", {}).setdefault("zones", [])
        for i, z in enumerate(zones):
            if z.get("id") == zone_id:
                merged = {**z, **{k: v for k, v in data.items() if k != "id"}}
                zone = EStopZone.from_dict(merged)
                validate_zones(zones[:i] + [zone.to_dict()] + zones[i + 1:])
                zones[i] = zone.to_dict()
                return
        raise APIError("unknown emergency stop zone", 404)

    ctx.store.update(mutate, backup_reason="edit-estop-zone")
    return ok({"estop": ctx.gpio.estop_snapshot()})


@bp.delete("/estop/zones/<zone_id>")
def delete_zone(zone_id):
    ctx = get_ctx()
    if zone_id == MASTER_ZONE_ID:
        raise APIError("The master emergency stop cannot be removed")

    def mutate(cfg):
        estop = cfg["gpio"].setdefault("estop", {})
        zones = estop.get("zones", [])
        if not any(z.get("id") == zone_id for z in zones):
            raise APIError("unknown emergency stop zone", 404)
        estop["zones"] = [z for z in zones if z.get("id") != zone_id]
        # An input pointing at a deleted zone falls back to the master stop
        # rather than silently doing nothing.
        for item in estop.get("inputs", []):
            if item.get("zone") == zone_id:
                item["zone"] = MASTER_ZONE_ID

    ctx.store.update(mutate, backup_reason="remove-estop-zone")
    return ok({"estop": ctx.gpio.estop_snapshot()})


# ------------------------------------------------------------------ inputs
def _switch_pins(cfg: Dict[str, Any]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for s in cfg.get("gpio", {}).get("switches", []):
        try:
            out[int(s.get("pin"))] = str(s.get("name") or s.get("id"))
        except (TypeError, ValueError):
            continue
    return out


@bp.post("/estop/inputs")
def add_input():
    ctx = get_ctx()
    data = dict(body())
    data["id"] = new_id("estopin")
    item = EStopInput.from_dict(data)

    def mutate(cfg):
        estop = cfg["gpio"].setdefault("estop", {})
        inputs = estop.setdefault("inputs", [])
        validate_inputs(inputs + [item.to_dict()], taken_pins=_switch_pins(cfg))

        inputs.append(item.to_dict())

    ctx.store.update(mutate, backup_reason="add-estop-input")
    return ok({"input": item.to_dict(), "estop": ctx.gpio.estop_snapshot()}, 201)


@bp.put("/estop/inputs/<input_id>")
@bp.patch("/estop/inputs/<input_id>")
def update_input(input_id):
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        inputs = cfg["gpio"].setdefault("estop", {}).setdefault("inputs", [])
        for i, raw in enumerate(inputs):
            if raw.get("id") == input_id:
                item = EStopInput.from_dict({**raw, **{k: v for k, v in data.items() if k != "id"}})
                validate_inputs(inputs[:i] + [item.to_dict()] + inputs[i + 1:], taken_pins=_switch_pins(cfg))
                inputs[i] = item.to_dict()
                return
        raise APIError("unknown emergency stop input", 404)

    ctx.store.update(mutate, backup_reason="edit-estop-input")
    return ok({"estop": ctx.gpio.estop_snapshot()})


@bp.delete("/estop/inputs/<input_id>")
def delete_input(input_id):
    ctx = get_ctx()

    def mutate(cfg):
        estop = cfg["gpio"].setdefault("estop", {})
        inputs = estop.get("inputs", [])
        if not any(i.get("id") == input_id for i in inputs):
            raise APIError("unknown emergency stop input", 404)
        estop["inputs"] = [i for i in inputs if i.get("id") != input_id]

    ctx.store.update(mutate, backup_reason="remove-estop-input")
    return ok({"estop": ctx.gpio.estop_snapshot()})


@bp.put("/estop/settings")
@bp.patch("/estop/settings")
def update_settings():
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        estop = cfg["gpio"].setdefault("estop", {})
        if "master_name" in data:
            estop["master_name"] = str(data["master_name"] or "All relays")[:60]
        if "confirm_engage" in data:
            estop["confirm_engage"] = bool(data["confirm_engage"])

    ctx.store.update(mutate, backup_reason="estop-settings")
    return ok({"estop": ctx.gpio.estop_snapshot()})
