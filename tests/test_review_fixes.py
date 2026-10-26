"""Regression tests for problems found in review: safety, tokens, presets,
fan-out reporting, scheduling edge cases and config robustness."""
import io
import json
import time
from datetime import date, datetime, timedelta

import pytest

from jimboled import scheduler as S
from jimboled.config import ConfigStore
from jimboled.gpio.common import GPIOError
from jimboled.wled.client import fit_name, is_idempotent
from jimboled.wled.manager import DeviceError, DeviceManager, sanitise_state


# ------------------------------------------------------------- relay safety
def test_hold_to_run_switch_cannot_be_pulsed_unattended(client, app):
    ctx = app.extensions["jimboled"]
    up = client.post("/api/gpio/switches", json={"name": "Bed up", "pin": 17, "mode": "momentary"}).json["switch"]["id"]
    r = client.post(f"/api/gpio/switches/{up}/action", json={"action": "pulse", "duration_ms": 60000})
    assert r.status_code == 400 and "hold-to-run" in r.json["error"]
    with pytest.raises(GPIOError):
        ctx.gpio.pulse(up)
    scene = client.post("/api/scenes", json={"name": "Up", "actions": [{"type": "switch", "ref": up, "action": "pulse"}]}).json["scene"]
    run = client.post(f"/api/scenes/{scene['id']}/run").json
    assert run["failed"] and "hold-to-run" in run["failed"][0]["error"]
    assert not any(s["on"] for s in ctx.gpio.snapshot()["switches"])
    # The short "which relay is this?" test while setting up still works.
    assert client.post("/api/gpio/test", json={"pin": 17, "duration_ms": 50}).status_code == 200


# ------------------------------------------------------------------ tokens
def test_tokens_operate_but_cannot_reconfigure(client, app):
    token = client.post("/api/tokens", json={"name": "HA"}).json["token"]
    sw = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 22}).json["switch"]["id"]
    up = client.post("/api/gpio/switches", json={"name": "Bed up", "pin": 17, "mode": "momentary"}).json["switch"]["id"]
    bare = app.test_client()
    auth = {"Authorization": f"Bearer {token}"}
    allowed = [
        ("/api/gpio/switches/%s/action" % sw, {"action": "on"}),
        ("/api/gpio/switches/%s/action" % sw, {"action": "off"}),
        ("/api/timers", {"minutes": 5}),
        ("/api/estop/engage", {}),
    ]
    for path, payload in allowed:
        assert bare.post(path, json=payload, headers=auth).status_code in (200, 201), path
    denied = [
        ("post", "/api/estop/reset", {}),
        ("put", f"/api/gpio/switches/{up}", {"mode": "toggle"}),
        ("post", f"/api/gpio/switches/{up}/action", {"action": "press"}),
        ("post", f"/api/gpio/switches/{up}/action", {"action": "heartbeat", "token": "x"}),
        ("post", "/api/schedules", {"when": {"type": "time", "time": "07:00"}, "action": {"type": "all_off"}}),
        ("put", "/api/location", {"timezone": "Europe/London"}),
        ("post", "/api/devices", {"host": "10.0.0.9", "skip_probe": True}),
        ("delete", f"/api/gpio/switches/{sw}", None),
        ("get", "/api/backup", None),
    ]
    for method, path, payload in denied:
        kwargs = {"headers": auth}
        if payload is not None:
            kwargs["json"] = payload
        assert getattr(bare, method)(path, **kwargs).status_code == 403, (method, path)


# ------------------------------------------------------- backup / restore
def test_restore_keeps_the_current_password(client):
    client.post("/api/settings/password", json={"password": "oldpass"})
    backup = json.loads(client.get("/api/backup").data)
    assert "password_hash" not in backup["server"]
    backup["server"]["password_hash"] = "pbkdf2:sha256:1$x$y"  # an old one smuggled in
    client.post("/api/settings/password", json={"current": "oldpass", "password": "newpass"})
    r = client.post("/api/restore", data={"file": (io.BytesIO(json.dumps(backup).encode()), "b.json")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    fresh = client.application.test_client()
    fresh.environ_base["HTTP_X_REQUESTED_WITH"] = "JimboLED"
    assert fresh.post("/api/login", json={"password": "newpass"}).status_code == 200
    assert client.application.test_client().post("/api/login", json={"password": "oldpass"},
                                                   headers={"X-Requested-With": "JimboLED"}).status_code == 401


# -------------------------------------------------------------- scheduling
def test_jitter_that_crosses_midnight_still_runs(tmp_path):
    store = ConfigStore(tmp_path)
    calls = []
    sch = S.Scheduler(store, lambda a: calls.append(a) or {"message": "ok"})

    class Sync:
        def submit(self, fn, *a):
            fn(*a)

        def shutdown(self, wait=False):
            pass

    sch._pool = Sync()
    base = {"id": "mid", "name": "Midnight", "enabled": True, "when": {"type": "time", "time": "00:00"},
            "days": list(range(7)), "random": 15, "action": {"type": "all_off"}, "delete_after": False, "updated_at": 0}
    store.update(lambda c: c.__setitem__("schedules", [base]))
    start = date(2026, 1, 1)
    early = 0
    for n in range(30):
        day = start + timedelta(days=n)
        due = S.occurrence(base, day, {})
        if due.date() < day:
            early += 1
        # Tick every minute from 23:40 the evening before to 00:20.
        t = datetime(day.year, day.month, day.day).astimezone() - timedelta(minutes=20)
        while t <= datetime(day.year, day.month, day.day).astimezone() + timedelta(minutes=20):
            sch.tick(t)
            t += timedelta(minutes=1)
    assert early > 0  # the case under test happened
    assert len(calls) == 30  # once a night, none missed


def test_running_a_timer_by_hand_uses_it_up(client, app):
    ctx = app.extensions["jimboled"]
    t = client.post("/api/timers", json={"minutes": 30}).json["schedule"]
    assert client.post(f"/api/schedules/{t['id']}/run").json["ok"]
    assert not any(s["id"] == t["id"] for s in ctx.store.get()["schedules"])


# ------------------------------------------------------------------ config
def test_lists_that_are_not_lists_are_repaired(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"groups": None, "schedules": "nope",
                                                      "devices": [1, {"id": "a", "name": "A", "host": "x"}],
                                                      "dashboard": {"tiles": 5}}))
    cfg = ConfigStore(tmp_path).get()
    assert cfg["groups"] == [] and cfg["schedules"] == [] and cfg["dashboard"]["tiles"] == []
    assert [d["id"] for d in cfg["devices"]] == ["a"]


# -------------------------------------------------------------------- WLED
def test_strict_booleans():
    assert sanitise_state({"on": "false"})["on"] is False
    assert sanitise_state({"on": "off"})["on"] is False
    assert sanitise_state({"on": 1})["on"] is True
    assert sanitise_state({"on": "t"})["on"] == "t"
    with pytest.raises(DeviceError):
        sanitise_state({"on": "maybe"})


def test_names_are_cut_by_bytes_and_retries_are_safe():
    assert fit_name("Weihnachtsbaum Wohnzimmer 🎄🎄") == "Weihnachtsbaum Wohnzimmer 🎄"
    assert len(fit_name("é" * 40).encode()) <= 32
    assert is_idempotent({"on": True, "seg": [{"id": 0, "col": [[1, 2, 3]]}]})
    assert not is_idempotent({"on": "t"})
    assert not is_idempotent({"np": True})
    assert not is_idempotent({"seg": [{"fx": "~"}]})


@pytest.fixture
def dm(fake_wled, data_dir):
    a = fake_wled("A", 60, "aabbccdd0c01", segments=2)
    store = ConfigStore(data_dir)
    store.update(lambda c: c.update({"devices": [{"id": "a", "name": "A", "host": a.host, "enabled": True}],
                                     "wled": {"poll_interval_s": 60, "offline_poll_interval_s": 60, "request_timeout_s": 2}}))
    m = DeviceManager(store)
    m.start()
    m.refresh("a")
    yield m, a
    m.stop()


def test_same_preset_again_after_off_comes_back_on(dm):
    m, a = dm
    m.save_preset("a", 5, "Movie", state={"on": True, "bri": 60, "seg": [{"id": 0, "fx": 2}]})
    m.set_state("a", {"ps": 5})
    m.set_state("a", {"on": False})
    assert a.fake["state"]["ps"] == 5 and a.fake["state"]["on"] is False
    m.set_state("a", {"ps": 5})
    assert a.fake["state"]["on"] is True and a.fake["state"]["bri"] == 60


def test_toggle_is_sent_as_a_plain_on_or_off(dm):
    m, a = dm
    m.set_state("a", {"on": True})
    m.set_state("a", {"on": "t"})
    assert a.fake["log"][-1]["on"] is False


def test_colour_without_segment_id_reaches_every_segment(dm):
    m, a = dm
    m.set_state("a", {"seg": {"col": [[0, 255, 0]]}})
    assert all(s["col"][0][:3] == [0, 255, 0] for s in a.fake["state"]["seg"])


def test_rename_keeps_the_preset(dm):
    m, a = dm
    a.fake["presets"]["8"] = {"n": "Party", "playlist": {"ps": [1, 2], "dur": [30, 30], "transition": [7, 7]}, "on": True}
    m.refresh("a")
    m._load_catalog(m._get("a"), force=True)
    m.rename_preset("a", 8, "Party time")
    saved = a.fake["presets"]["8"]
    assert saved["n"] == "Party time" and saved["playlist"]["ps"] == [1, 2]


def test_rename_through_the_api(client, fake_wled):
    srv = fake_wled("R", 30, "aabbccdd0c02")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    srv.fake["presets"]["3"] = {"n": "Calm", "on": True, "bri": 10, "seg": [{"id": 0, "fx": 2}]}
    client.post(f"/api/devices/{did}/catalog")
    client.post(f"/api/devices/{did}/state", json={"on": True, "bri": 200, "seg": [{"id": 0, "fx": 7}]})
    r = client.post(f"/api/devices/{did}/presets", json={"slot": 3, "name": "Calm blue", "rename": True})
    assert r.status_code == 200
    assert srv.fake["presets"]["3"]["n"] == "Calm blue" and srv.fake["presets"]["3"]["bri"] == 10


def test_busy_catalogue_is_retried_not_cached_empty(dm):
    m, a = dm
    rec = m._get("a")
    rec.catalog_key = ""
    rec.effects = []
    a.fake["fail_paths"] = {"/json/fxdata"}
    m.refresh("a")
    assert m.summary("a")["online"] is True  # busy is not offline
    assert rec.catalog_key == ""            # and not remembered as loaded
    a.fake["fail_paths"] = set()
    m.refresh("a")
    assert rec.catalog_key and rec.fxdata


def test_missing_preset_number_is_reported_for_all_lights(dm):
    m, a = dm
    res = m.set_state_all({"ps": 77})
    assert "no preset #77" in res["a"]


def test_identify_blinks_and_puts_it_back(dm):
    m, a = dm
    m.set_state("a", {"on": True, "bri": 40, "seg": [{"id": 0, "fx": 2}, {"id": 1, "fx": 2}]})
    m.identify("a", seconds=1.0)
    assert a.fake["state"]["bri"] == 255 and a.fake["state"]["seg"][0]["fx"] == 1
    time.sleep(1.4)
    assert a.fake["state"]["bri"] == 40 and a.fake["state"]["seg"][0]["fx"] == 2


# --------------------------------------------------------- lights API extras
def test_undo_puts_lights_back(client, fake_wled):
    srv = fake_wled("U", 30, "aabbccdd0c03")
    client.post("/api/devices", json={"host": srv.host})
    client.post("/api/lights/apply", json={"color": "#0000ff", "brightness": 50})
    r = client.post("/api/lights/apply", json={"color": "#ff0000", "brightness": 100}).json
    assert srv.fake["state"]["seg"][0]["col"][0][:3] == [255, 0, 0]
    u = client.post(f"/api/lights/undo/{r['undo']}").json
    assert u["ok"] and srv.fake["state"]["seg"][0]["col"][0][:3] == [0, 0, 255]
    assert client.post(f"/api/lights/undo/{r['undo']}").status_code == 404


def test_deleting_a_controller_cleans_scenes(client, fake_wled):
    a = fake_wled("A", 30, "aabbccdd0c04")
    b = fake_wled("B", 30, "aabbccdd0c05")
    da = client.post("/api/devices", json={"host": a.host}).json["device"]["id"]
    db = client.post("/api/devices", json={"host": b.host}).json["device"]["id"]
    cap = client.post("/api/lights/capture", json={}).json
    sc = client.post("/api/scenes", json={"name": "Both", "actions": cap["actions"] + [
        {"type": "lights", "target": {"devices": [da, db]}, "intent": {"on": True}}]}).json["scene"]
    client.delete(f"/api/devices/{da}")
    scene = next(s for s in client.get("/api/scenes").json["scenes"] if s["id"] == sc["id"])
    assert [a.get("ref") for a in scene["actions"] if a["type"] == "device"] == [db]
    assert scene["actions"][-1]["target"]["devices"] == [db]


def test_bad_scene_state_is_refused_when_saved(client):
    r = client.post("/api/scenes", json={"name": "Bad", "actions": [{"type": "all", "state": {"bri": "high"}}]})
    assert r.status_code == 400
    r = client.post("/api/scenes", json={"name": "Hex", "actions": [{"type": "all", "state": {"seg": {"col": ["#ff0000"]}}}]})
    assert r.status_code == 201 and r.json["scene"]["actions"][0]["state"]["seg"]["col"] == [[255, 0, 0]]


def test_all_off_says_which_lights_it_missed(client, fake_wled):
    srv = fake_wled("Here", 30, "aabbccdd0c06")
    client.post("/api/devices", json={"host": srv.host})
    client.post("/api/devices", json={"host": "127.0.0.1:1", "name": "Gone", "skip_probe": True})
    r = client.post("/api/system/shutdown-all").json
    assert r["ok"] is False and "Gone" in r["message"] and srv.fake["state"]["on"] is False


def test_schedules_listing_reports_clock_state(client):
    assert "clock_synced" in client.get("/api/schedules").json


def test_controller_that_moved_is_followed(app, client, fake_wled):
    ctx = app.extensions["jimboled"]
    old = fake_wled("Porch", 30, "aabbccdd0d01")
    did = client.post("/api/devices", json={"host": old.host}).json["device"]["id"]
    ctx.devices.refresh(did)
    old.stop()
    rec = ctx.devices._get(did)
    for _ in range(2):
        ctx.devices.refresh(did)
    rec.last_seen = time.time() - 120
    new = fake_wled("Porch", 30, "aabbccdd0d01")  # same MAC, new address
    moved = ctx.discovery.follow_moved([{"host": new.host, "mac": "aabbccdd0d01"}])
    assert moved == [did]
    assert ctx.store.get()["devices"][0]["host"] == new.host
    # Somebody else's controller (another MAC) is never adopted.
    assert ctx.discovery.follow_moved([{"host": "10.9.9.9", "mac": "ffffffffffff"}]) == []


def test_try_a_scene_before_saving(client, fake_wled):
    srv = fake_wled("T", 30, "aabbccdd0d02")
    client.post("/api/devices", json={"host": srv.host})
    r = client.post("/api/scenes/test", json={"actions": [{"type": "lights", "target": "all", "intent": {"color": "#00ff00"}}]})
    assert r.status_code == 200 and r.json["failed"] == []
    assert srv.fake["state"]["seg"][0]["col"][0][:3] == [0, 255, 0]
    assert client.get("/api/scenes").json["scenes"] == []
    assert client.post("/api/scenes/test", json={"actions": []}).status_code == 400
