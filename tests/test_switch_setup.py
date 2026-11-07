"""Adding several switches at once (a motor's Up and Down) is all-or-nothing."""


def _switches(client):
    return client.get("/api/gpio/switches").json["switches"]


def _pair(up=17, down=27, group="bed"):
    return {"switches": [
        {"name": "Bed up", "pin": up, "mode": "momentary", "interlock_group": group, "icon": "up", "active_high": False},
        {"name": "Bed down", "pin": down, "mode": "momentary", "interlock_group": group, "icon": "down", "active_high": False},
    ]}


def test_pair_is_added_together_with_both_tiles(client):
    r = client.post("/api/gpio/switches", json=_pair())
    assert r.status_code == 201, r.json
    names = [s["name"] for s in r.json["switches"]]
    assert names == ["Bed up", "Bed down"]
    assert all(s["max_on_seconds"] > 0 for s in r.json["switches"])  # hold-to-run always has a cap
    ids = {s["id"] for s in _switches(client)}
    assert len(ids) == 2
    tiles = client.get("/api/dashboard").json["dashboard"]["tiles"]
    assert ids <= {t["ref"] for t in tiles if t["type"] == "switch"}


def test_pair_with_a_taken_pin_adds_nothing(client):
    assert client.post("/api/gpio/switches", json={"name": "Lamp", "pin": 27}).status_code == 201
    r = client.post("/api/gpio/switches", json=_pair())
    assert r.status_code == 400
    assert "GPIO27" in r.json["error"]
    # The Up half must not have been kept on its own.
    assert [s["name"] for s in _switches(client)] == ["Lamp"]


def test_pair_on_the_same_pin_twice_adds_nothing(client):
    r = client.post("/api/gpio/switches", json=_pair(17, 17))
    assert r.status_code == 400
    assert _switches(client) == []


def test_pair_with_a_bad_half_adds_nothing(client):
    body = _pair()
    body["switches"][1]["mode"] = "spin"
    assert client.post("/api/gpio/switches", json=body).status_code == 400
    assert _switches(client) == []


def test_bad_batches_are_refused(client):
    assert client.post("/api/gpio/switches", json={"switches": []}).status_code == 400
    assert client.post("/api/gpio/switches", json={"switches": "nope"}).status_code == 400
    assert client.post("/api/gpio/switches", json={"switches": [1, 2]}).status_code == 400
    many = [{"name": f"S{i}", "pin": 2 + i} for i in range(17)]
    assert client.post("/api/gpio/switches", json={"switches": many}).status_code == 400
    assert _switches(client) == []


def test_single_add_still_returns_the_switch(client):
    r = client.post("/api/gpio/switches", json={"name": "Fan", "pin": 22})
    assert r.status_code == 201
    assert r.json["switch"]["name"] == "Fan"
