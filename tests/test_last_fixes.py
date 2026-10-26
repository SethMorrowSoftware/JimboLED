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
    # Learned afresh from the controller at the new address.
    assert client.application.extensions["jimboled"].store.get()["devices"][0]["mac"] == "aabbccdd0f05"


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


def test_a_replaced_controller_is_taken_on_by_saving_the_light(app, client, fake_wled):
    srv = fake_wled("Porch", 30, "aabbccdd0f10")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    ctx = app.extensions["jimboled"]
    srv.fake["info"]["mac"] = "aabbccdd0f99"  # a new board at the same address
    ctx.devices.refresh(did)
    summary = client.get("/api/devices").json["devices"][0]
    assert summary["wrong_device"] and "save" in summary["last_error"]
    # A plain save (the address included, as the edit dialog always sends it)
    # doesn't take it on: only one made after the warning was shown does.
    client.put(f"/api/devices/{did}", json={"name": "Porch light", "host": srv.host})
    assert ctx.devices.wrong_device(did) and ctx.store.get()["devices"][0]["mac"] == "aabbccdd0f10"
    r = client.put(f"/api/devices/{did}", json={"name": "Porch light", "host": srv.host, "replace": True})
    assert r.status_code == 200 and r.json["device"]["online"] and not r.json["device"]["wrong_device"]
    assert ctx.store.get()["devices"][0]["mac"] == "aabbccdd0f99"
    assert client.post(f"/api/devices/{did}/state", json={"bri": 12}).status_code == 200
    assert srv.fake["state"]["bri"] == 12


def test_overwriting_a_preset_under_its_own_name_waits_for_the_new_content(two):
    m, a, _, _ = two
    m.set_state("a", {"on": True, "bri": 50})
    m.save_preset("a", 6, "Movie")
    assert a.fake["presets"]["6"]["bri"] == 50
    a.fake["save_delay"] = 0.6  # the controller writes presets.json a little later
    m.set_state("a", {"bri": 222})
    presets = m.save_preset("a", 6, "Movie")
    assert m._get("a").presets["6"]["bri"] == 222
    assert any(p["id"] == 6 for p in presets)


def test_replace_never_takes_another_configured_lights_controller(app, client, fake_wled):
    """Two controllers swapped addresses: "replacing" one with the other would
    leave one light driving the other's strip and nothing following."""
    ctx = app.extensions["jimboled"]
    x, y = fake_wled("X", 30, "aabbccdd0f11"), fake_wled("Y", 30, "aabbccdd0f12")
    dx = client.post("/api/devices", json={"host": x.host}).json["device"]["id"]
    dy = client.post("/api/devices", json={"host": y.host}).json["device"]["id"]
    hosts = {dx: y.host, dy: x.host}
    ctx.store.update(lambda c: [d.update({"host": hosts[d["id"]]}) for d in c["devices"]])
    for did in (dx, dy):
        for _ in range(2):
            ctx.devices.refresh(did)
    r = client.put(f"/api/devices/{dx}", json={"name": "X light", "host": y.host, "replace": True})
    assert r.status_code == 200 and "“Y”" in r.json.get("warning", "")
    macs = {d["id"]: d["mac"] for d in ctx.store.get()["devices"]}
    assert macs == {dx: "aabbccdd0f11", dy: "aabbccdd0f12"}
    ctx.discovery._started_at = time.time() - 120
    moved = ctx.discovery.follow_moved([{"host": x.host, "mac": "aabbccdd0f11"}, {"host": y.host, "mac": "aabbccdd0f12"}])
    assert sorted(moved) == sorted([dx, dy])


def test_a_light_not_learned_yet_never_adopts_another_lights_controller(app, client, fake_wled):
    """'Garage' was added with "add anyway" (no MAC yet); after a power cut the
    router gives its address to Porch's controller."""
    ctx = app.extensions["jimboled"]
    porch_board = fake_wled("Porch", 30, "aabbccdd0f20")
    porch = client.post("/api/devices", json={"host": porch_board.host}).json["device"]["id"]
    garage = client.post("/api/devices", json={"host": "127.0.0.1:9", "name": "Garage", "skip_probe": True}).json["device"]["id"]
    hosts = {porch: "127.0.0.1:8", garage: porch_board.host}  # Porch's old address is dead now
    ctx.store.update(lambda c: [d.update({"host": hosts[d["id"]]}) for d in c["devices"]])
    for _ in range(2):
        ctx.devices.refresh(garage)
        ctx.devices.refresh(porch)
    assert ctx.devices.wrong_device(garage) and "Porch" in ctx.devices.summary(garage)["last_error"]
    assert next(d for d in ctx.store.get()["devices"] if d["id"] == garage)["mac"] == ""
    porch_board.fake["state"]["bri"] = 66
    assert client.post(f"/api/devices/{garage}/state", json={"bri": 3}).status_code != 200
    assert porch_board.fake["state"]["bri"] == 66
    # Porch is followed to the address that Garage still names.
    ctx.discovery._started_at = time.time() - 120
    assert ctx.discovery.follow_moved([{"host": porch_board.host, "mac": "aabbccdd0f20"}]) == [porch]


def test_two_lights_cannot_be_given_the_same_address(client, fake_wled):
    one, two_ = fake_wled("One", 30, "aabbccdd0f21"), fake_wled("Two", 30, "aabbccdd0f22")
    a = client.post("/api/devices", json={"host": one.host}).json["device"]["id"]
    client.post("/api/devices", json={"host": two_.host})
    r = client.put(f"/api/devices/{a}", json={"host": two_.host})
    assert r.status_code == 409 and "already added" in r.json["error"]


def test_adding_a_moved_controller_again_updates_the_light(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    old = fake_wled("Porch", 30, "aabbccdd0f23")
    did = client.post("/api/devices", json={"host": old.host}).json["device"]["id"]
    # Still online: adding the same controller by another address is refused.
    twin = fake_wled("Porch", 30, "aabbccdd0f23")
    assert client.post("/api/devices", json={"host": twin.host}).status_code == 409
    old.stop()
    for _ in range(2):
        ctx.devices.refresh(did)
    r = client.post("/api/devices", json={"host": twin.host})
    assert r.status_code == 200 and r.json["followed"] and r.json["device"]["id"] == did and r.json["device"]["online"]
    assert [d["host"] for d in ctx.store.get()["devices"]] == [twin.host]


def test_entering_a_new_address_after_the_warning_is_not_a_takeover(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    x, y = fake_wled("X", 30, "aabbccdd0f24"), fake_wled("Y", 30, "aabbccdd0f25")
    dx = client.post("/api/devices", json={"host": x.host}).json["device"]["id"]
    dy = client.post("/api/devices", json={"host": y.host}).json["device"]["id"]
    # Y's controller now answers at X's address (and X's own is elsewhere).
    x.fake["info"]["mac"] = "aabbccdd0f25"
    ctx.devices.refresh(dx)
    assert ctx.devices.wrong_device(dx)
    elsewhere = fake_wled("X", 30, "aabbccdd0f24")
    r = client.put(f"/api/devices/{dx}", json={"host": elsewhere.host, "replace": True})
    assert r.status_code == 200 and "warning" not in r.json
    assert {d["id"]: d["host"] for d in ctx.store.get()["devices"]} == {dx: elsewhere.host, dy: y.host}
    assert r.json["device"]["online"] and not r.json["device"]["wrong_device"]
