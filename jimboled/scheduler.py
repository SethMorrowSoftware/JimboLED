"""Timed actions: "lights on at 07:00 on weekdays", "porch on 15 minutes after
sunset", "everything off in 30 minutes".

A schedule is::

    {"id", "name", "enabled",
     "when":   {"type": "time", "time": "07:00"}
             | {"type": "sun", "event": "sunrise"|"sunset", "offset": -180..180 (minutes)}
             | {"type": "once", "at": <unix seconds>},
     "days":   [0..6]  (Monday = 0; ignored for "once"),
     "random": 0..60   (minutes of random variation either side, for an
                        "away" look that doesn't run like clockwork),
     "action": {"type": "scene", "ref": <scene id>}
             | {"type": "lights", "target": ..., "intent": {...}}
             | {"type": "switch", "ref": <switch id>, "action": "off"|"on"|"toggle"|"pulse"}
             | {"type": "all_off"},
     "delete_after": bool (one-shot timers remove themselves once they fire),
     "updated_at": <unix seconds, set by the server>}

The runner wakes every few seconds and fires each schedule whose most recent
due time has passed and has not fired yet.  A schedule that was due while the
Pi was off still fires if it comes back within ``GRACE_S`` – a short power cut
at 06:59 should not cost the morning lights – but older misses are skipped:
nobody wants the "goodnight" scene at 10 in the morning.

Relay safety is not this module's job and it does not try: every switch action
goes through :class:`GPIOManager`, which refuses hold-to-run switches, honours
interlocks and refuses anything a latched emergency stop covers.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .atomicio import write_json_atomic
from .sun import sun_times

log = logging.getLogger(__name__)

TICK_S = 5.0
GRACE_S = 180.0
MAX_SCHEDULES = 100
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SWITCH_ACTIONS = ("off", "on", "toggle", "pulse")


class ScheduleError(ValueError):
    pass


# --------------------------------------------------------------- parsing
def normalise_schedule(raw: Any, *, clean_action: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Validate a schedule.  ``clean_action`` validates lights actions (it lives
    with the API so this module stays free of Flask)."""
    if not isinstance(raw, dict):
        raise ScheduleError("a schedule must be an object")
    name = str(raw.get("name") or "").strip()[:60]
    when = raw.get("when")
    if not isinstance(when, dict):
        raise ScheduleError("say when it should run")
    kind = when.get("type")
    if kind == "time":
        m = TIME_RE.match(str(when.get("time") or "").strip())
        if not m:
            raise ScheduleError("time must look like 07:30")
        clean_when: Dict[str, Any] = {"type": "time", "time": f"{int(m.group(1)):02d}:{m.group(2)}"}
    elif kind == "sun":
        event = when.get("event")
        if event not in ("sunrise", "sunset"):
            raise ScheduleError("choose sunrise or sunset")
        try:
            offset = int(round(float(when.get("offset") or 0)))
        except (TypeError, ValueError):
            raise ScheduleError("the offset must be a number of minutes")
        clean_when = {"type": "sun", "event": event, "offset": max(-180, min(180, offset))}
    elif kind == "once":
        try:
            at = float(when.get("at"))
        except (TypeError, ValueError):
            raise ScheduleError("a one-off timer needs a time")
        if at != at or at <= 0:
            raise ScheduleError("a one-off timer needs a time")
        clean_when = {"type": "once", "at": at}
    else:
        raise ScheduleError("when.type must be time, sun or once")
    days = raw.get("days")
    if days is None:
        days = list(range(7))
    if not isinstance(days, list):
        raise ScheduleError("days must be a list of weekday numbers (Monday = 0)")
    clean_days = sorted({int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
    if kind != "once" and not clean_days:
        raise ScheduleError("pick at least one day")
    try:
        jitter = int(round(float(raw.get("random") or 0)))
    except (TypeError, ValueError):
        raise ScheduleError("random must be a number of minutes")
    action = raw.get("action")
    if not isinstance(action, dict):
        raise ScheduleError("say what it should do")
    akind = action.get("type")
    if akind == "scene":
        if not action.get("ref"):
            raise ScheduleError("choose a scene")
        clean_act: Dict[str, Any] = {"type": "scene", "ref": str(action["ref"])[:64]}
    elif akind == "switch":
        if not action.get("ref"):
            raise ScheduleError("choose a switch")
        if action.get("action") not in SWITCH_ACTIONS:
            raise ScheduleError("switch action must be off, on, toggle or pulse")
        clean_act = {"type": "switch", "ref": str(action["ref"])[:64], "action": action["action"]}
    elif akind == "all_off":
        clean_act = {"type": "all_off"}
    elif akind == "lights":
        clean_act = clean_action(action)
    else:
        raise ScheduleError("action.type must be scene, lights, switch or all_off")
    return {
        "id": str(raw.get("id") or "")[:64],
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "when": clean_when,
        "days": clean_days if kind != "once" else [],
        "random": max(0, min(60, jitter)),
        "action": clean_act,
        "delete_after": bool(raw.get("delete_after", False)) if kind == "once" else False,
        "updated_at": float(raw.get("updated_at") or time.time()),
    }


# ----------------------------------------------------------------- timing
def _local(day: date, hh: int, mm: int) -> datetime:
    # A naive local wall-clock time made aware in the Pi's own timezone.
    return datetime(day.year, day.month, day.day, hh, mm).astimezone()


def _jitter_s(sched: Dict[str, Any], day: date) -> float:
    minutes = sched.get("random") or 0
    if not minutes:
        return 0.0
    # Deterministic per schedule and day, so a restart does not re-roll it.
    digest = hashlib.sha256(f"{sched.get('id')}|{day.isoformat()}".encode()).digest()
    fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0..1
    return (fraction * 2 - 1) * minutes * 60


def occurrence(sched: Dict[str, Any], day: date, location: Dict[str, Any]) -> Optional[datetime]:
    """When ``sched`` runs on local calendar ``day``, or ``None`` if it doesn't."""
    when = sched.get("when") or {}
    if when.get("type") == "once":
        return None
    if day.weekday() not in (sched.get("days") or []):
        return None
    if when.get("type") == "time":
        hh, mm = (int(x) for x in when["time"].split(":"))
        base = _local(day, hh, mm)
    elif when.get("type") == "sun":
        lat, lon = location.get("lat"), location.get("lon")
        if lat is None or lon is None:
            return None
        rise, sets = sun_times(day, float(lat), float(lon))
        moment = rise if when["event"] == "sunrise" else sets
        if moment is None:
            return None
        base = moment.astimezone() + timedelta(minutes=when.get("offset") or 0)
    else:
        return None
    return base + timedelta(seconds=_jitter_s(sched, day))


def last_due(sched: Dict[str, Any], now: datetime, location: Dict[str, Any]) -> Optional[datetime]:
    """The most recent time ``sched`` should have run, at or before ``now``."""
    when = sched.get("when") or {}
    if when.get("type") == "once":
        at = datetime.fromtimestamp(float(when["at"])).astimezone()
        return at if at <= now else None
    best = None
    # Tomorrow's calendar day too: a 00:05 run with ±15 min of variation can
    # fall at 23:52 *today*.  Jitter and sun offsets can also push a run past
    # midnight the other way, hence the two days back.
    for back in range(-1, 3):
        t = occurrence(sched, (now - timedelta(days=back)).date(), location)
        if t is not None and t <= now and (best is None or t > best):
            best = t
    return best


def next_due(sched: Dict[str, Any], now: datetime, location: Dict[str, Any]) -> Optional[datetime]:
    """The next time ``sched`` will run after ``now`` (within 8 days)."""
    when = sched.get("when") or {}
    if when.get("type") == "once":
        at = datetime.fromtimestamp(float(when["at"])).astimezone()
        return at if at > now else None
    for ahead in range(-1, 9):
        t = occurrence(sched, (now + timedelta(days=ahead)).date(), location)
        if t is not None and t > now:
            return t
    return None


# ------------------------------------------------------------------ runner
class Scheduler:
    def __init__(self, store, runner: Callable[[Dict[str, Any]], Dict[str, Any]]):
        self.store = store
        self.runner = runner
        self.state_path = Path(store.data_dir) / "schedules-state.json"
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="schedule")
        self._running: set = set()
        self.started = False

    # persistence ---------------------------------------------------------
    def _load_state(self) -> None:
        import json

        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                self._state = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self._state = {}

    def _save_state(self) -> None:
        try:
            write_json_atomic(self.state_path, self._state)
        except OSError as exc:  # a full disk must not stop the schedules
            log.warning("could not save schedule state: %s", exc)

    # lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._load_state()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)
        self.started = False

    def _loop(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # pragma: no cover - the loop must survive anything
                log.exception("scheduler tick failed")

    # core ------------------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> List[str]:
        """Fire everything that is due.  Returns the ids fired (for tests)."""
        now = now or datetime.now().astimezone()
        cfg = self.store.get()
        location = cfg.get("location") if isinstance(cfg.get("location"), dict) else {}
        fired = []
        for sched in cfg.get("schedules", []):
            if not isinstance(sched, dict) or not sched.get("enabled", True) or not sched.get("id"):
                continue
            try:
                due = last_due(sched, now, location)
            except Exception as exc:  # a bad entry must not stop the others
                log.warning("schedule %s: %s", sched.get("name") or sched.get("id"), exc)
                continue
            if due is None:
                continue
            sid = sched["id"]
            with self._lock:
                last = float((self._state.get(sid) or {}).get("fired_for") or 0)
                if sid in self._running:
                    continue
                if due.timestamp() <= max(last, float(sched.get("updated_at") or 0)):
                    continue
                late = (now - due).total_seconds()
                if late > GRACE_S:
                    # Missed (the Pi was off): remember it so it is not
                    # reconsidered, and say so in the history.
                    self._state[sid] = {**(self._state.get(sid) or {}), "fired_for": due.timestamp(),
                                        "missed_at": due.timestamp()}
                    self._save_state()
                    if (sched.get("when") or {}).get("type") == "once":
                        self._finish_once(sched)
                    continue
                self._running.add(sid)
                self._state[sid] = {**(self._state.get(sid) or {}), "fired_for": due.timestamp()}
                self._save_state()
            fired.append(sid)
            try:
                self._pool.submit(self._fire, sched)
            except RuntimeError:  # shutting down
                with self._lock:
                    self._running.discard(sid)
        return fired

    def _fire(self, sched: Dict[str, Any]) -> None:
        sid = sched["id"]
        started = time.time()
        try:
            result = self.runner(sched["action"]) or {}
        except Exception as exc:  # pragma: no cover - runner already catches device errors
            log.exception("schedule %s failed", sched.get("name"))
            result = {"error": f"{type(exc).__name__}: {exc}"}
        ok = not result.get("error")
        message = result.get("error") or result.get("message") or "done"
        log.info("schedule '%s' ran: %s", sched.get("name") or sid, message)
        with self._lock:
            self._running.discard(sid)
            self._state[sid] = {**(self._state.get(sid) or {}), "last_run": started, "ok": ok, "message": str(message)[:300]}
            self._save_state()
        if (sched.get("when") or {}).get("type") == "once":
            self._finish_once(sched)

    def _finish_once(self, sched: Dict[str, Any]) -> None:
        sid = sched["id"]

        def mutate(cfg):
            for s in cfg.get("schedules", []):
                if s.get("id") == sid:
                    if s.get("delete_after"):
                        cfg["schedules"] = [x for x in cfg["schedules"] if x.get("id") != sid]
                    else:
                        s["enabled"] = False
                    return

        try:
            self.store.update(mutate)
        except Exception:  # pragma: no cover
            log.exception("could not retire one-off schedule %s", sid)

    def run_now(self, sched: Dict[str, Any]) -> Dict[str, Any]:
        """Run a schedule's action straight away (the "Run now" button).

        A one-off timer that is run by hand is used up: it must not go off a
        second time later, after somebody has switched things back on.
        """
        sid = sched["id"]
        with self._lock:
            if sid in self._running:
                return {"error": "it is already running"}
            self._running.add(sid)
        try:
            result = self.runner(sched["action"]) or {}
        except Exception as exc:  # pragma: no cover - runner already catches device errors
            log.exception("schedule %s failed", sched.get("name"))
            result = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._lock:
                self._running.discard(sid)
        once = (sched.get("when") or {}).get("type") == "once"
        with self._lock:
            entry = {**(self._state.get(sid) or {}), "last_run": time.time(), "ok": not result.get("error"),
                     "message": str(result.get("error") or result.get("message") or "done")[:300]}
            if once:
                entry["fired_for"] = float((sched.get("when") or {}).get("at") or time.time())
            self._state[sid] = entry
            self._save_state()
        if once:
            self._finish_once(sched)
        return result

    def describe(self, sched: Dict[str, Any], location: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now().astimezone()
        try:
            nxt = next_due(sched, now, location) if sched.get("enabled", True) else None
        except Exception:
            nxt = None
        with self._lock:
            st = dict(self._state.get(sched.get("id")) or {})
        needs_location = (sched.get("when") or {}).get("type") == "sun" and (location.get("lat") is None or location.get("lon") is None)
        return {**sched, "next_run": nxt.timestamp() if nxt else None, "last_run": st.get("last_run"),
                "last_ok": st.get("ok"), "last_message": st.get("message"), "missed_at": st.get("missed_at"),
                "needs_location": needs_location}

    def forget(self, sid: str) -> None:
        with self._lock:
            if self._state.pop(sid, None) is not None:
                self._save_state()
