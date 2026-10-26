"""Dashboard layout, appearance and scenes (quick actions)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from flask import Blueprint

from .. import get_ctx
from ..config import new_id
from . import APIError, body, ok

bp = Blueprint("dashboard", __name__)

TILE_TYPES = ("device", "switch", "scene", "group", "all", "clock", "heading", "note")
# Tiles that exist because something else does; they can be hidden, not removed.
BOUND_TILE_TYPES = ("device", "switch", "scene", "group")
MAX_FAVORITE_COLORS = 24
TILE_SIZES = ("s", "m", "l", "xl")
# "auto" follows the device's light/dark setting; the rest are explicit.
THEMES = ("auto", "midnight", "graphite", "oled", "ocean", "daylight", "paper")
DENSITIES = ("comfortable", "compact", "roomy")
RADII = ("sharp", "soft", "round")
TEXT_SCALE_RANGE = (85, 150)
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Total time one scene run may spend waiting, across all of its delay actions.
SCENE_DELAY_BUDGET_S = 30.0


# ------------------------------------------------------------ tile helpers
def ensure_tile(cfg: Dict[str, Any], tile_type: str, ref: str, title: str = "") -> Dict[str, Any]:
    tiles = cfg["dashboard"].setdefault("tiles", [])
    for tile in tiles:
        if tile.get("type") == tile_type and tile.get("ref") == ref:
            return tile
    tile = {"id": new_id("tile"), "type": tile_type, "ref": ref, "size": "m", "hidden": False,
            "name": "", "title": title, "icon": "", "color": "", "opts": {}}
    tiles.append(tile)
    return tile


def remove_tile(cfg: Dict[str, Any], tile_type: str, ref: str) -> None:
    tiles = cfg["dashboard"].get("tiles", [])
    cfg["dashboard"]["tiles"] = [t for t in tiles if not (t.get("type") == tile_type and t.get("ref") == ref)]


def sync_tiles(cfg: Dict[str, Any]) -> None:
    """Make sure every device/switch has a tile and orphans are dropped."""
    device_ids = {d["id"] for d in cfg.get("devices", []) if d.get("id")}
    switch_ids = {s["id"] for s in cfg.get("gpio", {}).get("switches", []) if s.get("id")}
    scene_ids = {s["id"] for s in cfg["dashboard"].get("scenes", []) if s.get("id")}
    group_ids = {g["id"] for g in cfg.get("groups", []) if isinstance(g, dict) and g.get("id")}
    tiles = cfg["dashboard"].setdefault("tiles", [])
    kept = []
    for tile in tiles:
        t, ref = tile.get("type"), tile.get("ref")
        if t == "device" and ref not in device_ids:
            continue
        if t == "switch" and ref not in switch_ids:
            continue
        if t == "scene" and ref not in scene_ids:
            continue
        if t == "group" and ref not in group_ids:
            continue
        kept.append(tile)
    cfg["dashboard"]["tiles"] = kept
    for d in cfg.get("devices", []):
        if d.get("id"):
            ensure_tile(cfg, "device", d["id"], d.get("name", ""))
    for s in cfg.get("gpio", {}).get("switches", []):
        if s.get("id"):
            ensure_tile(cfg, "switch", s["id"], s.get("name", ""))
    for s in cfg["dashboard"].get("scenes", []):
        if s.get("id"):
            ensure_tile(cfg, "scene", s["id"], s.get("name", ""))
    for g in cfg.get("groups", []):
        if isinstance(g, dict) and g.get("id"):
            ensure_tile(cfg, "group", g["id"], g.get("name", ""))


def clean_tile(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIError("tile must be an object")
    t = str(raw.get("type") or "")
    if t not in TILE_TYPES:
        raise APIError(f"unknown tile type '{t}'")
    size = str(raw.get("size") or "m")
    if size not in TILE_SIZES:
        size = "m"
    color = str(raw.get("color") or "")
    if color and not HEX_RE.match(color):
        color = ""
    opts = raw.get("opts") if isinstance(raw.get("opts"), dict) else {}
    return {
        "id": str(raw.get("id") or new_id("tile")),
        "type": t,
        "ref": str(raw.get("ref") or ""),
        "size": size,
        "hidden": bool(raw.get("hidden", False)),
        "name": str(raw.get("name") or "")[:60],
        "title": str(raw.get("title") or "")[:60],
        "icon": str(raw.get("icon") or "")[:32],
        "color": color,
        "opts": {k: v for k, v in opts.items() if isinstance(k, str) and len(k) <= 32 and isinstance(v, (str, int, float, bool, list))},
    }


def clean_lights_action(a: Dict[str, Any]) -> Dict[str, Any]:
    """A device-agnostic lights action: ``{"type":"lights","target":...,"intent":{...}}``."""
    from ..wled.intents import IntentError, normalise_intent
    from .lights import normalise_target

    try:
        intent = normalise_intent(a.get("intent"))
    except IntentError as exc:
        raise APIError(str(exc))
    target = normalise_target(a.get("target"))
    out = {"type": "lights", "target": target, "intent": intent}
    if a.get("label"):
        out["label"] = str(a["label"])[:60]
    return out


def clean_scene(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise APIError("scene must be an object")
    name = str(raw.get("name") or "").strip()[:60]
    if not name:
        raise APIError("Give the scene a name")
    actions = raw.get("actions")
    if not isinstance(actions, list) or not actions:
        raise APIError("A scene needs at least one action")
    if len(actions) > 100:
        raise APIError("A scene can have at most 100 actions")
    clean_actions = []
    for a in actions:
        if not isinstance(a, dict):
            raise APIError("scene action must be an object")
        kind = a.get("type")
        if kind in ("device", "all"):
            from ..wled.manager import DeviceError, sanitise_state

            state = a.get("state")
            if not isinstance(state, dict) or not state:
                raise APIError("device action needs a state object")
            try:
                # Checked (and hex colours turned into numbers) now, not every
                # time the scene runs and fails.
                state = sanitise_state(state)
            except DeviceError as exc:
                raise APIError(f"scene action: {exc}")
            entry = {"type": kind, "ref": str(a.get("ref") or ""), "state": state}
            if a.get("label"):
                entry["label"] = str(a["label"])[:60]
            clean_actions.append(entry)
        elif kind == "switch":
            action = str(a.get("action") or "")
            if action not in ("on", "off", "toggle", "pulse"):
                raise APIError("switch action must be on, off, toggle or pulse")
            clean_actions.append({"type": "switch", "ref": str(a.get("ref") or ""), "action": action})
        elif kind == "delay":
            clean_actions.append({"type": "delay", "ms": int(max(0, min(10000, int(a.get("ms") or 0))))})
        elif kind == "lights":
            clean_actions.append(clean_lights_action(a))
        elif kind == "all_off":
            clean_actions.append({"type": "all_off"})
        else:
            raise APIError(f"unknown action type '{kind}'")
    color = str(raw.get("color") or "")
    return {
        "id": str(raw.get("id") or new_id("scene")),
        "name": name,
        "icon": str(raw.get("icon") or "sparkles")[:32],
        "color": color if HEX_RE.match(color) else "",
        "actions": clean_actions,
    }


# --------------------------------------------------------------- endpoints
@bp.get("/dashboard")
def get_dashboard():
    ctx = get_ctx()
    cfg = ctx.store.get()
    dash = cfg["dashboard"]
    before = list(dash.get("tiles", []))
    sync_tiles(cfg)
    if cfg["dashboard"].get("tiles") != before:
        ctx.store.update(sync_tiles)
        dash = ctx.store.section("dashboard")
    return ok({"dashboard": dash, "setup_complete": cfg.get("setup_complete", False),
               "groups": [g for g in cfg.get("groups", []) if isinstance(g, dict)]})


@bp.put("/dashboard")
@bp.patch("/dashboard")
def update_dashboard():
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        dash = cfg["dashboard"]
        if "title" in data:
            dash["title"] = str(data["title"] or "JimboLED")[:40]
        if "subtitle" in data:
            dash["subtitle"] = str(data["subtitle"] or "")[:80]
        if "theme" in data:
            if data["theme"] not in THEMES:
                raise APIError(f"theme must be one of {', '.join(THEMES)}")
            dash["theme"] = data["theme"]
        if "accent" in data:
            if not HEX_RE.match(str(data["accent"])):
                raise APIError("accent must be a hex colour like #7c5cff")
            dash["accent"] = data["accent"]
        if "density" in data:
            if data["density"] not in DENSITIES:
                raise APIError(f"density must be one of {', '.join(DENSITIES)}")
            dash["density"] = data["density"]
        if "radius" in data:
            if data["radius"] not in RADII:
                raise APIError(f"radius must be one of {', '.join(RADII)}")
            dash["radius"] = data["radius"]
        if "text_scale" in data:
            lo, hi = TEXT_SCALE_RANGE
            try:
                scale = int(data["text_scale"])
            except (TypeError, ValueError):
                raise APIError("text_scale must be a number")
            if not lo <= scale <= hi:
                raise APIError(f"text_scale must be between {lo} and {hi}")
            dash["text_scale"] = scale
        if "favorite_colors" in data:
            favs = data["favorite_colors"]
            if not isinstance(favs, list):
                raise APIError("favorite_colors must be a list of hex colours")
            clean = []
            for c in favs:
                c = str(c).lower()
                if HEX_RE.match(c) and c not in clean:
                    clean.append(c)
            dash["favorite_colors"] = clean[:MAX_FAVORITE_COLORS]
        for flag in ("show_offline", "show_clock", "show_estop", "show_lights_button"):
            if flag in data:
                dash[flag] = bool(data[flag])
        if "tiles" in data:
            if not isinstance(data["tiles"], list):
                raise APIError("tiles must be a list")
            dash["tiles"] = [clean_tile(t) for t in data["tiles"]]
            sync_tiles(cfg)
        if "scenes" in data:
            if not isinstance(data["scenes"], list):
                raise APIError("scenes must be a list")
            dash["scenes"] = [clean_scene(s) for s in data["scenes"]]
            sync_tiles(cfg)

    ctx.store.update(mutate, backup_reason="dashboard")
    return ok({"dashboard": ctx.store.section("dashboard")})


@bp.post("/dashboard/tiles")
def add_tile():
    ctx = get_ctx()
    data = body()
    tile = clean_tile(data)
    if tile["type"] in BOUND_TILE_TYPES:
        raise APIError("controller, switch, scene and group tiles are created automatically")

    def mutate(cfg):
        cfg["dashboard"].setdefault("tiles", []).append(tile)

    ctx.store.update(mutate)
    return ok({"tile": tile, "dashboard": ctx.store.section("dashboard")}, 201)


@bp.put("/dashboard/tiles/<tile_id>")
@bp.patch("/dashboard/tiles/<tile_id>")
def update_tile(tile_id):
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        for i, tile in enumerate(cfg["dashboard"].get("tiles", [])):
            if tile.get("id") == tile_id:
                merged = {**tile, **{k: v for k, v in data.items() if k in ("size", "hidden", "name", "icon", "color", "opts")}}
                cfg["dashboard"]["tiles"][i] = clean_tile(merged)
                return
        raise APIError("unknown tile", 404)

    ctx.store.update(mutate)
    return ok({"dashboard": ctx.store.section("dashboard")})


@bp.delete("/dashboard/tiles/<tile_id>")
def delete_tile(tile_id):
    ctx = get_ctx()

    def mutate(cfg):
        tiles = cfg["dashboard"].get("tiles", [])
        target = next((t for t in tiles if t.get("id") == tile_id), None)
        if target is None:
            raise APIError("unknown tile", 404)
        if target.get("type") in BOUND_TILE_TYPES:
            target["hidden"] = True  # bound tiles can only be hidden, not removed
        else:
            cfg["dashboard"]["tiles"] = [t for t in tiles if t.get("id") != tile_id]

    ctx.store.update(mutate)
    return ok({"dashboard": ctx.store.section("dashboard")})


@bp.post("/dashboard/order")
def reorder_tiles():
    ctx = get_ctx()
    data = body()
    order = data.get("ids")
    if not isinstance(order, list):
        raise APIError("ids must be a list of tile ids")

    def mutate(cfg):
        tiles = cfg["dashboard"].get("tiles", [])
        by_id = {t.get("id"): t for t in tiles}
        new_list = [by_id[i] for i in order if i in by_id]
        seen = {t.get("id") for t in new_list}
        new_list.extend(t for t in tiles if t.get("id") not in seen)
        cfg["dashboard"]["tiles"] = new_list

    ctx.store.update(mutate)
    return ok({"dashboard": ctx.store.section("dashboard")})


# ----------------------------------------------------------------- scenes
@bp.get("/scenes")
def list_scenes():
    return ok({"scenes": (get_ctx().store.section("dashboard") or {}).get("scenes", [])})


@bp.post("/scenes")
def add_scene():
    ctx = get_ctx()
    scene = clean_scene(body())

    def mutate(cfg):
        cfg["dashboard"].setdefault("scenes", []).append(scene)
        ensure_tile(cfg, "scene", scene["id"], scene["name"])

    ctx.store.update(mutate, backup_reason="scene")
    return ok({"scene": scene, "dashboard": ctx.store.section("dashboard")}, 201)


@bp.put("/scenes/<scene_id>")
def update_scene(scene_id):
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        scenes = cfg["dashboard"].setdefault("scenes", [])
        for i, s in enumerate(scenes):
            if s.get("id") == scene_id:
                scenes[i] = clean_scene({**s, **data, "id": scene_id})
                for tile in cfg["dashboard"].get("tiles", []):
                    if tile.get("type") == "scene" and tile.get("ref") == scene_id:
                        tile["title"] = scenes[i]["name"]
                return
        raise APIError("unknown scene", 404)

    ctx.store.update(mutate, backup_reason="scene")
    return ok({"dashboard": ctx.store.section("dashboard")})


@bp.delete("/scenes/<scene_id>")
def delete_scene(scene_id):
    ctx = get_ctx()

    def mutate(cfg):
        scenes = cfg["dashboard"].get("scenes", [])
        if not any(s.get("id") == scene_id for s in scenes):
            raise APIError("unknown scene", 404)
        cfg["dashboard"]["scenes"] = [s for s in scenes if s.get("id") != scene_id]
        remove_tile(cfg, "scene", scene_id)

    ctx.store.update(mutate, backup_reason="scene")
    return ok({"dashboard": ctx.store.section("dashboard")})


@bp.post("/scenes/test")
def test_scene():
    """Run a scene that has not been saved yet (the editor's Try it button)."""
    data = body()
    scene = clean_scene({"name": "Test", "actions": data.get("actions")})
    results = run_scene_actions(get_ctx(), scene)
    return ok({"results": results, "failed": [r for r in results if r.get("error")]})


@bp.post("/scenes/<scene_id>/run")
def run_scene(scene_id):
    ctx = get_ctx()
    scenes = (ctx.store.section("dashboard") or {}).get("scenes", [])
    scene = next((s for s in scenes if s.get("id") == scene_id), None)
    if scene is None:
        raise APIError("unknown scene", 404)
    results = run_scene_actions(ctx, scene)
    failed = [r for r in results if r.get("error")]
    return ok({"results": results, "failed": failed})


def run_scene_actions(ctx, scene: Dict[str, Any], *, source: str = "scene") -> List[Dict[str, Any]]:
    import time as _time

    # A scene runs on the request's own worker thread, and there are only eight
    # of them.  Each delay is capped at 10 s, but nothing capped how many a
    # scene could string together; the budget keeps one scene from holding a
    # worker (and the dashboard's share of them) for minutes.
    budget = SCENE_DELAY_BUDGET_S
    results = []
    for action in scene.get("actions", []):
        kind = action.get("type")
        if kind == "delay":
            entry: Dict[str, Any] = {"type": kind}
            wait = max(0.0, min(10.0, action.get("ms", 0) / 1000.0, budget))
            budget -= wait
            if wait:
                _time.sleep(wait)
            elif action.get("ms"):
                entry["error"] = "skipped: this scene has already waited long enough"
            results.append(entry)
            continue
        results.append(run_action(ctx, action, source=source))
    return results


def run_action(ctx, action: Dict[str, Any], *, source: str = "scene") -> Dict[str, Any]:
    """Run one scene/schedule action; never raises for device or relay problems."""
    from ..gpio.manager import GPIOError
    from ..wled.manager import DeviceError

    kind = action.get("type")
    entry: Dict[str, Any] = {"type": kind, "ref": action.get("ref")}
    try:
        if kind == "device":
            ctx.devices.set_state(action["ref"], action["state"])
        elif kind == "all":
            res = ctx.devices.set_state_all(action["state"])
            bad = {k: v for k, v in res.items() if v != "ok"}
            if bad:
                entry["error"] = "; ".join(f"{k}: {v}" for k, v in bad.items())
        elif kind == "lights":
            from .lights import resolve_target, summarise

            label, ids = resolve_target(ctx.store.get(), action.get("target"))
            out = summarise(ctx.devices.apply_intent(ids, action["intent"]))
            entry["label"] = label
            entry["message"] = out["message"]
            if not out["ok"]:
                entry["error"] = out["message"]
        elif kind == "all_off":
            ctx.gpio.all_off(source=source)
            res = ctx.devices.set_state_all({"on": False})
            bad = {k: v for k, v in res.items() if v != "ok"}
            if bad:
                entry["error"] = "; ".join(f"{k}: {v}" for k, v in bad.items())
        elif kind == "switch":
            act = action["action"]
            if act == "on":
                ctx.gpio.turn_on(action["ref"], source=source)
            elif act == "off":
                ctx.gpio.turn_off(action["ref"], source=source)
            elif act == "toggle":
                ctx.gpio.toggle(action["ref"], source=source)
            elif act == "pulse":
                ctx.gpio.pulse(action["ref"], source=source)
        else:
            entry["error"] = f"unknown action '{kind}'"
    except (DeviceError, GPIOError) as exc:
        entry["error"] = str(exc)
    except APIError as exc:
        entry["error"] = exc.message
    return entry
