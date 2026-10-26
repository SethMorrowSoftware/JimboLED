"""Regression tests for the last review round: a failed put-back after a
preset save is a warning not an error, a different controller at a light's
address is not mistaken for it, swapped controllers are followed, identify
survives a failed second press, and more."""
import time

import pytest

from jimboled import scheduler as S
from jimboled.config import ConfigStore
from jimboled.wled import intents as I
from jimboled.wled.manager import DeviceError, DeviceManager


@pytest.fixture
def two(fake_wled, data_dir):
    a = fake_wled("A", 60, "aabbccdd0f01")
    b = fake_wled("B", 60, "aabbccdd0f02")
    store = ConfigStore(data_dir)
    store.update(lambda c: c.update({"devices": [{"id": "a", "name": "A", "host": a.host}, {"id": "b", "name": "B", "host": b.host}],
                                     "wled": {"poll_interval_s": 60, "offline_poll_interval_s": 60, "request_timeout_s": 2}}))
    m = DeviceManager(store)
    m.start()
    m.refresh("a"); m.refresh("b")
    yield m, a, b, store
    m.stop()


def test_copy_that_saved_but_could_not_put_back_is_partial(two):
    m, a, b, _ = two
    m.save_preset("a", 4, "Glow", state={"on": True, "bri": 150, "seg": [{"id": 0, "fx": 2}]})
    m._put_back = lambda *args, **kw: "no reply"
    rows = m.copy_preset("a", 4, ["b"])
    assert rows[0]["status"] == "partial" and "put back" in rows[0]["message"]
    assert rows[0]["slot"] and any(v.get("n") == "Glow" for v in b.fake["presets"].values() if v)


def test_rename_that_could_not_put_back_answers_with_a_warning(app, client, fake_wled):
    srv = fake_wled("R", 30, "aabbccdd0f03")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    srv.fake["presets"]["3"] = {"n": "Movie", "on": True, "bri": 200, "seg": [{"id": 0, "fx": 2}]}
    ctx = app.extensions["jimboled"]
    ctx.devices._load_catalog(ctx.devices._get(did), force=True)
    ctx.devices._put_back = lambda *args, **kw: "no reply"
    r = client.post(f"/api/devices/{did}/presets", json={"slot": 3, "name": "Film", "rename": True})
    assert r.status_code == 200 and "put back" in r.json["warning"]
    assert any(p["name"] == "Film" for p in r.json["presets"])


def test_a_different_controller_at_the_address_is_not_this_light(two):
    m, a, _, _ = two
    rec = m._get("a")
    assert rec.cfg.get("mac") == "aabbccdd0f01"
    rec.cfg["mac"] = "001122334455"  # as if another controller now answers here
    m.refresh("a")
    assert not rec.online and "different WLED" in rec.last_error
    a.fake["state"]["bri"] = 77
    with pytest.raises(DeviceError):
        m.set_state("a", {"bri": 5})
    assert a.fake["state"]["bri"] == 77  # nothing was sent to it
    rec.cfg["mac"] = "aabbccdd0f01"
    m.refresh("a")
    assert rec.online and not rec.wrong_device
    m.set_state("a", {"bri": 5})
    assert a.fake["state"]["bri"] == 5


def test_mac_is_not_written_to_an_entry_whose_address_changed(two):
    m, a, _, store = two
    rec = m._get("a")
    rec.cfg["mac"] = ""
    store.update(lambda c: [d.update({"host": "127.0.0.1:9", "mac": ""}) for d in c["devices"] if d["id"] == "a"])
    m._remember_mac(rec)  # a poll of the old address finishing late
    assert next(d for d in store.get()["devices"] if d["id"] == "a")["mac"] == ""


def test_editing_the_address_forgets_the_mac(client, fake_wled):
    one, other = fake_wled("X", 30, "aabbccdd0f04"), fake_wled("Y", 30, "aabbccdd0f05")
    did = client.post("/api/devices", json={"host": one.host}).json["device"]["id"]
    client.put(f"/api/devices/{did}", json={"name": "Renamed"})
    assert client.application.extensions["jimboled"].store.get()["devices"][0]["mac"] == "aabbccdd0f04"
    client.put(f"/api/devices/{did}", json={"host": other.host})
    assert client.application.extensions["jimboled"].store.get()["devices"][0]["mac"] == ""


def test_two_controllers_that_swapped_addresses_are_both_followed(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    x, y = fake_wled("X", 30, "aabbccdd0f06"), fake_wled("Y", 30, "aabbccdd0f07")
    dx = client.post("/api/devices", json={"host": x.host}).json["device"]["id"]
    dy = client.post("/api/devices", json={"host": y.host}).json["device"]["id"]
    # The router swapped them: each light's address now answers with the other's MAC.
    hosts = {dx: y.host, dy: x.host}
    ctx.store.update(lambda c: [d.update({"host": hosts[d["id"]]}) for d in c["devices"]])
    for did in (dx, dy):
        for _ in range(2):
            ctx.devices.refresh(did)
        assert not ctx.devices.summary(did)["online"]
    ctx.discovery._started_at = time.time() - 120
    moved = ctx.discovery.follow_moved([{"host": x.host, "mac": "aabbccdd0f06"}, {"host": y.host, "mac": "aabbccdd0f07"}])
    assert sorted(moved) == sorted([dx, dy])
    final = {d["id"]: d["host"] for d in ctx.store.get()["devices"]}
    assert final == {dx: x.host, dy: y.host}


def test_identify_that_fails_the_second_time_still_puts_the_light_back(two):
    m, a, _, _ = two
    m.set_state("a", {"on": True, "bri": 40, "seg": [{"id": 0, "fx": 2, "col": [[255, 128, 0]]}]})
    m.identify("a", seconds=1.0)
    time.sleep(0.2)
    a.fake["fail"] = True
    with pytest.raises(DeviceError):
        m.identify("a", seconds=1.0)
    a.fake["fail"] = False
    time.sleep(1.4)
    st = a.fake["state"]
    assert st["bri"] == 40 and st["seg"][0]["fx"] == 2 and st["seg"][0]["col"][0][:3] == [255, 128, 0]


def test_sync_riding_along_with_something_this_light_cannot_do_is_skipped():
    rgb_only = I.DeviceView(name="x", online=True, state={"seg": [{"id": 0, "col": [[1, 2, 3]]}]},
                            info={"leds": {"lc": 1, "seglc": [1]}}, effects=[], palettes=[], presets={})
    payload, notes = I.plan(rgb_only, I.normalise_intent({"preset": "Movie", "sync": True}))
    assert payload is None and notes
    payload, _ = I.plan(rgb_only, I.normalise_intent({"sync": True}))
    assert payload == {"tb": 0}


def test_no_undo_requests_are_not_remembered(client, fake_wled):
    srv = fake_wled("N", 30, "aabbccdd0f08")
    client.post("/api/devices", json={"host": srv.host})
    r = client.post("/api/lights/apply", json={"target": "all", "intent": {"bri": 30}, "no_undo": True}).json
    assert r["changed"] and "undo" not in r
    r = client.post("/api/lights/apply", json={"target": "all", "intent": {"bri": 90}}).json
    assert r.get("undo")
    client.post(f"/api/lights/undo/{r['undo']}")
    assert srv.fake["state"]["bri"] == 30


def test_schedule_with_a_number_id_can_fire_again(data_dir):
    """A hand-edited config may hold ``"id": 7``; firing must not leave it
    "already running" for ever, and its last run must show."""
    from datetime import datetime

    store = ConfigStore(data_dir)
    sched = {"id": 7, "name": "T", "enabled": True, "when": {"type": "once", "at": time.time() - 5},
             "action": {"type": "all_off"}}
    store.update(lambda c: c.update({"schedules": [dict(sched)]}))
    sch = S.Scheduler(store, lambda a: {"message": "ok"})
    try:
        assert sch._consider(sched, datetime.now().astimezone(), {}, False)
        deadline = time.time() + 3
        while sch._running and time.time() < deadline:
            time.sleep(0.05)
        assert not sch._running
        assert sch.describe(sched, {})["last_run"]
        assert store.get()["schedules"][0]["enabled"] is False
    finally:
        sch.stop()


def test_tokens_cannot_copy_presets_onto_controllers(app, client, fake_wled):
    srv = fake_wled("P", 30, "aabbccdd0f09")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    token = client.post("/api/tokens", json={"name": "HA"}).json["token"]
    bare = app.test_client()
    r = bare.post(f"/api/devices/{did}/presets/1/copy", json={"target": "all"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
