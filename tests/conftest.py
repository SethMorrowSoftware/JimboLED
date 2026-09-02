import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("GPIOZERO_PIN_FACTORY", "mock")

from tests.fake_wled import FakeWLEDServer  # noqa: E402


@pytest.fixture
def fake_wled():
    servers = []

    def _make(name="Fake WLED", leds=60, mac="aabbccddeeff"):
        s = FakeWLEDServer(name, leds, mac).start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.stop()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def app(data_dir):
    from jimboled import create_app

    application = create_app(str(data_dir), testing=True)
    ctx = application.extensions["jimboled"]
    ctx.store.update(lambda c: c["wled"].update({"poll_interval_s": 1, "offline_poll_interval_s": 3, "request_timeout_s": 2}))
    yield application
    ctx.gpio.stop()
    ctx.devices.stop()
    ctx.discovery.listener.stop()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "JimboLED"
    return c
