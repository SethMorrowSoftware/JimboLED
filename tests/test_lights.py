"""Controlling many controllers at once: intents, the lights API and groups."""
import time

import pytest

from jimboled.wled import intents as I

# A second controller that numbers its effects differently, has two segments
# and a white channel, and keeps "Movie" in another preset slot.
EFFECTS_B = ["Solid", "Fire 2012", "Rainbow", "Blink", "Breathe"]
PRESETS_B = {"0": {}, "4": {"n": "movie", "on": True, "bri": 40, "seg": [{"id": 0, "fx": 2}]}}


def _view(**kw):
    state = kw.pop("state", {"on": True, "bri": 128, "mainseg": 0,
                             "seg": [{"id": 0, "start": 0, "stop": 30, "col": [[255, 0, 0], [0, 0, 0], [0, 0, 0]], "fx": 0, "pal": 0},
                                     {"id": 1, "start": 30, "stop": 60, "col": [[0, 255, 0], [0, 0, 0], [0, 0, 0]], "fx": 0, "pal": 0}]})
    return I.DeviceView(name=kw.pop("name", "Test"), online=kw.pop("online", True), state=state,
                        info=kw.pop("info", {"leds": {"lc": 1}}),
                        effects=kw.pop("effects", [{"id": i, "name": n} for i, n in enumerate(["Solid", "Blink", "Fire 2012"])]),
                        palettes=kw.pop("palettes", [{"id": 0, "name": "Default"}, {"id": 6, "name": "Party"}]),
                        presets=kw.pop("presets", {"3": {"n": "Movie"}, "7": {"n": "movie"}}))


# ---------------------------------------------------------------- intents
def test_normalise_intent_validates_and_converts():
    out = I.normalise_intent({"color": "#FF8800", "brightness": 50, "transition": 1.25, "effect": "solid"})
    assert out["colors"] == [[255, 136, 0]]
    assert out["bri"] in (127, 128)
    assert out["transition"] in (1.2, 1.3)
    assert out["effect"] in ("Solid", "solid", 0)
    with pytest.raises(I.IntentError):
        I.normalise_intent({"color": "red"})
    with pytest.raises(I.IntentError):
        I.normalise_intent({})
    assert I.normalise_intent({"bri": 999})["bri"] == 255  # clamped, not refused
    with pytest.raises(I.IntentError):
        I.normalise_intent({"bri": "bright"})
    nl = I.normalise_intent({"nightlight": {"on": True, "minutes": 30, "mode": "fade"}})
    assert nl["nightlight"]["on"] and nl["nightlight"]["dur"] == 30


def test_plan_maps_names_per_device_and_every_segment():
    view = _view()
    payload, notes = I.plan(view, I.normalise_intent({"color": "#0000ff", "effect": "Fire 2012", "palette": "Party"}))
    assert payload["on"] is True
    segs = payload["seg"]
    assert [s["id"] for s in segs] == [0, 1]
    assert all(s["fx"] == 2 and s["pal"] == 6 and s["col"][0] == [0, 0, 255] for s in segs)
    assert all(s.get("fxdef") for s in segs)  # no speed given: use the effect's own defaults
    assert notes == []


def test_plan_notes_missing_effect_and_picks_lowest_preset_slot():
    view = _view()
    payload, notes = I.plan(view, I.normalise_intent({"effect": "Not An Effect"}))
    assert payload is None and notes
    payload, _ = I.plan(view, I.normalise_intent({"preset": "MOVIE"}))
    assert payload["ps"] == 3


def test_plan_white_only_on_capable_devices():
    rgb = _view()
    payload, notes = I.plan(rgb, I.normalise_intent({"white": 200}))
    assert payload is None and any("white" in n for n in notes)
    rgbw = _view(info={"leds": {"lc": 3}}, state={"on": True, "bri": 10, "seg": [
        {"id": 0, "start": 0, "stop": 10, "col": [[1, 2, 3, 4], [0, 0, 0, 0], [0, 0, 0, 0]], "fx": 0}]})
    payload, notes = I.plan(rgbw, I.normalise_intent({"white": 200}))
    assert payload["seg"][0]["col"][0] == [1, 2, 3, 200]


def test_plan_sync_and_transition_and_nightlight():
    payload, _ = I.plan(_view(), I.normalise_intent({"on": True, "transition": 2, "sync": True,
                                                     "nightlight": {"on": True, "minutes": 10}}))
    assert payload["tt"] == 20 and payload["tb"] == 0
    assert payload["nl"]["on"] is True and payload["nl"]["dur"] == 10


def test_union_catalog_counts_shared_names():
    a = _view(name="A", presets={"1": {"n": "Movie"}})
    b = _view(name="B", effects=[{"id": 0, "name": "Solid"}, {"id": 1, "name": "Fire 2012"}],
              presets={"5": {"n": "movie"}, "6": {"n": "Party"}})
    off = _view(name="C", online=False)
    cat = I.union_catalog([a, b, off])
    assert cat["total"] == 3 and cat["online"] == 2
    fx = {e["name"]: e["count"] for e in cat["effects"]}
    assert fx["Fire 2012"] == 2 and fx["Blink"] == 1
    assert cat["effects"][0]["name"] == "Solid"
    presets = {p["name"].lower(): p["count"] for p in cat["presets"]}
    assert presets["movie"] == 2 and presets["party"] == 1


def test_look_intent_round_trips():
    view = _view()
    intent = I.look_intent(view)
    assert intent["best_effort"] and intent["colors"][0] == [255, 0, 0]
    payload, notes = I.plan(_view(info={"leds": {"lc": 3}}), I.normalise_intent(intent))
    assert payload and notes == []


# --------------------------------------------------------------- the API
@pytest.fixture
def two(client, fake_wled):
    a = fake_wled("Bed", 60, "aabbccdd0a01")
    b = fake_wled("Desk", 40, "aabbccdd0a02", effects=EFFECTS_B, segments=2, lc=3, presets=PRESETS_B)
    da = client.post("/api/devices", json={"host": a.host}).json["device"]["id"]
    db = client.post("/api/devices", json={"host": b.host}).json["device"]["id"]
    assert client.post(f"/api/devices/{da}/presets", json={"name": "Movie", "slot": 9}).status_code == 200
    return a, b, da, db


def test_all_lights_colour_and_effect_by_name(client, two):
    a, b, da, db = two
    cat = client.get("/api/lights/catalog").json
    assert cat["online"] == 2
    assert {p["name"].lower(): p["count"] for p in cat["presets"]}["movie"] == 2
    r = client.post("/api/lights/apply", json={"target": "all", "color": "#ff0000", "effect": "Fire 2012"})
    assert r.status_code == 200 and r.json["ok"], r.json
    fire_a = [e for e in a.fake["effects"]].index("Fire 2012")
    assert a.fake["state"]["seg"][0]["fx"] == fire_a and a.fake["state"]["seg"][0]["col"][0][:3] == [255, 0, 0]
    assert [s["fx"] for s in b.fake["state"]["seg"]] == [1, 1]
    assert all(s["col"][0][:3] == [255, 0, 0] for s in b.fake["state"]["seg"])


def test_all_lights_preset_by_name_uses_each_slot(client, two):
    a, b, _, _ = two
    r = client.post("/api/lights/apply", json={"target": "all", "intent": {"preset": "Movie"}})
    assert r.json["ok"], r.json
    assert a.fake["state"]["ps"] == 9 and b.fake["state"]["ps"] == 4


def test_partial_and_white_and_errors(client, two):
    a, b, _, _ = two
    r = client.post("/api/lights/apply", json={"effect": "Colorwaves"}).json
    assert not r["ok"] and r["changed"] == 1 and "Desk" in r["message"]
    r = client.post("/api/lights/apply", json={"color": "#ffffff", "white": 200}).json
    assert b.fake["state"]["seg"][1]["col"][0] == [255, 255, 255, 200]
    assert len(a.fake["state"]["seg"][0]["col"][0]) == 3
    assert client.post("/api/lights/apply", json={"color": "red"}).status_code == 400
    assert client.post("/api/lights/apply", json={}).status_code == 400
    assert client.post("/api/lights/apply", json={"target": "group:nope", "on": True}).status_code == 404
    assert client.post("/api/lights/apply", json={"target": 7, "on": True}).status_code == 400


def test_offline_controller_is_reported_not_fatal(client, two, fake_wled):
    a, b, da, db = two
    r = client.post("/api/devices", json={"host": "10.255.255.1", "skip_probe": True})
    r = client.post("/api/lights/apply", json={"on": False}).json
    statuses = {x["name"]: x["status"] for x in r["results"]}
    assert statuses["Bed"] == "ok" and statuses["Desk"] == "ok"
    assert "offline" in statuses.values() and not r["ok"]
    assert a.fake["state"]["on"] is False and b.fake["state"]["on"] is False


def test_groups_crud_target_and_cleanup(client, two):
    a, b, da, db = two
    r = client.post("/api/groups", json={"name": "Bedroom", "devices": [da, "ghost", da], "color": "#123456"})
    assert r.status_code == 201
    g = r.json["group"]
    assert g["devices"] == [da]
    assert client.post("/api/groups", json={"name": "bedroom"}).status_code == 409
    assert client.post("/api/groups", json={"name": ""}).status_code == 400
    tiles = client.get("/api/dashboard").json["dashboard"]["tiles"]
    assert any(t["type"] == "group" and t["ref"] == g["id"] for t in tiles)
    client.post("/api/lights/apply", json={"on": True})
    r = client.post("/api/lights/apply", json={"target": f"group:{g['id']}", "on": False}).json
    assert r["total"] == 1 and a.fake["state"]["on"] is False and b.fake["state"]["on"] is True
    r = client.put(f"/api/groups/{g['id']}", json={"name": "Main bedroom", "devices": [da, db]})
    assert r.json["groups"][0]["name"] == "Main bedroom"
    cat = client.get(f"/api/lights/catalog?target=group:{g['id']}").json
    assert cat["label"] == "Main bedroom" and cat["total"] == 2
    client.delete(f"/api/devices/{da}")
    assert client.get("/api/groups").json["groups"][0]["devices"] == [db]
    assert client.delete(f"/api/groups/{g['id']}").status_code == 200
    assert not any(t["type"] == "group" for t in client.get("/api/dashboard").json["dashboard"]["tiles"])
    assert client.delete(f"/api/groups/{g['id']}").status_code == 404


def test_capture_copy_look_and_copy_preset(client, two):
    a, b, da, db = two
    client.post(f"/api/devices/{db}/state", json={"on": True, "bri": 90, "seg": [{"id": 0, "col": [[0, 0, 255, 0]], "fx": 2}]})
    cap = client.post("/api/lights/capture", json={}).json
    assert len(cap["actions"]) == 2 and all(x["type"] == "device" for x in cap["actions"])
    r = client.post(f"/api/devices/{db}/copy", json={"target": "all"}).json
    assert r["ok"], r
    assert a.fake["state"]["bri"] == 90
    assert a.fake["state"]["seg"][0]["fx"] == a.fake["effects"].index("Rainbow")
    r = client.post(f"/api/devices/{db}/presets/4/copy", json={"target": "all"}).json
    assert r["ok"], r
    # Bed already had a "Movie" in slot 9: it is overwritten there, not duplicated.
    names = {k: v.get("n") for k, v in a.fake["presets"].items() if v}
    assert names.get("9", "").lower() == "movie" and list(names.values()).count("movie") + list(names.values()).count("Movie") == 1
    assert client.post(f"/api/devices/{db}/presets/99/copy", json={"target": "all"}).status_code in (400, 404)


def test_lights_scene_runs(client, two):
    a, b, _, _ = two
    r = client.post("/api/scenes", json={"name": "Red", "actions": [
        {"type": "lights", "target": "all", "intent": {"color": "#ff0000", "brightness": 100}}]})
    assert r.status_code == 201, r.json
    run = client.post(f"/api/scenes/{r.json['scene']['id']}/run").json
    assert run["failed"] == []
    assert a.fake["state"]["bri"] == 255 and b.fake["state"]["seg"][1]["col"][0][:3] == [255, 0, 0]
    bad = client.post("/api/scenes", json={"name": "Bad", "actions": [{"type": "lights", "intent": {"color": "nope"}}]})
    assert bad.status_code == 400


def test_favourite_colours(client):
    r = client.put("/api/dashboard", json={"favorite_colors": ["#FF0000", "#ff0000", "bad", "#00ff00"]})
    assert r.json["dashboard"]["favorite_colors"] == ["#ff0000", "#00ff00"]
