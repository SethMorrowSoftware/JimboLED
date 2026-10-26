"""Phone notifications (ntfy / webhook)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jimboled.config import ConfigStore
from jimboled.notify import OFFLINE_AFTER_S, Notifier, clean_settings


@pytest.fixture
def hook():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook", got
    srv.shutdown()


def test_settings_validation():
    assert clean_settings({"url": "", "events": ["estop", "bogus"]}) == {"url": "", "events": ["estop"]}
    with pytest.raises(ValueError):
        clean_settings({"url": "ftp://x"})
    assert clean_settings({"url": "https://ntfy.sh/abc"})["events"] == ["estop", "relay", "schedule", "offline"]


def test_events_are_filtered_rate_limited_and_delivered(tmp_path, hook):
    url, got = hook
    store = ConfigStore(tmp_path)
    store.update(lambda c: c.__setitem__("notify", {"url": url, "events": ["relay", "estop"]}))
    n = Notifier(store)
    n.start()
    try:
        n.gpio_event({"action": "off", "name": "Bed up", "reason": "max-on-time"})
        n.gpio_event({"action": "off", "name": "Bed up", "reason": "max-on-time"})  # repeat: dropped
        n.gpio_event({"action": "off", "name": "Lamp", "reason": "manual"})       # not interesting
        n.schedule_result({"id": "s", "name": "Night"}, {"error": "boom"})          # event not chosen
        n.gpio_event({"action": "estop-engaged", "name": "Bed", "reason": "button pressed"})
        deadline = time.time() + 5
        while len(got) < 2 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
    finally:
        n.stop()
    assert len(got) == 2
    titles = sorted(json.loads(g["body"])["title"] for g in got)
    assert titles == ["Bed up switched off", "Emergency stop: Bed"]


def test_offline_lights_are_announced_once_and_on_return(tmp_path):
    class Devices:
        online = False

        def snapshot(self):
            return [{"id": "d", "name": "Porch", "host": "1.2.3.4", "enabled": True, "online": self.online,
                     "last_seen": 1.0, "last_error": "timeout"}]

    store = ConfigStore(tmp_path)
    store.update(lambda c: c.__setitem__("notify", {"url": "https://example.invalid/x", "events": ["offline"]}))
    devs = Devices()
    n = Notifier(store, devs)
    n.check_offline(now=1000.0)
    n.check_offline(now=1000.0 + OFFLINE_AFTER_S + 1)
    n.check_offline(now=1000.0 + OFFLINE_AFTER_S + 60)
    devs.online = True
    n.check_offline(now=1000.0 + OFFLINE_AFTER_S + 120)
    titles = []
    while not n._q.empty():
        titles.append(n._q.get_nowait()["title"])
    assert titles == ["Porch is offline", "Porch is back"]


def test_notify_api(client, app, hook):
    url, got = hook
    assert client.put("/api/notify", json={"url": "nope"}).status_code == 400
    assert client.post("/api/notify/test").status_code == 400  # no address yet
    assert client.put("/api/notify", json={"url": url, "events": ["estop"]}).status_code == 200
    r = client.post("/api/notify/test")
    assert r.status_code == 200 and got and json.loads(got[-1]["body"])["title"] == "JimboLED test"
    token = client.post("/api/tokens", json={"name": "x"}).json["token"]
    assert app.test_client().get("/api/notify", headers={"Authorization": f"Bearer {token}"}).status_code == 403
