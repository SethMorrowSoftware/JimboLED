"""API tokens for integrations, and backup/restore of the newer sections."""
import io
import json


def test_tokens_lifecycle(client, app):
    assert client.post("/api/tokens", json={"name": ""}).status_code == 400
    r = client.post("/api/tokens", json={"name": "Home Assistant"})
    assert r.status_code == 201
    token = r.json["token"]
    assert token.startswith("jl_") and len(token) > 30
    listed = client.get("/api/tokens").json["tokens"]
    assert listed[0]["name"] == "Home Assistant" and "hash" not in listed[0] and token.startswith(listed[0]["prefix"])
    settings = client.get("/api/settings").json
    assert all("hash" not in t for t in settings["server"]["api_tokens"])

    # Lock the dashboard: a bare client is now refused, a token is not.
    client.post("/api/settings/password", json={"password": "secret1"})
    bare = app.test_client()
    assert bare.get("/api/state").status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    assert bare.get("/api/state", headers=auth).status_code == 200
    # No X-Requested-With needed with a token.
    r = bare.post("/api/timers", json={"minutes": 5}, headers=auth)
    assert r.status_code == 201, r.json
    assert bare.get("/api/state", headers={"Authorization": "Bearer jl_wrong"}).status_code == 401
    # What a token may not touch.
    assert bare.post("/api/tokens", json={"name": "x"}, headers=auth).status_code == 403
    assert bare.post("/api/settings/password", json={"password": ""}, headers=auth).status_code == 403
    assert bare.get("/api/backup", headers=auth).status_code == 403
    assert bare.post("/api/restore", data=b"{}", headers=auth).status_code == 403
    assert client.get("/api/tokens").json["tokens"][0]["last_used"]

    tid = listed[0]["id"]
    assert client.delete(f"/api/tokens/{tid}").status_code == 200
    assert bare.get("/api/state", headers=auth).status_code == 401


def test_backup_round_trip_keeps_new_sections(client, fake_wled):
    srv = fake_wled("Bed", 60, "aabbccdd0b01")
    did = client.post("/api/devices", json={"host": srv.host}).json["device"]["id"]
    gid = client.post("/api/groups", json={"name": "Bedroom", "devices": [did]}).json["group"]["id"]
    client.put("/api/location", json={"lat": 40.7, "lon": -74.0})
    client.post("/api/schedules", json={"name": "Night", "when": {"type": "time", "time": "23:00"},
                                        "action": {"type": "lights", "target": f"group:{gid}", "intent": {"on": False}}})
    client.put("/api/dashboard", json={"favorite_colors": ["#ff0000"]})
    token = client.post("/api/tokens", json={"name": "Script"}).json["token"]

    backup = client.get("/api/backup")
    data = json.loads(backup.data)
    assert "api_tokens" not in data["server"] and "secret_key" not in data["server"]
    assert data["groups"][0]["name"] == "Bedroom" and data["schedules"][0]["name"] == "Night"

    # Damage it a little: a bad schedule, a bad group, a bad location, a junk colour.
    data["schedules"].append({"name": "broken", "when": {"type": "time", "time": "99:99"}, "action": {"type": "all_off"}})
    data["groups"].append({"name": ""})
    data["location"] = {"lat": "north", "lon": 5}
    data["dashboard"]["favorite_colors"].append("not a colour")
    client.delete(f"/api/groups/{gid}")
    r = client.post("/api/restore", data={"file": (io.BytesIO(json.dumps(data).encode()), "b.json")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    cfg = client.application.extensions["jimboled"].store.get()
    assert [g["name"] for g in cfg["groups"]] == ["Bedroom"]
    assert [s["name"] for s in cfg["schedules"]] == ["Night"]
    assert cfg["location"]["lat"] is None
    assert cfg["dashboard"]["favorite_colors"] == ["#ff0000"]
    # The token that existed before the restore still works.
    assert client.application.test_client().get("/api/state", headers={"Authorization": f"Bearer {token}"}).status_code == 200
