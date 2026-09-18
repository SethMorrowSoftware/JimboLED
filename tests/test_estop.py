"""Emergency stop: latching, scoping, persistence and hardware inputs."""
import time

import pytest

from jimboled.config import ConfigStore
from jimboled.gpio.common import EStopEngaged, GPIOError
from jimboled.gpio.estop import MASTER_ZONE_ID, EStopInput, EStopZone, validate_inputs, validate_zones
from jimboled.gpio.manager import GPIOManager

BED = {"id": "up", "name": "Bed up", "pin": 17, "mode": "toggle", "interlock_group": "bed"}
AWNING = {"id": "awn", "name": "Awning out", "pin": 22, "mode": "toggle", "interlock_group": "awning"}


def make_manager(data_dir, switches, estop=None, **gpio):
    store = ConfigStore(data_dir)
    cfg = {"backend": "mock", "hold_timeout_s": 0.3, "interlock_dead_time_ms": 50,
           "switches": switches, "estop": estop or {}}
    cfg.update(gpio)
    store.update(lambda c: c["gpio"].update(cfg))
    m = GPIOManager(store)
    m.start()
    return store, m


def state(m, sid):
    return next(s for s in m.snapshot()["switches"] if s["id"] == sid)


# ------------------------------------------------------------------ config
def test_zone_validation():
    with pytest.raises(GPIOError):  # the master stop is built in
        EStopZone.from_dict({"id": MASTER_ZONE_ID, "name": "Nope", "scope": "all"})
    with pytest.raises(GPIOError):  # a scoped stop must cover something
        EStopZone.from_dict({"id": "z", "name": "Z", "scope": "switch", "refs": []})
    with pytest.raises(GPIOError):
        EStopZone.from_dict({"id": "z", "name": "Z", "scope": "everything"})
    with pytest.raises(GPIOError):
        validate_zones([{"id": "z", "name": "A", "scope": "switch", "refs": ["x"]},
                        {"id": "z", "name": "B", "scope": "switch", "refs": ["y"]}])


def test_input_validation_rejects_pin_clashes():
    with pytest.raises(GPIOError):
        validate_inputs([{"id": "i", "name": "Stop", "pin": 17}], taken_pins={17: "Bed up"})
    with pytest.raises(GPIOError):
        validate_inputs([{"id": "i", "name": "Stop", "pin": 0}])
    with pytest.raises(GPIOError):
        validate_inputs([{"id": "i", "name": "Stop", "pin": 26, "pull": "sideways"}])
    # A normally-closed loop reads "1" when healthy, whichever pull is used.
    assert EStopInput.from_dict({"id": "i", "name": "S", "pin": 26}).healthy_value == 1
    assert EStopInput.from_dict({"id": "i", "name": "S", "pin": 26, "normally_closed": False}).healthy_value == 0


# ----------------------------------------------------------------- latching
def test_master_stop_blocks_every_switch(data_dir):
    _, m = make_manager(data_dir, [BED, AWNING])
    try:
        m.turn_on("up")
        m.turn_on("awn")
        snap = m.engage_estop(MASTER_ZONE_ID, reason="test")
        assert sorted(snap["stopped"]) == ["Awning out", "Bed up"]
        assert not state(m, "up")["on"] and not state(m, "awn")["on"]
        assert state(m, "up")["locked_out"] and state(m, "awn")["locked_out"]
        for sid in ("up", "awn"):
            with pytest.raises(EStopEngaged):
                m.turn_on(sid)
            with pytest.raises(EStopEngaged):
                m.pulse(sid, 50)
            with pytest.raises(EStopEngaged):
                m.toggle(sid)
        m.reset_estop(MASTER_ZONE_ID)
        assert m.turn_on("up")["on"]
    finally:
        m.stop()


def test_zone_stop_only_locks_its_own_relays(data_dir):
    _, m = make_manager(data_dir, [BED, AWNING],
                        estop={"zones": [{"id": "bed", "name": "Bed", "scope": "group", "refs": ["bed"]}]})
    try:
        m.turn_on("up")
        m.turn_on("awn")
        m.engage_estop("bed", reason="motor stuck")
        assert not state(m, "up")["on"]
        assert state(m, "awn")["on"], "an awning stop must not lock out the bed and vice versa"
        with pytest.raises(EStopEngaged):
            m.turn_on("up")
        m.turn_on("awn")  # still fine
        m.reset_estop("bed")
        assert m.turn_on("up")["on"]
    finally:
        m.stop()


def test_switch_scoped_zone(data_dir):
    _, m = make_manager(data_dir, [BED, AWNING],
                        estop={"zones": [{"id": "one", "name": "Just the bed", "scope": "switch", "refs": ["up"]}]})
    try:
        m.engage_estop("one")
        with pytest.raises(EStopEngaged):
            m.turn_on("up")
        assert m.turn_on("awn")["on"]
    finally:
        m.stop()


def test_turning_off_is_never_blocked(data_dir):
    """Safety only ever runs one way: a stop must not stand between you and off."""
    _, m = make_manager(data_dir, [BED, AWNING])
    try:
        m.turn_on("up")
        m.turn_on("awn")
        m.engage_estop(MASTER_ZONE_ID)
        m.turn_off("up")
        m.release("up")
        m.all_off()
        assert not any(s["on"] for s in m.snapshot()["switches"])
    finally:
        m.stop()


def test_hold_to_run_is_blocked_while_latched(data_dir):
    _, m = make_manager(data_dir, [{"id": "up", "name": "Bed up", "pin": 17, "mode": "momentary"}])
    try:
        m.engage_estop(MASTER_ZONE_ID)
        with pytest.raises(EStopEngaged):
            m.press("up")
    finally:
        m.stop()


def test_engaging_mid_hold_releases_the_relay(data_dir):
    _, m = make_manager(data_dir, [{"id": "up", "name": "Bed up", "pin": 17, "mode": "momentary"}])
    try:
        held = m.press("up")
        assert held["on"]
        m.engage_estop(MASTER_ZONE_ID, reason="panic")
        assert not state(m, "up")["on"]
        # The browser's next heartbeat must learn the hold is over, not error out.
        assert m.heartbeat("up", held["token"])["held"] is False
    finally:
        m.stop()


def test_latch_survives_a_restart(data_dir):
    store, m = make_manager(data_dir, [BED])
    try:
        m.engage_estop(MASTER_ZONE_ID, reason="power cut test")
    finally:
        m.stop()
    m2 = GPIOManager(store)
    m2.start()
    try:
        assert m2.snapshot()["estop"]["engaged_zones"] == [MASTER_ZONE_ID]
        with pytest.raises(EStopEngaged):
            m2.turn_on("up")
        m2.reset_estop(MASTER_ZONE_ID)
        assert m2.turn_on("up")["on"]
    finally:
        m2.stop()


def test_deleting_a_zone_clears_its_latch(data_dir):
    store, m = make_manager(data_dir, [BED],
                            estop={"zones": [{"id": "bed", "name": "Bed", "scope": "group", "refs": ["bed"]}]})
    try:
        m.engage_estop("bed")
        assert m.snapshot()["estop"]["engaged"]
        store.update(lambda c: c["gpio"]["estop"].__setitem__("zones", []))
        time.sleep(0.05)
        assert not m.snapshot()["estop"]["engaged"], "a stop nobody can reset must not survive its zone"
        assert m.turn_on("up")["on"]
    finally:
        m.stop()


def test_test_pin_is_refused_while_latched(data_dir):
    _, m = make_manager(data_dir, [BED])
    try:
        m.engage_estop(MASTER_ZONE_ID)
        with pytest.raises(EStopEngaged):
            m.test_pin(17, True, 50)
        with pytest.raises(EStopEngaged):
            m.test_pin(23, True, 50)  # an unconfigured pin, still under the master stop
    finally:
        m.stop()


def test_a_stop_cuts_a_pin_test_that_is_already_running(data_dir):
    """The pin under test belongs to no switch, so nothing else can reach it."""
    import threading

    _, m = make_manager(data_dir, [BED])
    try:
        tester = threading.Thread(target=m.test_pin, args=(23, True, 1500))
        tester.start()
        deadline = time.time() + 1.0
        while 23 not in m._test_pins and time.time() < deadline:
            time.sleep(0.01)
        dev = m._test_pins[23].device
        pin = dev.pin  # keep the pin itself: releasing closes the device
        assert dev.value == 1

        result = m.engage_estop(MASTER_ZONE_ID, reason="mid-test")
        assert dev.closed and pin.state == 0
        assert 23 not in m._test_pins
        assert any("GPIO23" in name for name in result["stopped"])
        tester.join(timeout=5)
    finally:
        m.stop()


# --------------------------------------------------------- hardware inputs
def hardware_manager(data_dir, **input_cfg):
    cfg = {"id": "e1", "name": "Bedside button", "pin": 26, "zone": MASTER_ZONE_ID,
           "normally_closed": True, "pull": "up"}
    cfg.update(input_cfg)
    return make_manager(data_dir, [BED], estop={"inputs": [cfg]})


def test_hardware_button_latches_and_blocks_reset_while_held(data_dir):
    _, m = hardware_manager(data_dir)
    try:
        m.turn_on("up")
        assert not m.snapshot()["estop"]["engaged"], "a healthy closed loop must not latch"

        m.estop._devices["e1"].pin.drive_high()   # loop opened: button slapped
        time.sleep(0.5)
        assert m.snapshot()["estop"]["engaged_zones"] == [MASTER_ZONE_ID]
        assert not state(m, "up")["on"]
        with pytest.raises(GPIOError, match="release the physical emergency stop"):
            m.reset_estop(MASTER_ZONE_ID)

        m.estop._devices["e1"].pin.drive_low()    # button pulled back out
        time.sleep(0.3)
        m.reset_estop(MASTER_ZONE_ID)
        assert m.turn_on("up")["on"]
    finally:
        m.stop()


def test_reopening_inputs_reads_them_instead_of_assuming(data_dir):
    """Reopening the inputs used to blank their state until the next poll.

    In that gap ``blocking_inputs()`` said "nothing is holding this down", so a
    phone could clear a lock-out while somebody stood on the mushroom button.
    """
    store, m = hardware_manager(data_dir)
    try:
        # Any estop edit reopens every input.
        store.update(lambda c: c["gpio"]["estop"].update({"master_name": "Everything"}))
        assert m.estop._input_state["e1"]["value"] is not None, "state must come from a read, not a guess"
    finally:
        m.stop()


def test_a_button_already_held_when_the_pin_opens_still_latches(data_dir):
    """A trip found by the priming read must still be handed over once."""
    _, m = hardware_manager(data_dir)
    try:
        m.estop._devices["e1"].pin.drive_high()   # as if held from boot
        m.estop._prime_inputs()
        assert m.estop.blocking_inputs(MASTER_ZONE_ID) == ["Bedside button"]
        time.sleep(0.4)
        assert m.snapshot()["estop"]["engaged_zones"] == [MASTER_ZONE_ID]
        assert not state(m, "up")["available"] or not state(m, "up")["on"]
    finally:
        m.stop()


def test_a_broken_input_latches(data_dir):
    """A stop you cannot read is not a stop you may rely on."""
    _, m = hardware_manager(data_dir)
    try:
        time.sleep(0.3)
        assert not m.snapshot()["estop"]["engaged"]
        m.estop._devices.pop("e1").close()        # severed cable / lost device
        time.sleep(0.4)
        assert m.snapshot()["estop"]["engaged_zones"] == [MASTER_ZONE_ID]
    finally:
        m.stop()


def test_normally_open_button_latches_when_closed(data_dir):
    _, m = hardware_manager(data_dir, normally_closed=False)
    try:
        time.sleep(0.3)
        assert not m.snapshot()["estop"]["engaged"]
        m.estop._devices["e1"].pin.drive_low()    # NO button pressed -> pin to GND
        time.sleep(0.5)
        assert m.snapshot()["estop"]["engaged"]
    finally:
        m.stop()


def test_hardware_input_can_latch_one_zone_only(data_dir):
    store = ConfigStore(data_dir)
    store.update(lambda c: c["gpio"].update({
        "backend": "mock", "interlock_dead_time_ms": 50, "switches": [BED, AWNING],
        "estop": {"zones": [{"id": "bed", "name": "Bed", "scope": "group", "refs": ["bed"]}],
                  "inputs": [{"id": "e1", "name": "Bed stop", "pin": 26, "zone": "bed"}]}}))
    m = GPIOManager(store)
    m.start()
    try:
        m.turn_on("awn")
        m.estop._devices["e1"].pin.drive_high()
        time.sleep(0.5)
        assert m.snapshot()["estop"]["engaged_zones"] == ["bed"]
        assert state(m, "awn")["on"], "a bed stop must leave the awning alone"
    finally:
        m.stop()


def test_a_disabled_input_does_not_block_reset(data_dir):
    """A button switched off on purpose must not make a stop unresettable."""
    _, m = hardware_manager(data_dir, enabled=False)
    try:
        time.sleep(0.3)
        assert not m.snapshot()["estop"]["engaged"], "a disabled input must not latch"
        m.engage_estop(MASTER_ZONE_ID, reason="by hand")
        m.reset_estop(MASTER_ZONE_ID)
        assert m.turn_on("up")["on"]
    finally:
        m.stop()


def test_reset_says_what_it_cannot_read(data_dir):
    _, m = hardware_manager(data_dir)
    try:
        m.estop._devices.pop("e1").close()
        time.sleep(0.4)
        with pytest.raises(GPIOError, match="cannot read"):
            m.reset_estop(MASTER_ZONE_ID)
    finally:
        m.stop()


def test_invalid_input_config_keeps_the_working_buttons(data_dir):
    """Refusing a bad edit must not answer it by disarming the physical stop."""
    store, m = hardware_manager(data_dir)
    try:
        assert [i.name for i in m.estop.inputs] == ["Bedside button"]
        store.update(lambda c: c["gpio"]["estop"].__setitem__(
            "inputs", [{"id": "e1", "name": "Bedside button", "pin": 999}]))
        assert [i.name for i in m.estop.inputs] == ["Bedside button"]
        assert m.estop._devices.get("e1") is not None
    finally:
        m.stop()
