"""Regression tests for the final review: preset saves leave the lights alone,
undo is complete and scoped, tokens can't edit presets via /state, identify
can be repeated, schedules survive DST gaps and bad entries, and more."""
import json
import time
from datetime import date, datetime, timedelta

import pytest

from jimboled import scheduler as S
from jimboled.config import ConfigStore
from jimboled.wled import intents as I
from jimboled.wled.manager import DeviceManager


@pytest.fixture
def two(fake_wled, data_dir):
    a = fake_wled("A", 60, "aabbccdd0e01")
    b = fake_wled("B", 60, "aabbccdd0e02")
    store = ConfigStore(data_dir)
    store.update(lambda c: c.update({"devices": [{"id": "a", "name": "A", "host": a.host}, {"id": "b", "name": "B", "host": b.host}],
                                     "wled": {"poll_interval_s": 60, "offline_poll_interval_s": 60, "request_timeout_s": 2}}))
    m = DeviceManager(store)
    m.start()
    m.refresh("a"); m.refresh("b")
    yield m, a, b
    m.stop()


def _look(srv):
    st = srv.fake["state"]
    return (st["on"], st["bri"], [(s["id"], s["fx"], s["col"][0][:3]) for s in st["seg"]], st.get("pl", -1))


def test_renaming_a_preset_leaves_the_lights_alone(two):
    m, a, _ = two
    m.set_state("a", {"on": False, "bri": 30})
    a.fake["presets"]["3"] = {"n": "Movie", "on": True, "bri": 200, "seg": [{"id": 0, "fx": 2, "col": [[255, 0, 0]]}]}
    a.fake["presets"]["8"] = {"n": "Party", "on": True, "playlist": {"ps": [3], "dur": [30], "transition": [7]}}
    m._load_catalog(m._get("a"), force=True)
    before = _look(a)
    m.rename_preset("a", 3, "Movie night")
    m.rename_preset("a", 8, "Party time")
    assert _look(a) == before  # still off, no playlist running
    assert a.fake["presets"]["3"]["n"] == "Movie night" and a.fake["presets"]["8"]["playlist"]["ps"] == [3]


def test_copying_a_preset_leaves_the_targets_alone(two):
    m, a, b = two
    m.save_preset("a", 4, "Glow", state={"on": True, "bri": 150, "seg": [{"id": 0, "fx": 2}]})
    m.set_state("b", {"on": False})
    before = _look(b)
    rows = m.copy_preset("a", 4, ["b"])
    assert rows[0]["status"] in ("ok", "partial")
    assert _look(b) == before
    assert any(v.get("n") == "Glow" for v in b.fake["presets"].values() if v)


def test_copy_uses_the_targets_real_preset_list(two):
    m, a, b = two
    m.save_preset("a", 4, "Movie", state={"on": True, "seg": [{"id": 0, "fx": 2}]})
    b.fake["presets"]["4"] = {"n": "Grandma's reading light", "on": True, "bri": 90}
    m._get("b").presets = {}  # the cached list has not loaded yet
    m.copy_preset("a", 4, ["b"])
    assert b.fake["presets"]["4"]["n"] == "Grandma's reading light"
    assert any(v.get("n") == "Movie" for k, v in b.fake["presets"].items() if k != "4" and v)


def test_rename_refuses_http_api_presets(two):
    m, a, _ = two
    a.fake["presets"]["5"] = {"n": "Reboot", "win": "&RB"}
    m._load_catalog(m._get("a"), force=True)
    from jimboled.wled.manager import DeviceError
    with pytest.raises(DeviceError):
        m.rename_preset("a", 5, "x")


def test_undo_removes_new_segments_and_stops_a_fade(client, fake_wled):
    srv = fake_wled("U", 60, "aabbccdd0e03")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    srv.fake["presets"]["6"] = {"n": "Zones", "on": True, "seg": [{"id": 0, "start": 0, "stop": 20, "fx": 0},
                                                              {"id": 1, "start": 20, "stop": 40, "fx": 9},
                                                              {"id": 2, "start": 40, "stop": 60, "fx": 3}]}
    client.post(f"/api/devices/{did}/catalog")
    r = client.post("/api/lights/apply", json={"preset": "Zones", "nightlight": {"on": True, "minutes": 30}}).json
    assert len(srv.fake["state"]["seg"]) == 3 and srv.fake["state"]["nl"]["on"] is True
    u = client.post(f"/api/lights/undo/{r['undo']}").json
    assert u["ok"]
    assert [s["id"] for s in srv.fake["state"]["seg"]] == [0] and srv.fake["state"]["seg"][0]["stop"] == 60
    assert srv.fake["state"]["nl"]["on"] is False


def test_undo_does_not_touch_lights_the_change_skipped(client, fake_wled):
    a = fake_wled("A", 30, "aabbccdd0e04")
    b = fake_wled("B", 30, "aabbccdd0e05")
    da = client.post("/api/devices", json={"host": a.host}).json["device"]["id"]
    db = client.post("/api/devices", json={"host": b.host}).json["device"]["id"]
    client.post(f"/api/devices/{da}/presets", json={"name": "Movie", "slot": 9})
    r = client.post("/api/lights/apply", json={"preset": "Movie"}).json  # B has no Movie: skipped
    client.post(f"/api/devices/{db}/state", json={"seg": [{"id": 0, "col": [[0, 0, 255]]}]})
    client.post(f"/api/lights/undo/{r['undo']}")
    assert b.fake["state"]["seg"][0]["col"][0][:3] == [0, 0, 255]


def test_tokens_cannot_store_or_delete_presets_via_state(client, app, fake_wled):
    srv = fake_wled("T", 30, "aabbccdd0e06")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    token = client.post("/api/tokens", json={"name": "x"}).json["token"]
    auth = {"Authorization": f"Bearer {token}"}
    bare = app.test_client()
    for path, payload in ((f"/api/devices/{did}/state", {"pdel": 1}), ("/api/devices/all/state", {"psave": 2, "n": "pwned"}),
                          (f"/api/devices/{did}/state", {"state": {"pdel": 1}})):
        assert bare.post(path, json=payload, headers=auth).status_code == 403, payload
    assert bare.post(f"/api/devices/{did}/state", json={"on": False}, headers=auth).status_code == 200


def test_identify_twice_still_puts_the_light_back(two):
    m, a, _ = two
    m.set_state("a", {"on": True, "bri": 40, "seg": [{"id": 0, "fx": 2, "col": [[255, 128, 0]]}]})
    m.identify("a", seconds=1.0)
    time.sleep(0.3)
    m.identify("a", seconds=1.0)
    time.sleep(1.6)
    st = a.fake["state"]
    assert st["bri"] == 40 and st["seg"][0]["fx"] == 2 and st["seg"][0]["col"][0][:3] == [255, 128, 0]


def test_sync_on_its_own_is_sent():
    view = I.DeviceView(name="x", online=True, state={"seg": [{"id": 0}]}, info={}, effects=[], palettes=[], presets={})
    payload, _ = I.plan(view, I.normalise_intent({"sync": True}))
    assert payload == {"tb": 0}


def test_kelvin_per_segment_on_mixed_boards():
    view = I.DeviceView(name="x", online=True, state={"seg": [{"id": 0, "col": [[1, 2, 3]]}, {"id": 1, "col": [[1, 2, 3, 4]]}]},
                        info={"leds": {"lc": 7, "seglc": [1, 6]}}, effects=[], palettes=[], presets={})
    payload, _ = I.plan(view, I.normalise_intent({"kelvin": 2700}))
    assert payload["seg"][0]["col"][0] == I.kelvin_to_rgb(2700)
    assert payload["seg"][1]["col"][0] == [0, 0, 0, 255] and "cct" not in payload["seg"][0]


def test_dst_gap_runs_after_the_jump(monkeypatch):
    import os
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        t = S._local(date(2026, 3, 8), 2, 30)
        assert (t.hour, t.minute) == (3, 0)
        assert S._local(date(2026, 3, 9), 2, 30).hour == 2
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_a_bad_schedule_does_not_stop_the_others(tmp_path):
    store = ConfigStore(tmp_path)
    calls = []
    sch = S.Scheduler(store, lambda a: calls.append(a) or {"message": "ok"})

    class Sync:
        def submit(self, fn, *a):
            fn(*a)

        def shutdown(self, wait=False):
            pass

    sch._pool = Sync()
    day = date(2026, 10, 26)
    good = {"id": "good", "enabled": True, "when": {"type": "time", "time": "07:00"}, "days": list(range(7)),
            "action": {"type": "all_off"}, "updated_at": 0}
    bad = dict(good, id="bad", updated_at="2024-01-01")
    store.update(lambda c: c.__setitem__("schedules", [bad, good]))
    assert sch.tick(datetime(day.year, day.month, day.day, 7, 0, 30).astimezone()) == ["good"]


def test_used_up_timers_are_forgotten(client, app):
    ctx = app.extensions["jimboled"]
    t = client.post("/api/timers", json={"minutes": 30}).json["schedule"]
    client.post(f"/api/schedules/{t['id']}/run")
    assert t["id"] in ctx.scheduler._state
    ctx.scheduler.tick()
    assert t["id"] not in ctx.scheduler._state


def test_login_form_is_throttled(client, app):
    client.post("/api/settings/password", json={"password": "abcd"})
    form = app.test_client()
    codes = [form.post("/login", data={"password": "wrong"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert form.post("/login", data={"password": "abcd"}).status_code == 429  # still waiting


def test_schedule_on_a_removed_light_is_flagged(client, fake_wled):
    srv = fake_wled("P", 30, "aabbccdd0e07")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    sid = client.post("/api/schedules", json={"when": {"type": "time", "time": "23:00"},
                                              "action": {"type": "lights", "target": {"devices": [did]}, "intent": {"on": False}}}).json["schedule"]["id"]
    client.delete(f"/api/devices/{did}")
    item = next(s for s in client.get("/api/schedules").json["schedules"] if s["id"] == sid)
    assert item["problem"] == "Its lights have been removed."
