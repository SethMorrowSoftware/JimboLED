import time

import pytest

from jimboled.config import ConfigStore
from jimboled.wled.client import WLEDClient, WLEDError, normalise_host
from jimboled.wled.fxdata import build_effect_catalog, palette_catalog, parse_fxdata
from jimboled.wled.manager import DeviceError, DeviceManager, sanitise_state


def test_normalise_host():
    assert normalise_host("http://192.168.1.5/") == "192.168.1.5"
    assert normalise_host(" wled-abc.local ") == "wled-abc.local"
    assert normalise_host("10.0.0.2:8080") == "10.0.0.2:8080"
    with pytest.raises(WLEDError):
        normalise_host("")
    with pytest.raises(WLEDError):
        normalise_host("bad host!")


def test_fxdata_matches_stock_ui_rules():
    b = parse_fxdata("!,Duty cycle;!,!;!;01", 1)
    assert [s["visible"] for s in b["sliders"]][:3] == [True, True, False]
    assert b["sliders"][1]["label"] == "Duty cycle"
    assert [c["visible"] for c in b["colors"]] == [True, True, False]
    assert b["palette"]["visible"]
    solid = parse_fxdata("", 0)
    assert not any(s["visible"] for s in solid["sliders"])
    assert [c["visible"] for c in solid["colors"]] == [True, False, False]
    assert not solid["palette"]["visible"]
    other_empty = parse_fxdata("", 62)
    assert all(c["visible"] for c in other_empty["colors"]) and other_empty["palette"]["visible"]
    pal = parse_fxdata("Shift,Size,Rotation,,,Animate Shift,Animate Rotation,Anamorphic;;!;12;ix=112,c1=0,o1=1", 65)
    assert not any(c["visible"] for c in pal["colors"]) and pal["flags"]["two_d"] and pal["defaults"]["ix"] == 112
    assert not parse_fxdata("!,!;!;;1", 71)["palette"]["visible"]
    assert parse_fxdata("!,!;;Palette=11;1", 70)["defaults"] == {"pal": 11}


def test_catalogs_drop_reserved_and_handle_custom_palettes():
    cat = build_effect_catalog(["Solid", "RSVD", "Blink@!,Duty cycle;!,!;!;01", "-"], [])
    assert [e["id"] for e in cat] == [0, 2] and cat[1]["sliders"][1]["label"] == "Duty cycle"
    assert palette_catalog(["Default"], {"ver": "16.0.1", "vid": 2605010, "cpalcount": 1})[-1]["id"] == 200
    assert palette_catalog(["Default"], {"ver": "0.15.4", "vid": 2508020, "cpalcount": 1})[-1]["id"] == 255
    assert palette_catalog(["Default"], {"ver": "14.7.1", "vid": 2607011, "product": "MoonModules", "cpalcount": 1})[-1]["id"] == 255


def test_sanitise_state():
    assert sanitise_state({"on": True, "bri": 300, "bogus": 1}) == {"on": True, "bri": 255}
    assert sanitise_state({"seg": {"col": ["#ff0000"], "fx": "5"}})["seg"] == {"col": [[255, 0, 0]], "fx": 5}
    assert sanitise_state({"seg": [{"id": 1, "stop": 0}]})["seg"] == [{"id": 1, "stop": 0}]
    assert sanitise_state({"ps": "1~5~"})["ps"] == "1~5~"
    assert sanitise_state({"on": "t"})["on"] == "t"
    with pytest.raises(DeviceError):
        sanitise_state({"bogus": 1})
    with pytest.raises(DeviceError):
        sanitise_state({"seg": {"col": [[1, 2]], "n": 5, "start": "x"}})


def test_client_against_fake(fake_wled):
    srv = fake_wled("Strip", 30, "aabbccdd0001")
    c = WLEDClient(srv.host, timeout=2)
    doc = c.get_state_info()
    assert doc["info"]["name"] == "Strip" and doc["state"]["on"] is True
    st = c.set_state({"on": False, "bri": 10})
    assert st["on"] is False and st["bri"] == 10
    assert len(c.get_effects()) == 30 and c.get_palettes()[0] == "Default"
    assert "1" in c.get_presets() and "0" not in c.get_presets()
    c.save_preset(9, "Nine", include_brightness=True)
    assert c.get_presets()["9"]["n"] == "Nine"
    c.delete_preset(9)
    assert "9" not in c.get_presets()
    srv.fake["fail"] = True
    with pytest.raises(WLEDError):
        c.get_state()


def test_device_manager_lifecycle(fake_wled, data_dir):
    a = fake_wled("A", 60, "aabbccdd0002")
    store = ConfigStore(data_dir)
    store.update(lambda c: c.update({
        "devices": [{"id": "a", "name": "A", "host": a.host, "enabled": True}, {"id": "ghost", "name": "G", "host": "127.0.0.1:1", "enabled": True}],
        "wled": {"poll_interval_s": 1, "offline_poll_interval_s": 3, "request_timeout_s": 2},
    }))
    dm = DeviceManager(store)
    dm.start()
    try:
        dm.refresh("a")
        dm.refresh("ghost")
        assert dm.summary("a")["online"] and not dm.summary("ghost")["online"]
        r = dm.set_state("a", {"on": True, "bri": 99, "seg": {"fx": 9}})
        assert r["state"]["bri"] == 99 and r["effect_name"] == "Rainbow"
        with pytest.raises(DeviceError):
            dm.set_state("ghost", {"on": True})
        assert dm.set_state_all({"on": False}) == {"a": "ok"}
        dm.save_preset("a", 7, "Seven")
        assert any(p["id"] == 7 for p in dm.presets("a"))
        assert dm.set_state("a", {"ps": 7})["preset_name"] == "Seven"
        full = dm.full("a")
        assert full["effects"] and full["palettes"] and full["state_full"]["ps"] == 7
        store.update(lambda c: c.update({"devices": [d for d in c["devices"] if d["id"] == "a"]}))
        assert not dm.has("ghost")
    finally:
        dm.stop()
