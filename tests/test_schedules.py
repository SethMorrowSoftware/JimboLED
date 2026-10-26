"""Schedules: the timing maths, the runner and the REST API."""
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from jimboled import scheduler as S
from jimboled.config import ConfigStore
from jimboled.sun import sun_times

MONDAY = date(2026, 10, 26)
LONDON = {"lat": 51.5074, "lon": -0.1278, "label": "London"}


def local(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss).astimezone()


def sched(**kw):
    base = {"id": "s1", "name": "Morning", "enabled": True, "when": {"type": "time", "time": "07:00"},
            "days": list(range(7)), "random": 0, "action": {"type": "all_off"}, "delete_after": False,
            "updated_at": 0}
    base.update(kw)
    return base


class SyncPool:
    """Runs submitted work straight away so tests need no waiting."""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=False):
        pass


@pytest.fixture
def rig(tmp_path):
    store = ConfigStore(tmp_path)
    calls = []

    def runner(action):
        calls.append(action)
        return {"message": "ok"}

    sch = S.Scheduler(store, runner)
    sch._pool = SyncPool()
    return store, sch, calls


def _set(store, *schedules, location=None):
    def mutate(cfg):
        cfg["schedules"] = list(schedules)
        if location is not None:
            cfg["location"] = location
    store.update(mutate)


# ----------------------------------------------------------------- timing
def test_sun_times_are_sensible():
    rise, sets = sun_times(date(2026, 6, 21), 51.5074, -0.1278)
    assert rise.astimezone(timezone.utc).hour in (3, 4) and sets.astimezone(timezone.utc).hour in (20, 21)
    rise, sets = sun_times(date(2026, 12, 21), -33.87, 151.21)  # Sydney summer
    assert (sets - rise) > timedelta(hours=14)
    assert sun_times(date(2026, 6, 21), 69.65, 18.96) == (None, None)  # Tromsø midnight sun


def test_normalise_schedule_validation():
    clean = lambda a: a  # noqa: E731
    with pytest.raises(S.ScheduleError):
        S.normalise_schedule({"when": {"type": "time", "time": "25:00"}, "action": {"type": "all_off"}}, clean_action=clean)
    with pytest.raises(S.ScheduleError):
        S.normalise_schedule({"when": {"type": "time", "time": "07:00"}, "days": [], "action": {"type": "all_off"}}, clean_action=clean)
    with pytest.raises(S.ScheduleError):
        S.normalise_schedule({"when": {"type": "time", "time": "07:00"}, "action": {"type": "switch", "ref": "x", "action": "explode"}}, clean_action=clean)
    out = S.normalise_schedule({"when": {"type": "sun", "event": "sunset", "offset": 999}, "days": [0, 0, 9, 3],
                                "random": 500, "action": {"type": "all_off"}}, clean_action=clean)
    assert out["when"]["offset"] == 180 and out["days"] == [0, 3] and out["random"] == 60
    once = S.normalise_schedule({"when": {"type": "once", "at": time.time() + 60}, "action": {"type": "all_off"},
                                 "delete_after": True}, clean_action=clean)
    assert once["days"] == [] and once["delete_after"] is True


def test_occurrence_respects_days_and_jitter():
    s = sched(days=[0])  # Mondays only
    assert S.occurrence(s, MONDAY, {}) == local(MONDAY, 7, 0)
    assert S.occurrence(s, MONDAY + timedelta(days=1), {}) is None
    j = sched(random=20)
    a, b = S.occurrence(j, MONDAY, {}), S.occurrence(j, MONDAY, {})
    assert a == b and abs((a - local(MONDAY, 7, 0)).total_seconds()) <= 20 * 60
    nxt = S.next_due(s, local(MONDAY, 7, 30), {})
    assert nxt == local(MONDAY + timedelta(days=7), 7, 0)


def test_sun_schedule_needs_a_location():
    s = sched(when={"type": "sun", "event": "sunset", "offset": 15})
    assert S.occurrence(s, MONDAY, {}) is None
    t = S.occurrence(s, MONDAY, LONDON)
    rise, sets = sun_times(MONDAY, LONDON["lat"], LONDON["lon"])
    assert t == sets.astimezone() + timedelta(minutes=15)


# ------------------------------------------------------------------ runner
def test_fires_once_within_grace_and_survives_restart(rig, tmp_path):
    store, sch, calls = rig
    _set(store, sched())
    assert sch.tick(local(MONDAY, 6, 59)) == []
    assert sch.tick(local(MONDAY, 7, 1)) == ["s1"]
    assert sch.tick(local(MONDAY, 7, 2)) == []
    assert calls == [{"type": "all_off"}]
    again = S.Scheduler(store, lambda a: calls.append(a) or {})
    again._pool = SyncPool()
    again._load_state()
    assert again.tick(local(MONDAY, 7, 2)) == []  # remembered across a restart
    assert again.tick(local(MONDAY + timedelta(days=1), 7, 0, 30)) == ["s1"]


def test_missed_run_is_skipped_and_recorded(rig):
    store, sch, calls = rig
    _set(store, sched())
    assert sch.tick(local(MONDAY, 9, 0)) == []
    assert calls == []
    info = sch.describe(store.get()["schedules"][0], {}, local(MONDAY, 9, 0))
    assert info["missed_at"] == local(MONDAY, 7, 0).timestamp()
    assert info["next_run"] == local(MONDAY + timedelta(days=1), 7, 0).timestamp()


def test_disabled_and_freshly_edited_do_not_fire(rig):
    store, sch, calls = rig
    _set(store, sched(enabled=False), sched(id="s2", updated_at=local(MONDAY, 7, 0, 30).timestamp()))
    assert sch.tick(local(MONDAY, 7, 1)) == []
    assert calls == []


def test_one_off_timers_retire_themselves(rig):
    store, sch, calls = rig
    at = local(MONDAY, 22, 0).timestamp()
    _set(store, sched(id="t1", when={"type": "once", "at": at}, days=[], delete_after=True),
         sched(id="t2", when={"type": "once", "at": at}, days=[], delete_after=False))
    assert sorted(sch.tick(local(MONDAY, 22, 0, 10))) == ["t1", "t2"]
    remaining = store.get()["schedules"]
    assert [s["id"] for s in remaining] == ["t2"] and remaining[0]["enabled"] is False


def test_failed_action_is_reported(tmp_path):
    store = ConfigStore(tmp_path)
    sch = S.Scheduler(store, lambda a: {"error": "Bed is offline"})
    sch._pool = SyncPool()
    _set(store, sched())
    sch.tick(local(MONDAY, 7, 0, 5))
    info = sch.describe(store.get()["schedules"][0], {}, local(MONDAY, 7, 1))
    assert info["last_ok"] is False and "offline" in info["last_message"]
    state = json.loads((tmp_path / "schedules-state.json").read_text())
    assert state["s1"]["ok"] is False


# --------------------------------------------------------------------- API
def test_schedule_api_crud(client):
    r = client.post("/api/schedules", json={"name": "Night", "when": {"type": "time", "time": "23:30"},
                                            "days": [0, 1, 2, 3, 4], "action": {"type": "all_off"}})
    assert r.status_code == 201, r.json
    sid = r.json["schedule"]["id"]
    listing = client.get("/api/schedules").json
    item = next(s for s in listing["schedules"] if s["id"] == sid)
    assert item["next_run"] and item["problem"] is None and listing["running"]
    assert "timezone" in listing and "now" in listing
    before = item["updated_at"]
    time.sleep(0.01)
    r = client.patch(f"/api/schedules/{sid}", json={"enabled": False})
    item = next(s for s in r.json["schedules"] if s["id"] == sid)
    assert item["enabled"] is False and item["next_run"] is None and item["updated_at"] > before
    r = client.post(f"/api/schedules/{sid}/run")
    assert r.json["ok"] is True
    item = next(s for s in r.json["schedules"] if s["id"] == sid)
    assert item["last_ok"] is True and item["last_run"]
    assert client.delete(f"/api/schedules/{sid}").status_code == 200
    assert client.delete(f"/api/schedules/{sid}").status_code == 404
    assert client.post("/api/schedules", json={"when": {"type": "time", "time": "7am"}, "action": {"type": "all_off"}}).status_code == 400


def test_schedule_refs_are_checked(client):
    r = client.post("/api/schedules", json={"when": {"type": "time", "time": "07:00"}, "action": {"type": "scene", "ref": "nope"}})
    assert r.status_code == 400 and "scene" in r.json["error"]
    up = client.post("/api/gpio/switches", json={"name": "Bed up", "pin": 17, "mode": "momentary"}).json["switch"]["id"]
    r = client.post("/api/schedules", json={"when": {"type": "time", "time": "07:00"},
                                            "action": {"type": "switch", "ref": up, "action": "on"}})
    assert r.status_code == 400 and "hold-to-run" in r.json["error"]
    r = client.post("/api/schedules", json={"when": {"type": "time", "time": "07:00"},
                                            "action": {"type": "switch", "ref": up, "action": "off"}})
    assert r.status_code == 201
    r = client.post("/api/schedules", json={"when": {"type": "time", "time": "07:00"},
                                            "action": {"type": "lights", "target": "group:gone", "intent": {"on": True}}})
    assert r.status_code == 400 and "group" in r.json["error"]


def test_sun_schedule_and_location(client):
    r = client.post("/api/schedules", json={"name": "Porch", "when": {"type": "sun", "event": "sunset", "offset": 10},
                                            "action": {"type": "lights", "intent": {"on": True, "color": "#ffaa00"}}})
    assert r.status_code == 201
    item = r.json["schedules"][0]
    assert item["needs_location"] and "location" in item["problem"].lower() and item["next_run"] is None
    assert client.put("/api/location", json={"lat": 200, "lon": 0}).status_code == 400
    assert client.put("/api/location", json={"lat": "x", "lon": 0}).status_code == 400
    r = client.put("/api/location", json={"lat": 51.50735, "lon": -0.12776, "label": "Home"})
    assert r.status_code == 200 and r.json["location"]["lat"] == 51.5074
    item = r.json["schedules"][0]
    assert item["problem"] is None and item["next_run"]
    loc = client.get("/api/location?zones=1").json
    assert loc["sun"]["sunrise"] and loc["sun"]["sunset"] and "Europe/London" in loc["zones"]
    r = client.put("/api/location", json={"timezone": "Not/AZone"})
    assert r.status_code in (400, 501)
    assert client.put("/api/location", json={"timezone": "../../etc/passwd"}).status_code == 400


def test_timers(client):
    assert client.post("/api/timers", json={"minutes": 0}).status_code == 400
    assert client.post("/api/timers", json={"minutes": 99999}).status_code == 400
    r = client.post("/api/timers", json={"minutes": 30, "action": {"type": "lights", "intent": {"on": False}}})
    assert r.status_code == 201
    t = r.json["schedule"]
    assert t["when"]["type"] == "once" and t["delete_after"] and abs(t["when"]["at"] - (time.time() + 1800)) < 5
    assert t["name"] == "Sleep timer"


def test_scheduled_scene_action_runs(app, client):
    from jimboled import run_scheduled_action

    ctx = app.extensions["jimboled"]
    sw = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 22}).json["switch"]["id"]
    scene = client.post("/api/scenes", json={"name": "Lamp on", "actions": [{"type": "switch", "ref": sw, "action": "on"}]}).json["scene"]
    out = run_scheduled_action(ctx, {"type": "scene", "ref": scene["id"]})
    assert "Lamp on" in out["message"] and ctx.gpio.snapshot()["switches"][0]["on"] is True
    assert run_scheduled_action(ctx, {"type": "scene", "ref": "gone"})["error"]
    assert run_scheduled_action(ctx, {"type": "switch", "ref": sw, "action": "off"}) == {"message": "done"}
    assert ctx.gpio.snapshot()["switches"][0]["on"] is False
