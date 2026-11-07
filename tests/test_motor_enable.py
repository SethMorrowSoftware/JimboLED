"""A motor (hold-to-run switch) only moves from the dashboard after Enable."""
import time

import pytest


def _act(client, sid, action, **extra):
    return client.post(f"/api/gpio/switches/{sid}/action", json={"action": action, **extra})


@pytest.fixture
def bed(client):
    r = client.post("/api/gpio/switches", json={"switches": [
        {"name": "Bed up", "pin": 17, "mode": "momentary", "interlock_group": "bed"},
        {"name": "Bed down", "pin": 27, "mode": "momentary", "interlock_group": "bed"},
    ]})
    up, down = r.json["switches"]
    return up["id"], down["id"]


def _snap(client, sid):
    return next(s for s in client.get("/api/gpio").json["switches"] if s["id"] == sid)


def test_hold_is_refused_until_enable(client, bed):
    up, _ = bed
    r = _act(client, up, "press")
    assert r.status_code == 400 and "Enable" in r.json["error"]
    assert _snap(client, up)["on"] is False
    assert _snap(client, up)["require_enable"] is True and _snap(client, up)["enabled_for"] == 0


def test_enable_unlocks_both_directions_of_the_motor(client, bed):
    up, down = bed
    r = _act(client, up, "enable")
    assert r.status_code == 200 and r.json["switch"]["enabled_for"] > 10
    assert _snap(client, down)["enabled_for"] > 10
    tok = _act(client, down, "press").json["switch"]["token"]
    assert _snap(client, down)["on"]
    _act(client, down, "release", token=tok)
    assert _act(client, up, "press").json["switch"]["on"]


def test_disable_locks_it_again(client, bed):
    up, down = bed
    _act(client, up, "enable")
    _act(client, down, "disable")
    assert _act(client, up, "press").status_code == 400


def test_enable_runs_out(client, bed, app):
    up, _ = bed
    gpio = app.extensions["jimboled"].gpio
    gpio._enable_window = 0.2
    _act(client, up, "enable")
    time.sleep(0.3)
    assert _act(client, up, "press").status_code == 400


def test_using_the_motor_keeps_it_enabled(client, bed, app):
    up, down = bed
    gpio = app.extensions["jimboled"].gpio
    gpio._enable_window = 0.5
    _act(client, up, "enable")
    tok = _act(client, up, "press").json["switch"]["token"]
    for _ in range(4):  # held for longer than the window, with heartbeats
        time.sleep(0.2)
        _act(client, up, "heartbeat", token=tok)
    _act(client, up, "release", token=tok)
    assert _act(client, down, "press").json["switch"]["on"]


def test_all_off_and_emergency_stop_lock_motors(client, bed):
    up, _ = bed
    _act(client, up, "enable")
    client.post("/api/gpio/all-off")
    assert _act(client, up, "press").status_code == 400
    _act(client, up, "enable")
    client.post("/api/estop/engage", json={})
    client.post("/api/estop/reset", json={})
    assert _act(client, up, "press").status_code == 400


def test_enable_can_be_switched_off_per_motor(client, bed):
    up, down = bed
    client.put(f"/api/gpio/switches/{up}", json={"require_enable": False})
    assert _act(client, up, "press").json["switch"]["on"]
    assert _act(client, down, "press").status_code == 400  # Down still asks for it


def test_enable_only_applies_to_motors(client):
    sid = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 22}).json["switch"]["id"]
    assert _act(client, sid, "enable").status_code == 400
    assert _snap(client, sid)["require_enable"] is False


def test_enable_window_setting(client):
    assert client.put("/api/settings", json={"gpio": {"enable_window_s": 30}}).status_code == 200
    assert client.get("/api/gpio").json["enable_window_s"] == 30
    assert client.put("/api/settings", json={"gpio": {"enable_window_s": 1}}).status_code == 400
