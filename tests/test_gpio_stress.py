"""Concurrency stress for the interlock/dead-time logic (mock pin factory).

Many threads hammer interlocked switches while a monitor samples the pins
every millisecond; at no instant may two members of a group be energised.
"""
import random
import threading
import time

from jimboled.config import ConfigStore
from jimboled.gpio.manager import GPIOError, GPIOManager


def test_interlock_never_violated_under_load(tmp_path, monkeypatch):
    monkeypatch.setenv("GPIOZERO_PIN_FACTORY", "mock")
    store = ConfigStore(tmp_path)
    store.update(lambda c: c["gpio"].update({"interlock_dead_time_ms": 40, "hold_timeout_s": 0.3, "switches": [
        {"id": "up", "name": "Up", "pin": 17, "mode": "momentary", "active_high": False, "interlock_group": "bed", "max_on_seconds": 2},
        {"id": "down", "name": "Down", "pin": 27, "mode": "momentary", "active_high": True, "interlock_group": "bed", "max_on_seconds": 2},
        {"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle", "interlock_group": "bed"},
        {"id": "tap", "name": "Tap", "pin": 23, "mode": "pulse", "pulse_ms": 30, "interlock_group": "bed"},
        {"id": "free", "name": "Free", "pin": 24, "mode": "toggle"},
    ]}))
    gm = GPIOManager(store)
    gm.start()
    try:
        devs = {sid: rt.device for sid, rt in gm._switches.items()}
        violations, errors = [], []
        stop = threading.Event()

        def monitor():
            while not stop.is_set():
                on = [sid for sid in ("up", "down", "lamp", "tap") if devs[sid].value]
                if len(on) > 1:
                    violations.append(tuple(on))
                time.sleep(0.001)

        def worker(n):
            rnd = random.Random(n)
            while not stop.is_set():
                try:
                    sid = rnd.choice(["up", "down", "lamp", "tap", "free"])
                    act = rnd.choice(["press", "toggle", "pulse", "on", "off", "allof", "reconf"])
                    if act == "press" and sid in ("up", "down"):
                        snap = gm.press(sid, source=f"w{n}")
                        for _ in range(rnd.randint(0, 4)):
                            time.sleep(rnd.random() * 0.03)
                            gm.heartbeat(sid, snap["token"])
                        if rnd.random() < 0.7:
                            gm.release(sid, snap["token"])
                    elif act == "toggle" and sid in ("lamp", "free"):
                        gm.toggle(sid)
                    elif act == "pulse" and sid == "tap":
                        gm.pulse(sid)
                    elif act == "on" and sid in ("lamp", "free"):
                        gm.turn_on(sid)
                    elif act == "off":
                        gm.turn_off(sid)
                    elif act == "allof" and rnd.random() < 0.05:
                        gm.all_off()
                    elif act == "reconf" and rnd.random() < 0.05:
                        grp = rnd.choice(["bed", ""])
                        store.update(lambda c, g=grp: [s.update({"interlock_group": g}) for s in c["gpio"]["switches"] if s["id"] == "free"])
                except GPIOError:
                    pass
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(repr(exc))
                time.sleep(rnd.random() * 0.005)

        mon = threading.Thread(target=monitor, daemon=True)
        mon.start()
        workers = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(6)]
        for w in workers:
            w.start()
        time.sleep(4.0)
        stop.set()
        for w in workers:
            w.join(3)
        mon.join(1)
        gm.all_off()
        time.sleep(0.2)
        assert not errors, errors[:3]
        assert not violations, violations[:3]
        assert not [sid for sid, d in devs.items() if d.value]
    finally:
        gm.stop()
