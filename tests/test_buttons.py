"""Physical action buttons on GPIO inputs."""
import time

import pytest

from jimboled.gpio.buttons import HOLD_S, ButtonInputs


def _pin(app, button_id):
    return app.extensions["jimboled"].gpio.buttons._devices[button_id].pin


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_tap_and_hold_run_their_actions(app, client):
    ctx = app.extensions["jimboled"]
    lamp = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 22}).json["switch"]["id"]
    r = client.post("/api/gpio/buttons", json={"name": "Bedside", "pin": 5, "pull": "up",
                                               "press": {"type": "switch", "ref": lamp, "action": "toggle"},
                                               "hold": {"type": "all_off"}})
    assert r.status_code == 201, r.json
    bid = r.json["button"]["id"]
    pin = _pin(app, bid)
    lamp_on = lambda: next(s for s in ctx.gpio.snapshot()["switches"] if s["id"] == lamp)["on"]  # noqa: E731
    # Tap: pressed (pin pulled low against the pull-up) then released.
    pin.drive_low(); time.sleep(0.25); pin.drive_high()
    assert _wait(lamp_on)
    # Hold: the lamp goes off after about a second, before the release.
    pin.drive_low()
    assert _wait(lambda: not lamp_on(), timeout=HOLD_S + 2)
    pin.drive_high(); time.sleep(0.3)
    assert not lamp_on()  # releasing after a hold does not also "tap"
    snap = client.get("/api/gpio/buttons").json["buttons"][0]
    assert snap["presses"] == 2 and snap["last_event"] == "hold"
    events = [e["action"] for e in client.get("/api/gpio/events").json["events"]]
    assert "button-press" in events and "button-hold" in events


def test_button_validation_and_pin_clashes(client):
    sw = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 22}).json["switch"]["id"]
    up = client.post("/api/gpio/switches", json={"name": "Bed up", "pin": 17, "mode": "momentary"}).json["switch"]["id"]
    assert client.post("/api/gpio/buttons", json={"name": "B", "pin": 22, "press": {"type": "all_off"}}).status_code == 400
    assert client.post("/api/gpio/buttons", json={"name": "B", "pin": 6}).status_code == 400  # nothing to do
    assert client.post("/api/gpio/buttons", json={"name": "B", "pin": 6, "press": {"type": "nope"}}).status_code == 400
    r = client.post("/api/gpio/buttons", json={"name": "B", "pin": 6, "press": {"type": "lights", "intent": {"on": "toggle"}}})
    assert r.status_code == 201
    bid = r.json["button"]["id"]
    # Relays, stops and other buttons can't take its pin any more.
    assert client.post("/api/gpio/switches", json={"name": "X", "pin": 6}).status_code == 400
    assert client.post("/api/estop/inputs", json={"name": "Stop", "pin": 6}).status_code == 400
    assert client.post("/api/gpio/buttons", json={"name": "C", "pin": 6, "press": {"type": "all_off"}}).status_code == 400
    assert client.put(f"/api/gpio/buttons/{bid}", json={"name": "Renamed"}).status_code == 200
    pins = {p["bcm"]: p["in_use_by"] for p in client.get("/api/gpio/pins").json["pins"]}
    assert "Renamed" in pins[6]
    assert client.delete(f"/api/gpio/buttons/{bid}").status_code == 200
    assert client.delete(f"/api/gpio/buttons/{bid}").status_code == 404


def test_a_button_cannot_run_a_hold_to_run_motor(app, client):
    ctx = app.extensions["jimboled"]
    up = client.post("/api/gpio/switches", json={"name": "Bed up", "pin": 17, "mode": "momentary"}).json["switch"]["id"]
    bid = client.post("/api/gpio/buttons", json={"name": "B", "pin": 5, "press": {"type": "switch", "ref": up, "action": "pulse"}}).json["button"]["id"]
    assert client.post(f"/api/gpio/buttons/{bid}/try", json={"kind": "press"}).status_code == 200
    time.sleep(0.4)
    assert not any(s["on"] for s in ctx.gpio.snapshot()["switches"])
    assert client.post(f"/api/gpio/buttons/{bid}/try", json={"kind": "hold"}).status_code == 400


def test_poll_logic_without_hardware():
    class Dev:
        value = 0

    b = ButtonInputs()
    from jimboled.gpio.buttons import ButtonInput

    item = ButtonInput.from_dict({"id": "b", "name": "B", "pin": 5, "press": {"type": "all_off"}}, lambda a: a)
    b.buttons = [item]
    b._devices = {"b": Dev()}
    assert b.poll(0.0) == []
    Dev.value = 1
    assert b.poll(0.1) == []
    assert b.poll(3.0) == []          # long press, no hold action: still nothing yet
    Dev.value = 0
    assert [k for _, k in b.poll(3.1)] == ["press"]  # ...it counts as a tap on release
