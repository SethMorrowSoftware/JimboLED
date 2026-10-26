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
    # Another controller at the new address: only when confirmed …
    r = client.put(f"/api/devices/{did}", json={"host": other.host})
    assert r.status_code == 409 and r.json["code"] == "different_controller"
    assert client.application.extensions["jimboled"].store.get()["devices"][0]["host"] == one.host
    # … and then it is this light's controller.
    r = client.put(f"/api/devices/{did}", json={"host": other.host, "new_controller": True})
    assert r.status_code == 200 and r.json["device"]["online"]
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


def _legacy(ctx, entry):
    """A light as configs from before MACs were remembered stored it."""
    ctx.store.update(lambda c: c["devices"].append({"id": entry[0], "name": entry[1], "host": entry[2], "enabled": True,
                                                     "icon": "bulb", "color": "", "notes": "", "poll_interval_s": None}))


def test_a_light_from_before_macs_were_remembered_is_recognised(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    _legacy(ctx, ("wled-dd0f30", "Xmas", "127.0.0.1:9"))
    _legacy(ctx, ("wled-dd0f31", "Tree", "127.0.0.1:8"))
    for did in ("wled-dd0f30", "wled-dd0f31"):
        for _ in range(2):
            ctx.devices.refresh(did)
    xmas, tree = fake_wled("Xmas", 30, "aabbccdd0f30"), fake_wled("Tree", 30, "aabbccdd0f31")
    # Added again by hand: the old light is updated, not duplicated.
    r = client.post("/api/devices", json={"host": xmas.host})
    assert r.status_code == 200 and r.json["followed"] and r.json["device"]["id"] == "wled-dd0f30"
    # Or found by itself.
    ctx.discovery._started_at = time.time() - 120
    assert ctx.discovery.follow_moved([{"host": tree.host, "mac": "aabbccdd0f31"}]) == ["wled-dd0f31"]
    assert {d["id"]: d["host"] for d in ctx.store.get()["devices"]} == {"wled-dd0f30": xmas.host, "wled-dd0f31": tree.host}


def test_a_disabled_lights_address_is_not_taken(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    kitchen_board, xmas_board = fake_wled("Kitchen", 30, "aabbccdd0f32"), fake_wled("Xmas", 30, "aabbccdd0f33")
    kitchen = client.post("/api/devices", json={"host": kitchen_board.host}).json["device"]["id"]
    xmas = client.post("/api/devices", json={"host": xmas_board.host}).json["device"]["id"]
    client.put(f"/api/devices/{xmas}", json={"enabled": False})
    xmas_board.stop()
    moved_board = fake_wled("Kitchen", 30, "aabbccdd0f32")
    hosts = {kitchen: "127.0.0.1:9", xmas: moved_board.host}  # Kitchen's board got Xmas's old address
    ctx.store.update(lambda c: [d.update({"host": hosts[d["id"]]}) for d in c["devices"]])
    for _ in range(2):
        ctx.devices.refresh(kitchen)
    ctx.discovery._started_at = time.time() - 120
    assert ctx.discovery.follow_moved([{"host": moved_board.host, "mac": "aabbccdd0f32"}]) == [kitchen]
    # Re-enabling Xmas later never lets it drive Kitchen's strip.
    client.put(f"/api/devices/{xmas}", json={"enabled": True})
    ctx.devices.refresh(xmas)
    assert ctx.devices.wrong_device(xmas) and ctx.devices.summary(xmas)["wrong_owner"] == "Kitchen"


def test_reloading_effects_is_refused_while_another_controller_answers(two):
    m, a, _, _ = two
    m._get("a").cfg["mac"] = "001122334455"
    m.refresh("a")
    with pytest.raises(DeviceError):
        m.reload_catalog("a")


def test_a_wrong_address_keeps_the_light_recognisable(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Porch", 30, "aabbccdd0f34")
    did = client.post("/api/devices", json={"host": board.host}).json["device"]["id"]
    r = client.put(f"/api/devices/{did}", json={"host": "127.0.0.1:9"})  # nothing answers there
    assert r.status_code == 200 and "nothing answers" in r.json["warning"]
    assert ctx.store.get()["devices"][0]["mac"] == "aabbccdd0f34"
    for _ in range(2):
        ctx.devices.refresh(did)
    ctx.discovery._started_at = time.time() - 120
    assert ctx.discovery.follow_moved([{"host": board.host, "mac": "aabbccdd0f34"}]) == [did]


def test_swapped_addresses_can_be_fixed_by_hand(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    x, y = fake_wled("X", 30, "aabbccdd0f35"), fake_wled("Y", 30, "aabbccdd0f36")
    dx = client.post("/api/devices", json={"host": x.host}).json["device"]["id"]
    dy = client.post("/api/devices", json={"host": y.host}).json["device"]["id"]
    hosts = {dx: y.host, dy: x.host}
    ctx.store.update(lambda c: [d.update({"host": hosts[d["id"]]}) for d in c["devices"]])
    for did in (dx, dy):
        ctx.devices.refresh(did)
    # Typing an address where another light's controller answers is refused …
    z = fake_wled("Z", 30, "aabbccdd0f39")
    client.post("/api/devices", json={"host": z.host})
    z_moved = fake_wled("Z", 30, "aabbccdd0f39")
    r = client.put(f"/api/devices/{dx}", json={"host": z_moved.host})
    assert r.status_code == 409 and "your light 'Z'" in r.json["error"]
    # … and the right one is accepted, though the other light is configured there.
    r = client.put(f"/api/devices/{dx}", json={"host": x.host})
    assert r.status_code == 200 and r.json["device"]["online"], r.json
    r = client.put(f"/api/devices/{dy}", json={"host": y.host})
    assert r.status_code == 200 and r.json["device"]["online"], r.json


def test_a_light_that_never_answered_is_told_it_cannot_be_found(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    porch_board = fake_wled("Porch", 30, "aabbccdd0f37")
    client.post("/api/devices", json={"host": porch_board.host})
    garage = client.post("/api/devices", json={"host": "127.0.0.1:9", "name": "Garage", "skip_probe": True}).json["device"]["id"]
    ctx.store.update(lambda c: [d.update({"host": porch_board.host}) for d in c["devices"] if d["id"] == garage])
    ctx.devices.refresh(garage)
    assert ctx.devices.summary(garage)["wrong_owner"] == "Porch"
    r = client.put(f"/api/devices/{garage}", json={"name": "Garage", "replace": True})
    assert "never answered" in r.json["warning"]


def test_adding_a_disabled_lights_controller_enables_that_light(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Xmas", 30, "aabbccdd0f38")
    did = client.post("/api/devices", json={"host": board.host}).json["device"]["id"]
    client.put(f"/api/devices/{did}", json={"enabled": False})
    board.stop()
    back = fake_wled("Xmas", 30, "aabbccdd0f38")
    r = client.post("/api/devices", json={"host": back.host})
    assert r.status_code == 200 and "enabled again" in r.json["message"]
    assert ctx.store.get()["devices"][0]["enabled"] is True and len(ctx.store.get()["devices"]) == 1



def test_old_lights_keep_the_controllers_they_were_pointed_at(app, client, fake_wled):
    """In the release before MACs were remembered, a light's id named the board
    it was first added with, and its address could be pointed at another
    board (boards swapped between rooms).  Upgrading must leave both alone."""
    ctx = app.extensions["jimboled"]
    board_a, board_b = fake_wled("A", 30, "aabbccdd0a0a"), fake_wled("B", 30, "aabbccdd0b0b")
    _legacy(ctx, ("wled-dd0a0a", "Desk", board_b.host))
    _legacy(ctx, ("wled-dd0b0b", "Garage", board_a.host))
    for did in ("wled-dd0a0a", "wled-dd0b0b"):
        ctx.devices.refresh(did)
        assert ctx.devices.summary(did)["online"] and not ctx.devices.wrong_device(did)
    macs = {d["id"]: d["mac"] for d in ctx.store.get()["devices"]}
    assert macs == {"wled-dd0a0a": "aabbccdd0b0b", "wled-dd0b0b": "aabbccdd0a0a"}
    ctx.discovery._started_at = time.time() - 120
    assert ctx.discovery.follow_moved([{"host": board_a.host, "mac": "aabbccdd0a0a"},
                                       {"host": board_b.host, "mac": "aabbccdd0b0b"}]) == []


def test_an_old_lights_id_never_moves_it_onto_another_lights_address(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Board", 30, "aabbccdd0f41")
    other = client.post("/api/devices", json={"host": board.host, "name": "Tree"}).json["device"]["id"]
    # Tree remembers another board, so its address doesn't count as taken …
    ctx.store.update(lambda c: [d.update({"mac": "aabbccdd9999"}) for d in c["devices"] if d["id"] == other])
    ctx.devices.refresh(other)
    assert ctx.devices.wrong_device(other)
    _legacy(ctx, ("wled-dd0f41", "Christmas", "127.0.0.1:9"))  # … and Christmas's id names the board there
    for _ in range(2):
        ctx.devices.refresh("wled-dd0f41")
    ctx.discovery._started_at = time.time() - 120
    assert ctx.discovery.follow_moved([{"host": board.host, "mac": "aabbccdd0f41"}]) == []


def test_moves_within_a_wider_home_network_are_followed():
    from jimboled.wled.discovery import _same_subnet

    assert not _same_subnet("192.168.69.12", ["192.168.68.50"], local=[])
    assert _same_subnet("192.168.69.12", ["192.168.68.50"], local=["192.168.68.0/22"])
    assert not _same_subnet("10.9.9.9", ["192.168.68.50"], local=["192.168.68.0/22"])


def test_following_probes_every_candidate_in_turn(app):
    ctx = app.extensions["jimboled"]
    disc = ctx.discovery
    hosts = [f"10.0.0.{i}" for i in range(1, 13)]
    disc.listener.snapshot = lambda max_age_s=300: [{"host": h} for h in hosts]
    disc._lost_controllers = lambda: {"aabbccdd0f60": ("x", "10.0.0.50")}
    probed = []
    disc.probe = lambda host: probed.append(host)
    disc.follow_moved()
    disc.follow_moved()
    assert sorted(probed[8:12]) == sorted(set(hosts) - set(probed[:8]))


def test_adding_the_replacement_board_points_to_the_light(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Porch", 30, "aabbccdd0f50")
    did = client.post("/api/devices", json={"host": board.host}).json["device"]["id"]
    board.fake["info"]["mac"] = "aabbccdd5577"  # the new board took its address
    ctx.devices.refresh(did)
    r = client.post("/api/devices", json={"host": board.host})
    assert r.status_code == 409 and r.json["code"] == "replacement" and r.json["device_id"] == did
    assert len(ctx.store.get()["devices"]) == 1
    # "It replaces Porch's": the light takes it on.
    r = client.put(f"/api/devices/{did}", json={"replace": True})
    assert r.json["device"]["online"] and ctx.store.get()["devices"][0]["mac"] == "aabbccdd5577"


def test_nothing_answering_only_promises_what_following_can_do(app, client, fake_wled):
    board = fake_wled("Porch", 30, "aabbccdd0f51")
    did = client.post("/api/devices", json={"host": board.host}).json["device"]["id"]
    r = client.put(f"/api/devices/{did}", json={"host": "127.0.0.1:9"})
    assert "still finds the light" in r.json["warning"]
    r = client.put(f"/api/devices/{did}", json={"host": "wled-proch.invalid"})
    assert "Check the address" in r.json["warning"]



def test_a_disabled_light_is_sent_nothing(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Porch", 30, "aabbccdd0f52")
    did = client.post("/api/devices", json={"host": board.host}).json["device"]["id"]
    client.put(f"/api/devices/{did}", json={"enabled": False})
    board.fake["state"]["bri"] = 128
    assert client.post(f"/api/devices/{did}/state", json={"bri": 11}).status_code == 400
    assert board.fake["state"]["bri"] == 128


def test_a_replacement_behind_a_host_name_is_recognised(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Porch", 30, "aabbccdd0f53")
    port = board.host.rsplit(":", 1)[1]
    did = client.post("/api/devices", json={"host": f"localhost:{port}"}).json["device"]["id"]
    board.fake["info"]["mac"] = "aabbccdd5578"  # the replacement board, same name
    ctx.devices.refresh(did)
    r = client.post("/api/devices", json={"host": board.host})
    assert r.status_code == 409 and r.json["code"] == "replacement" and r.json["candidates"][0] == {"id": did, "name": "Porch", "how": "replace"}


def test_taking_over_checks_what_answers_now(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    board = fake_wled("Kitchen", 30, "aabbccdd0f54")
    did = client.post("/api/devices", json={"host": board.host}).json["device"]["id"]
    board.fake["info"]["mac"] = "112233445599"
    ctx.devices.refresh(did)
    board.fake["fail"] = True  # that controller has gone again
    r = client.put(f"/api/devices/{did}", json={"name": "Kitchen", "replace": True})
    assert r.status_code == 200 and "not switched" in r.json["warning"]
    assert ctx.store.get()["devices"][0]["mac"] == "aabbccdd0f54"


def test_a_replacement_board_at_a_new_address_can_take_the_lights_place(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    old = fake_wled("Kitchen", 30, "aabbccdd0f55")
    did = client.post("/api/devices", json={"host": old.host}).json["device"]["id"]
    old.stop()
    for _ in range(2):
        ctx.devices.refresh(did)
    new = fake_wled("WLED", 30, "aabbccdd7788")
    r = client.post("/api/devices", json={"host": new.host})
    assert r.status_code == 409 and r.json["candidates"] == [{"id": did, "name": "Kitchen", "how": "move"}]
    r = client.put(f"/api/devices/{did}", json={"host": new.host, "new_controller": True})
    assert r.status_code == 200 and r.json["device"]["online"]
    assert ctx.store.get()["devices"][0]["mac"] == "aabbccdd7788" and len(ctx.store.get()["devices"]) == 1
    # A separate light is still possible.
    another = fake_wled("Spare", 30, "aabbccdd7799")
    old2 = fake_wled("Gone", 30, "aabbccdd0f56")
    gone = client.post("/api/devices", json={"host": old2.host}).json["device"]["id"]
    old2.stop()
    for _ in range(2):
        ctx.devices.refresh(gone)
    assert client.post("/api/devices", json={"host": another.host, "separate": True}).status_code == 201
