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
