"""Schedules, one-off timers, and where/when the Pi thinks it is.

The scheduling itself lives in :mod:`jimboled.scheduler`; this module is the
REST face of it plus the location (for sunrise/sunset) and the timezone.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, request

from .. import get_ctx
from ..config import new_id
from ..scheduler import MAX_SCHEDULES, ScheduleError, normalise_schedule
from ..sun import sun_times
from . import APIError, body, ok
from .dashboard import clean_lights_action

bp = Blueprint("schedules", __name__)

TZ_RE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
MAX_TIMER_MINUTES = 24 * 60


# --------------------------------------------------------------- helpers
def _clean(raw: Any) -> Dict[str, Any]:
    try:
        return normalise_schedule(raw, clean_action=clean_lights_action)
    except ScheduleError as exc:
        raise APIError(str(exc))


def _ref_problem(cfg: Dict[str, Any], sched: Dict[str, Any]) -> Optional[str]:
    """Why ``sched``'s action cannot run (a deleted scene, switch or group)."""
    action = sched.get("action") or {}
    kind = action.get("type")
    if kind == "scene":
        scenes = (cfg.get("dashboard") or {}).get("scenes", [])
        if not any(s.get("id") == action.get("ref") for s in scenes):
            return "Its scene has been deleted."
    elif kind == "switch":
        sw = next((s for s in (cfg.get("gpio") or {}).get("switches", []) if s.get("id") == action.get("ref")), None)
        if sw is None:
            return "Its switch has been deleted."
        if sw.get("mode") == "momentary" and action.get("action") != "off":
            # Hold-to-run exists so that somebody is watching while it moves.
            return f"'{sw.get('name')}' is hold-to-run, so a schedule can only turn it off."
    elif kind == "lights":
        target = action.get("target") or {}
        if isinstance(target, dict) and target.get("group"):
            if not any(isinstance(g, dict) and g.get("id") == target["group"] for g in cfg.get("groups", [])):
                return "Its group has been deleted."
    return None


def schedule_problem(cfg: Dict[str, Any], sched: Dict[str, Any]) -> Optional[str]:
    """Why ``sched`` cannot run as configured, or ``None`` if it can."""
    problem = _ref_problem(cfg, sched)
    if problem:
        return problem
    if (sched.get("when") or {}).get("type") == "sun":
        loc = _location(cfg)
        if loc.get("lat") is None or loc.get("lon") is None:
            return "Set your location (Settings → Schedules) so sunrise and sunset can be worked out."
    return None


def _check_refs(cfg: Dict[str, Any], sched: Dict[str, Any]) -> None:
    """Refuse schedules that point at nothing (a missing location is fine: it can be set later)."""
    problem = _ref_problem(cfg, sched)
    if problem:
        raise APIError(problem)


def _location(cfg: Dict[str, Any]) -> Dict[str, Any]:
    loc = cfg.get("location")
    return loc if isinstance(loc, dict) else {"lat": None, "lon": None, "label": ""}


def current_timezone() -> str:
    """The Pi's IANA timezone name, best effort."""
    try:
        target = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    return os.environ.get("TZ") or (time.tzname[0] if time.tzname else "UTC")


def _sun_today(loc: Dict[str, Any]) -> Dict[str, Optional[float]]:
    if loc.get("lat") is None or loc.get("lon") is None:
        return {"sunrise": None, "sunset": None}
    rise, sets = sun_times(datetime.now().astimezone().date(), float(loc["lat"]), float(loc["lon"]))
    return {"sunrise": rise.timestamp() if rise else None, "sunset": sets.timestamp() if sets else None}


def _listing() -> Dict[str, Any]:
    ctx = get_ctx()
    cfg = ctx.store.get()
    loc = _location(cfg)
    now = datetime.now().astimezone()
    out = []
    for sched in cfg.get("schedules", []):
        if not isinstance(sched, dict):
            continue
        item = ctx.scheduler.describe(sched, loc, now)
        item["problem"] = schedule_problem(cfg, sched)
        out.append(item)
    return {
        "schedules": out,
        "location": loc,
        "now": now.timestamp(),
        "utc_offset_min": int(now.utcoffset().total_seconds() // 60) if now.utcoffset() else 0,
        "timezone": current_timezone(),
        "sun": _sun_today(loc),
        "running": bool(ctx.scheduler.started),
    }


# ------------------------------------------------------------------ CRUD
@bp.get("/schedules")
def list_schedules():
    return ok(_listing())


@bp.post("/schedules")
def add_schedule():
    ctx = get_ctx()
    raw = body()
    raw.pop("id", None)
    sched = _clean({**raw, "updated_at": time.time()})
    sched["id"] = new_id("sched")

    def mutate(cfg):
        schedules = cfg.setdefault("schedules", [])
        if len(schedules) >= MAX_SCHEDULES:
            raise APIError(f"You can have at most {MAX_SCHEDULES} schedules")
        _check_refs(cfg, sched)
        schedules.append(sched)

    ctx.store.update(mutate, backup_reason="schedule")
    return ok({"schedule": sched, **_listing()}, 201)


@bp.put("/schedules/<sched_id>")
@bp.patch("/schedules/<sched_id>")
def update_schedule(sched_id):
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        schedules = cfg.setdefault("schedules", [])
        for i, s in enumerate(schedules):
            if s.get("id") == sched_id:
                # Any edit re-arms from now: re-enabling a morning schedule at
                # 07:01 must not fire the 07:00 run it was switched off for.
                merged = {**s, **data, "id": sched_id, "updated_at": time.time()}
                schedules[i] = _clean(merged)
                _check_refs(cfg, schedules[i])
                return
        raise APIError("unknown schedule", 404)

    ctx.store.update(mutate, backup_reason="schedule")
    return ok(_listing())


@bp.delete("/schedules/<sched_id>")
def delete_schedule(sched_id):
    ctx = get_ctx()

    def mutate(cfg):
        schedules = cfg.get("schedules", [])
        if not any(s.get("id") == sched_id for s in schedules):
            raise APIError("unknown schedule", 404)
        cfg["schedules"] = [s for s in schedules if s.get("id") != sched_id]

    ctx.store.update(mutate, backup_reason="schedule")
    ctx.scheduler.forget(sched_id)
    return ok(_listing())


@bp.post("/schedules/<sched_id>/run")
def run_schedule(sched_id):
    ctx = get_ctx()
    cfg = ctx.store.get()
    sched = next((s for s in cfg.get("schedules", []) if s.get("id") == sched_id), None)
    if sched is None:
        raise APIError("unknown schedule", 404)
    result = ctx.scheduler.run_now(sched)
    if result.get("error"):
        return ok({"ok": False, "error": result["error"], **_listing()})
    return ok({"ok": True, "message": result.get("message") or "Done", **_listing()})


# ----------------------------------------------------------------- timers
@bp.post("/timers")
def add_timer():
    """``{"minutes": 30, "action": {...}, "name"?}`` – a one-off that removes itself."""
    ctx = get_ctx()
    data = body()
    try:
        minutes = float(data.get("minutes"))
    except (TypeError, ValueError):
        raise APIError("say how many minutes")
    if not (0 < minutes <= MAX_TIMER_MINUTES):
        raise APIError("a timer can be from 1 minute to 24 hours")
    action = data.get("action") if isinstance(data.get("action"), dict) else {"type": "all_off"}
    name = str(data.get("name") or "").strip()[:60] or "Sleep timer"
    sched = _clean({"name": name, "enabled": True, "when": {"type": "once", "at": time.time() + minutes * 60},
                    "action": action, "delete_after": True, "updated_at": time.time() - 1})
    sched["id"] = new_id("timer")

    def mutate(cfg):
        schedules = cfg.setdefault("schedules", [])
        if len(schedules) >= MAX_SCHEDULES:
            raise APIError(f"You can have at most {MAX_SCHEDULES} schedules")
        _check_refs(cfg, sched)
        schedules.append(sched)

    ctx.store.update(mutate)
    return ok({"schedule": sched, **_listing()}, 201)


# --------------------------------------------------------------- location
@bp.get("/location")
def get_location():
    cfg = get_ctx().store.get()
    loc = _location(cfg)
    out = {"location": loc, "timezone": current_timezone(), "sun": _sun_today(loc)}
    if request.args.get("zones"):
        try:
            import zoneinfo

            out["zones"] = sorted(z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
        except Exception:  # pragma: no cover - very old Python / no tzdata
            out["zones"] = []
    return ok(out)


@bp.put("/location")
@bp.patch("/location")
def set_location():
    ctx = get_ctx()
    data = body()
    message = "Saved."
    if "lat" in data or "lon" in data or "label" in data:
        lat, lon = data.get("lat"), data.get("lon")
        if lat in (None, "") and lon in (None, ""):
            clean = {"lat": None, "lon": None, "label": ""}
        else:
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                raise APIError("latitude and longitude must be numbers, e.g. 51.5 and -0.12")
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                raise APIError("latitude is -90 to 90 and longitude -180 to 180")
            clean = {"lat": round(lat, 4), "lon": round(lon, 4), "label": str(data.get("label") or "").strip()[:60]}
        ctx.store.update(lambda cfg: cfg.__setitem__("location", clean))
    tz = data.get("timezone")
    if tz and tz != current_timezone():
        tz = str(tz).strip()
        if not TZ_RE.match(tz) or ".." in tz:
            raise APIError("that is not a timezone name (e.g. Europe/London)")
        from .system import run_helper

        proc = run_helper("timezone", tz, timeout=20)
        if proc.returncode != 0:
            raise APIError((proc.stderr or proc.stdout or "could not set the timezone").strip()[:200])
        os.environ.pop("TZ", None)
        time.tzset()
        message = f"Saved. The clock is now on {tz} time."
    return ok({"message": message, **_listing()})


def valid_schedules(items: Any, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Schedules from a backup: drop the unusable ones instead of failing."""
    import logging

    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in items if isinstance(items, list) else []:
        try:
            sched = normalise_schedule(raw, clean_action=clean_lights_action)
        except Exception as exc:
            logging.getLogger(__name__).warning("dropping a schedule from the backup: %s", exc)
            continue
        if not sched["id"] or sched["id"] in seen:
            sched["id"] = new_id("sched")
        seen.add(sched["id"])
        # A restored schedule starts afresh: nothing it "missed" while it sat
        # in a backup file should fire the moment it lands.
        sched["updated_at"] = time.time()
        out.append(sched)
        if len(out) >= MAX_SCHEDULES:
            break
    return out
