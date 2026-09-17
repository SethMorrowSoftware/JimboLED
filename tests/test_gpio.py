import time

import pytest

from jimboled.config import ConfigStore
from jimboled.gpio.manager import GPIOError, GPIOManager, SwitchConfig, validate_switches


def make_manager(data_dir, switches, **gpio):
    store = ConfigStore(data_dir)
    cfg = {"backend": "mock", "hold_timeout_s": 0.3, "interlock_dead_time_ms": 150, "switches": switches}
    cfg.update(gpio)
    store.update(lambda c: c["gpio"].update(cfg))
    m = GPIOManager(store)
    m.start()
    return store, m


def state(m, sid):
    return next(s for s in m.snapshot()["switches"] if s["id"] == sid)


def test_validation_rejects_bad_pins_and_duplicates():
    with pytest.raises(GPIOError):
        SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 0})
    with pytest.raises(GPIOError):
        SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 99})
    with pytest.raises(GPIOError):
        SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 17, "mode": "weird"})
    with pytest.raises(GPIOError):
        validate_switches([{"id": "a", "name": "A", "pin": 17}, {"id": "b", "name": "B", "pin": 17}])
    # momentary always gets a cap
    assert SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 17, "mode": "momentary", "max_on_seconds": 0}).max_on_seconds == 60


def test_momentary_requires_heartbeat(data_dir):
    _, m = make_manager(data_dir, [{"id": "up", "name": "Up", "pin": 17, "mode": "momentary"}])
    try:
        s = m.press("up")
        assert s["on"] and s["token"]
        assert m.heartbeat("up", s["token"])["held"]
        assert not m.heartbeat("up", "wrong")["held"]
        time.sleep(0.6)
        st = state(m, "up")
        assert not st["on"] and st["last_off_reason"] == "heartbeat-timeout"
        with pytest.raises(GPIOError):
            m.turn_on("up")
    finally:
        m.stop()


def test_interlock_never_both_on(data_dir):
    _, m = make_manager(data_dir, [
        {"id": "up", "name": "Up", "pin": 17, "mode": "toggle", "interlock_group": "bed"},
        {"id": "down", "name": "Down", "pin": 27, "mode": "toggle", "interlock_group": "bed", "active_high": False},
    ])
    try:
        m.turn_on("up")
        t0 = time.monotonic()
        m.turn_on("down")
        assert time.monotonic() - t0 >= 0.14  # dead time honoured
        assert not state(m, "up")["on"] and state(m, "down")["on"]
        # active-low device drives the pin LOW when on
        assert m._switches["down"].device.pin.state == 0
        m.turn_off("down")
        assert m._switches["down"].device.pin.state == 1
    finally:
        m.stop()


def test_max_on_time_and_pulse(data_dir):
    _, m = make_manager(data_dir, [
        {"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle", "max_on_seconds": 0.4},
        {"id": "tap", "name": "Tap", "pin": 23, "mode": "pulse", "pulse_ms": 120},
    ])
    try:
        assert m.turn_on("lamp")["on"]
        time.sleep(0.7)
        assert state(m, "lamp")["last_off_reason"] == "max-on-time"
        assert m.pulse("tap")["on"]
        time.sleep(0.3)
        assert not state(m, "tap")["on"]
    finally:
        m.stop()


def test_release_with_stale_token_and_all_off(data_dir):
    _, m = make_manager(data_dir, [
        {"id": "up", "name": "Up", "pin": 17, "mode": "momentary"},
        {"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"},
    ])
    try:
        m.press("up")
        m.release("up", "stale-token")
        assert not state(m, "up")["on"]
        m.turn_on("lamp")
        m.all_off()
        assert not any(s["on"] for s in m.snapshot()["switches"])
    finally:
        m.stop()


def test_config_change_removes_and_turns_off(data_dir):
    store, m = make_manager(data_dir, [{"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"}])
    try:
        m.turn_on("lamp")
        dev = m._switches["lamp"].device
        store.update(lambda c: c["gpio"].update({"switches": []}))
        assert "lamp" not in m._switches
        assert dev.closed
    finally:
        m.stop()


def test_stop_releases_everything(data_dir):
    _, m = make_manager(data_dir, [{"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"}])
    m.turn_on("lamp")
    dev = m._switches["lamp"].device
    m.stop()
    assert dev.closed
    assert m.snapshot()["switches"] == []


def test_pin_map_marks_usage(data_dir):
    _, m = make_manager(data_dir, [{"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"}])
    try:
        pins = {p["bcm"]: p for p in m.pin_map()}
        assert pins[22]["in_use_by"] == "Lamp"
        assert pins[0]["reserved"] and pins[17]["recommended"]
        assert pins[4]["boot_pull"] == "up" and pins[17]["boot_pull"] == "down"
    finally:
        m.stop()


def test_interlock_enforced_after_reconfigure(data_dir):
    store, m = make_manager(data_dir, [
        {"id": "a", "name": "A", "pin": 17, "mode": "toggle"},
        {"id": "b", "name": "B", "pin": 27, "mode": "toggle"},
    ])
    try:
        m.turn_on("a")
        m.turn_on("b")
        assert state(m, "a")["on"] and state(m, "b")["on"]
        # Putting both into one group while both are on must not leave both energised.
        store.update(lambda c: c["gpio"].update({"switches": [
            {"id": "a", "name": "A", "pin": 17, "mode": "toggle", "interlock_group": "g"},
            {"id": "b", "name": "B", "pin": 27, "mode": "toggle", "interlock_group": "g"},
        ]}))
        assert not (state(m, "a")["on"] and state(m, "b")["on"])
    finally:
        m.stop()


def test_watchdog_forces_off_when_hardware_disagrees(data_dir):
    _, m = make_manager(data_dir, [{"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"}])
    try:
        dev = m._switches["lamp"].device
        dev.on()  # simulate a stray write behind the manager's back
        assert dev.value == 1
        time.sleep(0.3)
        assert dev.value == 0 and not state(m, "lamp")["on"]
    finally:
        m.stop()


def test_dead_time_does_not_block_other_switches(data_dir):
    _, m = make_manager(data_dir, [
        {"id": "up", "name": "Up", "pin": 17, "mode": "toggle", "interlock_group": "bed"},
        {"id": "down", "name": "Down", "pin": 27, "mode": "toggle", "interlock_group": "bed"},
        {"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"},
    ], interlock_dead_time_ms=600)
    try:
        m.turn_on("up")
        import threading
        t = threading.Thread(target=m.turn_on, args=("down",))
        t0 = time.monotonic()
        t.start()
        time.sleep(0.1)
        m.turn_on("lamp")  # must not wait for the bed dead time
        assert time.monotonic() - t0 < 0.4
        t.join(timeout=3)
        assert state(m, "down")["on"] and not state(m, "up")["on"] and state(m, "lamp")["on"]
    finally:
        m.stop()


@pytest.mark.parametrize("call", ["toggle", "turn_on"])
def test_dead_time_never_holds_the_lock(data_dir, call):
    """Waiting out a dead time must not freeze the rest of the manager.

    ``toggle`` and ``turn_on`` used to reach ``_energise`` from inside the lock,
    so a single interlocked press stalled every release, ``all_off`` and the
    watchdog for the whole dead time.
    """
    import threading

    _, m = make_manager(data_dir, [
        {"id": "up", "name": "Up", "pin": 17, "mode": "toggle", "interlock_group": "bed"},
        {"id": "down", "name": "Down", "pin": 27, "mode": "toggle", "interlock_group": "bed"},
        {"id": "lamp", "name": "Lamp", "pin": 22, "mode": "toggle"},
    ], interlock_dead_time_ms=800)
    try:
        m.turn_on("up")
        m.turn_on("lamp")
        waiting = threading.Thread(target=getattr(m, call), args=("down",))
        waiting.start()
        time.sleep(0.15)  # "down" is now parked in the dead time
        t0 = time.monotonic()
        m.all_off("panic")          # safety paths must never queue behind it
        m.snapshot()
        assert time.monotonic() - t0 < 0.3
        assert not state(m, "lamp")["on"]
        waiting.join(timeout=5)
        assert not waiting.is_alive()
    finally:
        m.stop()


def test_pulse_switch_reached_through_turn_on_still_pulses(data_dir):
    _, m = make_manager(data_dir, [
        {"id": "tap", "name": "Tap", "pin": 23, "mode": "pulse", "pulse_ms": 80, "interlock_group": "bed"},
    ])
    try:
        assert m.turn_on("tap")["on"]
        time.sleep(0.3)
        assert not state(m, "tap")["on"]
    finally:
        m.stop()


def test_pin_test_releases_its_pin(data_dir):
    _, m = make_manager(data_dir, [])
    try:
        m.test_pin(23, True, 50)
        assert not m._test_pins
    finally:
        m.stop()


def test_watchdog_reclaims_an_abandoned_pin_test(data_dir):
    """If the request thread dies mid-pulse, the pin must not keep driving a relay."""
    from gpiozero import OutputDevice

    from jimboled.gpio.manager import _TestPin

    _, m = make_manager(data_dir, [])
    try:
        dev = OutputDevice(24, active_high=True, initial_value=False, pin_factory=m._factory)
        with m._lock:
            m._test_pins[24] = _TestPin(device=dev, expires_at=time.monotonic() - 1)
        dev.on()
        time.sleep(0.3)
        assert 24 not in m._test_pins and dev.closed
    finally:
        m.stop()


def test_lenient_booleans():
    assert SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 17, "active_high": "false"}).active_high is False
    assert SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 17, "active_high": "1"}).active_high is True
    with pytest.raises(GPIOError):
        SwitchConfig.from_dict({"id": "a", "name": "A", "pin": 17, "active_high": "maybe"})
