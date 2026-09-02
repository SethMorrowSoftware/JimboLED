"""A small in-process imitation of a WLED controller for tests and demos.

It implements the subset of the JSON API that JimboLED uses, with realistic
state/info documents, preset storage and the ``v``/``psave``/``pdel``/``ps``
semantics of the real firmware.
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List

from flask import Flask, jsonify, request

EFFECTS = ["Solid", "Blink", "Breathe", "Wipe", "Wipe Random", "Random Colors", "Sweep", "Dynamic",
           "Colorloop", "Rainbow", "Scan", "Scan Dual", "Fade", "Theater", "Theater Rainbow",
           "Running", "Saw", "Twinkle", "Dissolve", "Dissolve Rnd", "Sparkle", "RSVD", "Strobe",
           "Fireworks", "Rain", "Chase", "Lightning", "Fire 2012", "Pride 2015", "Colorwaves"]
FXDATA = ["", "!,Duty cycle;!,!;!;01", "!,;!,!;!;01", "!,!;!,!;!;01", "!,,,,,Random;;!;01", "!,Fade time;;!;01",
          "!,!;!,!;!;01", "!,!;;!;01", "!;;!;01", "!,Size;;!;01", "!,# of dots,,,,Overlay;!,!,!;!;01",
          "!,# of dots,,,,Overlay;!,!,!;!;01", "!;!;!;01", "!,Gap size;!,!;!;01", "!,Gap size;,!;!;01",
          "!,Wave width;!,!;!;01", "!,Width;!,!;!;01", "!,!;!,!;!;01;sx=32", "!,Repeat;!,!;!;01", "!,Repeat;!,!;!;01",
          "!,,,,,Overlay;!,!;!;01", "", "!,!;!,!;!;01", "!,Frequency;!,!;!;01", "!,Spawning rate;!,!;!;01",
          "!,Width;!,!,!;!;01", "!,!;!,!;!;01", "Cooling,Spark rate,,2D Blur,Boost;;!;1;sx=64,ix=160,m12=1",
          "Speed;;;01;sx=64", "!,Hue;;!;01"]
PALETTES = ["Default", "* Random Cycle", "* Color 1", "* Colors 1&2", "* Color Gradient", "* Colors Only",
            "Party", "Cloud", "Lava", "Ocean", "Forest", "Rainbow", "Rainbow Bands", "Sunset", "Rivendell"]


def _seg(i: int, start: int, stop: int) -> Dict[str, Any]:
    return {"id": i, "start": start, "stop": stop, "len": stop - start, "grp": 1, "spc": 0, "of": 0,
            "on": True, "frz": False, "bri": 255, "cct": 127, "set": 0, "n": "",
            "col": [[255, 160, 0], [0, 0, 0], [0, 0, 0]], "fx": 0, "sx": 128, "ix": 128, "pal": 0,
            "c1": 128, "c2": 128, "c3": 16, "sel": i == 0, "rev": False, "mi": False,
            "o1": False, "o2": False, "o3": False, "si": 0, "m12": 0}


def make_state(leds: int = 60) -> Dict[str, Any]:
    return {"on": True, "bri": 128, "transition": 7, "ps": -1, "pl": -1,
            "nl": {"on": False, "dur": 60, "mode": 1, "tbri": 0, "rem": -1},
            "udpn": {"send": False, "recv": True, "sgrp": 1, "rgrp": 1},
            "lor": 0, "mainseg": 0, "seg": [_seg(0, 0, leds)]}


def make_info(name: str, leds: int = 60, mac: str = "aabbccddeeff", ip: str = "127.0.0.1") -> Dict[str, Any]:
    return {"ver": "0.15.0", "vid": 2412100, "cn": "Kōsen", "release": "ESP32",
            "leds": {"count": leds, "pwr": 420, "fps": 42, "maxpwr": 850, "maxseg": 32, "cct": 0,
                     "lc": 1, "seglc": [1], "bootps": 0},
            "str": False, "name": name, "udpport": 21324, "live": False, "liveseg": -1, "lm": "", "lip": "",
            "ws": 0, "fxcount": len(EFFECTS), "palcount": len(PALETTES), "cpalcount": 0, "maps": [{"id": 0}],
            "wifi": {"bssid": "00:11:22:33:44:55", "rssi": -58, "signal": 84, "channel": 6, "ap": False},
            "fs": {"u": 12, "t": 983, "pmt": 0}, "ndc": 1, "arch": "esp32", "core": "v3.3.6",
            "clock": 240, "flash": 4, "lwip": 0, "freeheap": 190000, "uptime": 4321, "time": "2026-1-1, 00:00:00",
            "opt": 79, "brand": "WLED", "product": "FOSS", "mac": mac, "ip": ip}


def create_fake_wled(name: str = "Fake WLED", leds: int = 60, mac: str = "aabbccddeeff") -> Flask:
    app = Flask(f"fake-wled-{mac}")
    lock = threading.Lock()
    state = make_state(leds)
    info = make_info(name, leds, mac)
    presets: Dict[str, Dict[str, Any]] = {
        "0": {},
        "1": {"n": "Warm White", "on": True, "bri": 200, "mainseg": 0, "seg": [{"id": 0, "col": [[255, 180, 100]], "fx": 0}]},
        "2": {"n": "Rainbow", "on": True, "bri": 255, "seg": [{"id": 0, "fx": 9, "pal": 11}]},
        "3": {"n": "Party mode", "playlist": {"ps": [1, 2], "dur": [30, 30], "transition": [7, 7], "repeat": 0, "end": 0}},
    }
    app.config["FAKE"] = {"state": state, "info": info, "presets": presets, "log": [], "fail": False}
    app.config["fake_nodes"]: List[Dict[str, Any]] = []

    def apply(payload: Dict[str, Any]) -> None:
        f = app.config["FAKE"]
        st = f["state"]
        f["log"].append(copy.deepcopy(payload))
        if payload.get("rb"):
            return
        if "psave" in payload:
            slot = str(int(payload["psave"]))
            saved: Dict[str, Any] = {"n": payload.get("n", f"Preset {slot}")}
            if payload.get("ql"):
                saved["ql"] = payload["ql"]
            if payload.get("o"):
                saved.update({k: v for k, v in payload.items() if k in ("on", "bri", "seg", "playlist", "mainseg")})
            else:
                saved.update({"on": st["on"], "bri": st["bri"], "mainseg": st["mainseg"], "seg": copy.deepcopy(st["seg"])})
            f["presets"][slot] = saved
            return
        if "pdel" in payload:
            f["presets"].pop(str(int(payload["pdel"])), None)
            return
        if "ps" in payload:
            slot = str(int(payload["ps"]))
            preset = f["presets"].get(slot)
            if preset:
                st["ps"] = int(slot)
                if "playlist" in preset:
                    st["pl"] = int(slot)
                for k in ("on", "bri", "mainseg"):
                    if k in preset:
                        st[k] = preset[k]
                for pseg in preset.get("seg", []):
                    for seg in st["seg"]:
                        if seg["id"] == pseg.get("id", 0):
                            seg.update({k: v for k, v in pseg.items() if k != "id"})
                return
        for key in ("on", "bri", "transition", "mainseg", "lor", "pl"):
            if key in payload:
                val = payload[key]
                if key == "on" and val == "t":
                    val = not st["on"]
                st[key] = val
        if "tt" in payload:
            pass
        for key in ("nl", "udpn"):
            if isinstance(payload.get(key), dict):
                st[key].update(payload[key])
        if "seg" in payload:
            segs = payload["seg"] if isinstance(payload["seg"], list) else [payload["seg"]]
            for i, pseg in enumerate(segs):
                sid = pseg.get("id", i if len(segs) > 1 else st["mainseg"])
                existing = next((s for s in st["seg"] if s["id"] == sid), None)
                if existing is None:
                    if "stop" in pseg and pseg["stop"] > 0:
                        existing = _seg(sid, pseg.get("start", 0), pseg["stop"])
                        st["seg"].append(existing)
                        st["seg"].sort(key=lambda s: s["id"])
                    else:
                        continue
                if pseg.get("stop") == 0:
                    st["seg"] = [s for s in st["seg"] if s["id"] != sid]
                    continue
                for k, v in pseg.items():
                    if k == "id":
                        continue
                    if k == "col":
                        for ci, c in enumerate(v[:3]):
                            existing["col"][ci] = list(c) + [0] * (3 - len(c)) if len(c) < 3 else list(c)
                    elif k in existing or k in ("startY", "stopY", "i"):
                        existing[k] = v
                existing["len"] = existing["stop"] - existing["start"]
            if "ps" not in payload:
                st["ps"] = -1
        if any(k in payload for k in ("on", "bri", "seg", "nl")):
            pass

    def maybe_fail():
        if app.config["FAKE"]["fail"]:
            from flask import abort
            abort(503)

    @app.get("/json")
    def json_all():
        maybe_fail()
        f = app.config["FAKE"]
        return jsonify({"state": f["state"], "info": f["info"], "effects": EFFECTS, "palettes": PALETTES})

    @app.post("/json")
    def json_post():
        maybe_fail()
        with lock:
            apply(request.get_json(force=True, silent=True) or {})
            return jsonify({"success": True})

    @app.get("/json/si")
    def json_si():
        maybe_fail()
        f = app.config["FAKE"]
        return jsonify({"state": f["state"], "info": f["info"]})

    @app.get("/json/state")
    def json_state():
        maybe_fail()
        return jsonify(app.config["FAKE"]["state"])

    @app.post("/json/state")
    def json_state_post():
        maybe_fail()
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            return jsonify({"error": 9}), 400
        with lock:
            apply(payload)
            if payload.get("v"):
                return jsonify(app.config["FAKE"]["state"])
            return jsonify({"success": True})

    @app.get("/json/info")
    def json_info():
        maybe_fail()
        return jsonify(app.config["FAKE"]["info"])

    @app.get("/json/eff")
    def json_eff():
        maybe_fail()
        return jsonify(EFFECTS)

    @app.get("/json/pal")
    def json_pal():
        maybe_fail()
        return jsonify(PALETTES)

    @app.get("/json/fxdata")
    def json_fxdata():
        maybe_fail()
        return jsonify(FXDATA)

    @app.get("/presets.json")
    def presets_json():
        maybe_fail()
        return jsonify(app.config["FAKE"]["presets"])

    @app.get("/json/nodes")
    def json_nodes():
        maybe_fail()
        return jsonify({"nodes": app.config.get("fake_nodes", [])})

    @app.get("/json/cfg")
    def json_cfg():
        maybe_fail()
        return jsonify({"id": {"name": app.config["FAKE"]["info"]["name"]}, "hw": {"led": {"total": leds}}})

    return app


class FakeWLEDServer:
    """Run a fake controller on a background thread (for tests)."""

    def __init__(self, name: str = "Fake WLED", leds: int = 60, mac: str = "aabbccddeeff", port: int = 0):
        import socket
        from werkzeug.serving import make_server

        self.app = create_fake_wled(name, leds, mac)
        if port == 0:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
        self.port = port
        self._server = make_server("127.0.0.1", port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def fake(self) -> Dict[str, Any]:
        return self.app.config["FAKE"]

    def start(self) -> "FakeWLEDServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()


if __name__ == "__main__":  # demo: python -m tests.fake_wled 8081
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    create_fake_wled("Demo WLED").run(host="127.0.0.1", port=port)
