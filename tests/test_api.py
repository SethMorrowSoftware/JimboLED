import json
import time


def test_state_and_csrf(client, fake_wled):
    r = client.get("/api/state")
    assert r.status_code == 200 and "devices" in r.json
    plain = client.application.test_client()
    assert plain.post("/api/devices", json={"host": "x"}).status_code == 403
    assert client.get("/api/nope").status_code == 404
    assert client.get("/healthz").json["ok"]
    assert client.get("/").status_code == 200
    assert b"JimboLED" in client.get("/").data


def test_device_crud_and_control(client, fake_wled):
    srv = fake_wled("Bed", 60, "aabbccdd0101")
    r = client.post("/api/devices/probe", json={"host": srv.host})
    assert r.status_code == 200 and r.json["name"] == "Bed"
    r = client.post("/api/devices", json={"host": srv.host})
    assert r.status_code == 201
    did = r.json["device"]["id"]
    assert did == "wled-dd0101"
    assert client.post("/api/devices", json={"host": srv.host}).status_code == 409
    assert client.post("/api/devices", json={"host": "10.255.255.1"}).status_code == 502
    r = client.post("/api/devices", json={"host": "10.255.255.1", "skip_probe": True})
    assert r.status_code == 201
    off = r.json["device"]["id"]
    full = client.get(f"/api/devices/{did}").json["device"]
    assert full["effects"] and full["presets"]
    r = client.post(f"/api/devices/{did}/state", json={"on": True, "bri": 77, "seg": {"fx": 2}})
    assert r.json["device"]["state"]["bri"] == 77 and r.json["device"]["effect_name"] == "Breathe"
    r = client.post(f"/api/devices/{did}/presets", json={"name": "Nice"})
    assert r.status_code == 200 and r.json["slot"] == 4
    assert client.delete(f"/api/devices/{did}/presets/4").status_code == 200
    assert client.post(f"/api/devices/{did}/presets", json={"name": ""}).status_code == 400
    r = client.put(f"/api/devices/{did}", json={"name": "Bedroom", "icon": "bed"})
    assert r.json["device"]["name"] == "Bedroom"
    tiles = client.get("/api/dashboard").json["dashboard"]["tiles"]
    assert [t["ref"] for t in tiles] == [did, off]
    assert tiles[0]["title"] == "Bedroom"
    r = client.post("/api/devices/all/state", json={"on": False})
    assert r.json["failed"] == {}
    assert client.delete(f"/api/devices/{off}").status_code == 200
    assert client.delete(f"/api/devices/{off}").status_code == 404
    assert len(client.get("/api/dashboard").json["dashboard"]["tiles"]) == 1


def test_gpio_endpoints(client):
    r = client.post("/api/gpio/switches", json={"name": "Up", "pin": 17, "mode": "momentary", "interlock_group": "bed"})
    assert r.status_code == 201
    up = r.json["switch"]["id"]
    r = client.post("/api/gpio/switches", json={"name": "Dup", "pin": 17})
    assert r.status_code == 400 and "used by" in r.json["error"]
    r = client.post("/api/gpio/switches", json={"name": "Down", "pin": 27, "mode": "momentary", "interlock_group": "bed", "active_high": False})
    down = r.json["switch"]["id"]
    r = client.post(f"/api/gpio/switches/{up}/action", json={"action": "press"})
    tok = r.json["switch"]["token"]
    assert r.json["switch"]["on"]
    assert client.post(f"/api/gpio/switches/{up}/action", json={"action": "heartbeat", "token": tok}).json["switch"]["held"]
    assert client.post(f"/api/gpio/switches/{down}/action", json={"action": "press"}).json["switch"]["on"]
    ons = {s["id"]: s["on"] for s in client.get("/api/gpio").json["switches"]}
    assert ons == {up: False, down: True}
    assert client.post(f"/api/gpio/switches/{up}/action", json={"action": "on"}).status_code == 400
    assert client.post(f"/api/gpio/switches/{up}/action", json={"action": "bogus"}).status_code == 400
    assert client.post("/api/gpio/all-off").status_code == 200
    assert not any(s["on"] for s in client.get("/api/gpio").json["switches"])
    assert any(p["in_use_by"] == "Up" for p in client.get("/api/gpio/pins").json["pins"])
    assert client.post("/api/gpio/test", json={"pin": 22, "duration_ms": 20}).status_code == 200
    assert client.post("/api/gpio/test", json={"pin": 0}).status_code == 400
    r = client.put(f"/api/gpio/switches/{up}", json={"name": "Head up", "max_on_seconds": 30})
    assert r.status_code == 200
    assert client.get("/api/gpio/switches").json["switches"][0]["name"] == "Head up"
    assert client.get("/api/gpio/events").status_code == 200
    assert client.delete(f"/api/gpio/switches/{down}").status_code == 200
    assert [t["type"] for t in client.get("/api/dashboard").json["dashboard"]["tiles"]] == ["switch"]


def test_dashboard_scenes_settings_backup(client, fake_wled):
    srv = fake_wled("Bed", 60, "aabbccdd0202")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    sw = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 22}).json["switch"]["id"]
    r = client.post("/api/scenes", json={"name": "Night", "actions": [{"type": "all", "state": {"on": False}}, {"type": "switch", "ref": sw, "action": "off"}, {"type": "delay", "ms": 10}]})
    assert r.status_code == 201
    sc = r.json["scene"]["id"]
    assert client.post("/api/scenes", json={"name": "", "actions": []}).status_code == 400
    r = client.post(f"/api/scenes/{sc}/run")
    assert r.status_code == 200 and not r.json["failed"]
    assert client.get(f"/api/devices/{did}").json["device"]["state"]["on"] is False
    tiles = client.get("/api/dashboard").json["dashboard"]["tiles"]
    assert [t["type"] for t in tiles] == ["device", "switch", "scene"]
    ids = [t["id"] for t in tiles]
    r = client.post("/api/dashboard/order", json={"ids": list(reversed(ids))})
    assert [t["id"] for t in r.json["dashboard"]["tiles"]] == list(reversed(ids))
    r = client.put(f"/api/dashboard/tiles/{ids[0]}", json={"size": "xl", "hidden": True, "name": "Big"})
    tile = next(t for t in r.json["dashboard"]["tiles"] if t["id"] == ids[0])
    assert tile["size"] == "xl" and tile["hidden"] and tile["name"] == "Big"
    r = client.post("/api/dashboard/tiles", json={"type": "heading", "name": "Bedroom"})
    assert r.status_code == 201
    assert client.post("/api/dashboard/tiles", json={"type": "device", "ref": did}).status_code == 400
    assert client.put("/api/dashboard", json={"theme": "oled", "accent": "#ff8800", "title": "Jim"}).json["dashboard"]["theme"] == "oled"
    assert client.put("/api/dashboard", json={"accent": "red"}).status_code == 400
    # settings / password
    s = client.get("/api/settings").json
    assert s["server"]["password_set"] is False and "secret_key" not in s["server"]
    assert client.post("/api/settings/password", json={"password": "abc"}).status_code == 400
    assert client.post("/api/settings/password", json={"password": "hunter2"}).json["password_set"]
    other = client.application.test_client()
    other.environ_base["HTTP_X_REQUESTED_WITH"] = "JimboLED"
    assert other.get("/api/state").status_code == 401
    assert other.get("/").status_code == 302
    assert other.post("/api/login", json={"password": "nope"}).status_code == 401
    assert other.post("/api/login", json={"password": "hunter2"}).status_code == 200
    assert other.get("/api/state").status_code == 200
    assert client.post("/api/settings/password", json={"current": "wrong", "password": ""}).status_code == 403
    assert client.post("/api/settings/password", json={"current": "hunter2", "password": ""}).json["password_set"] is False
    # backup / restore
    backup = client.get("/api/backup").data
    assert b"secret_key" not in backup
    client.delete(f"/api/devices/{did}")
    assert len(client.get("/api/devices").json["devices"]) == 0
    r = client.post("/api/restore", data=backup, content_type="application/json")
    assert r.status_code == 200
    assert len(client.get("/api/devices").json["devices"]) == 1
    assert client.post("/api/restore", data=b"garbage", content_type="application/json").status_code == 400
    assert client.get("/api/backups").json["backups"]
    name = client.get("/api/backups").json["backups"][0]["name"]
    assert client.post(f"/api/backups/{name}/restore").status_code == 200
    assert client.post("/api/backups/../etc/restore").status_code in (404, 400)
    # system
    r = client.get("/api/system")
    assert r.status_code == 200 and r.json["gpio_simulated"] is True
    assert client.get("/api/system/logs").status_code == 200
    assert client.post("/api/system/restart").status_code == 501
    assert client.post("/api/system/shutdown-all").status_code == 200


def test_discovery_endpoints(client):
    r = client.post("/api/discover", json={"methods": ["nodes"]})
    assert r.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        s = client.get("/api/discover").json
        if not s["running"]:
            break
        time.sleep(0.2)
    assert s["running"] is False and isinstance(s["found"], list) and "passive" in s


def test_restore_rejects_broken_devices_and_keeps_service_alive(client):
    bad = json.dumps({"devices": [{"id": "x"}, {"id": "ok", "host": "10.1.1.1", "name": "OK"}], "dashboard": {}, "gpio": {"switches": [], "backend": "weird"}, "server": {"port": "nope"}})
    r = client.post("/api/restore", data=bad, content_type="application/json")
    assert r.status_code == 400  # bad port is reported, not swallowed
    good = json.dumps({"devices": [{"id": "x"}, {"id": "ok", "host": "10.1.1.1", "name": "OK"}], "dashboard": {}, "gpio": {"switches": [], "backend": "weird"}})
    r = client.post("/api/restore", data=good, content_type="application/json")
    assert r.status_code == 200
    devs = client.get("/api/devices").json["devices"]
    assert [d["id"] for d in devs] == ["ok"]
    assert client.get("/api/settings").json["gpio"]["backend"] == "auto"
    assert client.get("/api/state").status_code == 200


def test_numeric_settings_validation(client):
    assert client.put("/api/settings", json={"wled": {"poll_interval_s": "abc"}}).status_code == 400
    assert client.put("/api/settings", json={"server": {"port": 99999}}).status_code == 400
    assert client.put("/api/settings", json={"server": {"allowed_hosts": "Jimbo.example.com other"}}).json["server"]["allowed_hosts"] == ["jimbo.example.com", "other"]


def test_password_change_invalidates_old_sessions_and_throttles(client):
    assert client.post("/api/settings/password", json={"password": "first1"}).status_code == 200
    other = client.application.test_client()
    other.environ_base["HTTP_X_REQUESTED_WITH"] = "JimboLED"
    assert other.post("/api/login", json={"password": "first1"}).status_code == 200
    assert other.get("/api/state").status_code == 200
    # changing the password logs the other client out
    assert client.post("/api/settings/password", json={"current": "first1", "password": "second2"}).status_code == 200
    assert other.get("/api/state").status_code == 401
    # brute force throttle
    for _ in range(5):
        assert other.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert other.post("/api/login", json={"password": "second2"}).status_code == 429


def test_host_header_guard(app):
    app.config["CHECK_HOST"] = True
    c = app.test_client()
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "JimboLED"
    for host in ("192.168.1.5", "192.168.1.5:8080", "jimboled", "jimboled.local", "pi.lan", "[fe80::1]", "localhost"):
        assert c.get("/healthz", headers={"Host": host}).status_code == 200, host
    assert c.get("/api/state", headers={"Host": "evil.example.com"}).status_code == 421
    assert c.get("/", headers={"Host": "evil.example.com"}).status_code == 421
    assert c.put("/api/settings", json={"server": {"allowed_hosts": ["evil.example.com"]}}, headers={"Host": "192.168.1.5"}).status_code == 200
    assert c.get("/api/state", headers={"Host": "evil.example.com"}).status_code == 200


def test_save_preset_state_is_sanitised(client, fake_wled):
    srv = fake_wled("Bed", 60, "aabbccdd0303")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    r = client.post(f"/api/devices/{did}/presets", json={"name": "Loop", "state": {"on": True, "playlist": {"ps": [1, 2], "dur": 100}, "rb": True, "pdel": 1}})
    assert r.status_code == 200
    sent = [p for p in srv.fake["log"] if "psave" in p][-1]
    assert "rb" not in sent and "pdel" not in sent and sent["o"] is True and "playlist" in sent
    assert client.post(f"/api/devices/{did}/presets", json={"name": "Nothing", "state": {"rb": True}}).status_code == 400


def test_open_redirect_blocked(client):
    client.post("/api/settings/password", json={"password": "abcd1"})
    r = client.post("/login?next=/\\evil.com", data={"password": "abcd1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


# ------------------------------------------------------------ emergency stop
def _add_switch(client, name="Bed up", pin=17, **kw):
    payload = {"name": name, "pin": pin, "mode": "toggle"}
    payload.update(kw)
    r = client.post("/api/gpio/switches", json=payload)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["switch"]


def test_estop_engage_blocks_switch_api(client):
    sw = _add_switch(client, interlock_group="bed")
    assert client.post(f"/api/gpio/switches/{sw['id']}/action", json={"action": "on"}).status_code == 200

    assert client.post("/api/estop/engage", json={"zone": "all", "reason": "bed ran away"}).status_code == 200
    status = client.get("/api/estop").get_json()
    assert status["engaged"] and status["engaged_zones"] == ["all"]

    r = client.post(f"/api/gpio/switches/{sw['id']}/action", json={"action": "on"})
    assert r.status_code == 409
    payload = r.get_json()
    assert payload["estop"] is True and payload["zone"] == "all"

    # Off always works, even latched.
    assert client.post(f"/api/gpio/switches/{sw['id']}/action", json={"action": "off"}).status_code == 200

    assert client.post("/api/estop/reset", json={"zone": "all"}).status_code == 200
    assert client.post(f"/api/gpio/switches/{sw['id']}/action", json={"action": "on"}).status_code == 200


def test_estop_zone_crud(client):
    _add_switch(client, name="Awning out", pin=22, interlock_group="awning")
    r = client.post("/api/estop/zones", json={"name": "Awning", "scope": "group", "refs": ["awning"]})
    assert r.status_code == 201
    zone_id = r.get_json()["zone"]["id"]
    assert client.put(f"/api/estop/zones/{zone_id}", json={"name": "Awning motor"}).status_code == 200
    assert any(z["name"] == "Awning motor" for z in client.get("/api/estop").get_json()["zones"])

    # The master stop is not editable or removable.
    assert client.put("/api/estop/zones/all", json={"name": "x"}).status_code == 400
    assert client.delete("/api/estop/zones/all").status_code == 400

    assert client.delete(f"/api/estop/zones/{zone_id}").status_code == 200
    assert [z["id"] for z in client.get("/api/estop").get_json()["zones"]] == ["all"]


def test_estop_input_rejects_a_pin_a_switch_uses(client):
    _add_switch(client, pin=17)
    r = client.post("/api/estop/inputs", json={"name": "Panic", "pin": 17})
    assert r.status_code == 400 and "already used" in r.get_json()["error"]
    assert client.post("/api/estop/inputs", json={"name": "Panic", "pin": 26}).status_code == 201
    pins = {p["bcm"]: p for p in client.get("/api/gpio/pins").get_json()["pins"]}
    assert "emergency stop" in (pins[26]["in_use_by"] or "")


def test_estop_blocks_scenes(client):
    sw = _add_switch(client)
    r = client.post("/api/scenes", json={"name": "Sit up", "actions": [
        {"type": "switch", "ref": sw["id"], "action": "on"}]})
    assert r.status_code == 201
    scene_id = r.get_json()["scene"]["id"]

    client.post("/api/estop/engage", json={"zone": "all"})
    result = client.post(f"/api/scenes/{scene_id}/run").get_json()
    assert result["failed"], "a scene must not drive a latched relay"
    assert "Emergency stop" in result["failed"][0]["error"]

    client.post("/api/estop/reset", json={"zone": "all"})
    assert not client.post(f"/api/scenes/{scene_id}/run").get_json()["failed"]


def test_estop_state_is_in_the_aggregate_snapshot(client):
    _add_switch(client)
    client.post("/api/estop/engage", json={"zone": "all", "reason": "testing"})
    gpio = client.get("/api/state").get_json()["gpio"]
    assert gpio["estop"]["engaged"] is True
    assert gpio["switches"][0]["locked_out"] is True
    assert gpio["switches"][0]["locked_by"] == "All relays"


def test_switch_cannot_steal_an_estop_input_pin(client):
    assert client.post("/api/estop/inputs", json={"name": "Panic", "pin": 26}).status_code == 201
    r = client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 26, "mode": "toggle"})
    assert r.status_code == 400 and "emergency stop button" in r.get_json()["error"]

    sw = _add_switch(client, name="Lamp", pin=17)
    r = client.put(f"/api/gpio/switches/{sw['id']}", json={"pin": 26})
    assert r.status_code == 400 and "emergency stop button" in r.get_json()["error"]
    # Testing that pin as a relay output would fight the button's pull resistor.
    r = client.post("/api/gpio/test", json={"pin": 26, "active_high": True})
    assert r.status_code == 400 and "not an output" in r.get_json()["error"]


def test_appearance_settings_round_trip(client):
    r = client.put("/api/dashboard", json={"theme": "daylight", "density": "roomy",
                                           "radius": "round", "text_scale": 125})
    assert r.status_code == 200
    dash = r.get_json()["dashboard"]
    assert (dash["theme"], dash["density"], dash["radius"], dash["text_scale"]) == ("daylight", "roomy", "round", 125)
    # The first paint must carry them, so nothing flashes on load.
    html = client.get("/").get_data(as_text=True)
    assert 'data-theme="daylight"' in html and 'data-radius="round"' in html
    assert "--text-scale: 1.250" in html

    for bad in ({"theme": "neon"}, {"density": "airy"}, {"radius": "blobby"}, {"text_scale": 400}):
        assert client.put("/api/dashboard", json=bad).status_code == 400
