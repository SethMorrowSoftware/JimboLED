"""End-to-end tests: the real dashboard in a real (headless) Chromium.

These drive the single-page app the way a person does – against a real
JimboLED server and a fake WLED controller – and fail on any JavaScript
error, any failed API call and any page that scrolls sideways on a phone.

They need Playwright (``pip install playwright && playwright install
chromium``) and are skipped without it.  ``JIMBOLED_CHROMIUM`` can point at
an already-installed Chromium binary instead.
"""
from __future__ import annotations

import base64
import os
import socket
import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

# 1x1 PNG; the logo test just needs a real picture.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

SETTINGS_SECTIONS = ("home", "devices", "groups", "switches", "scenes", "schedules", "estop", "appearance",
                     "security", "integrations", "system", "backup")
H = {"X-Requested-With": "JimboLED"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A real JimboLED on a free port, served by waitress like on the Pi."""

    def __init__(self, data_dir):
        from waitress.server import create_server

        from jimboled import create_app

        self.app = create_app(str(data_dir))
        self.ctx = self.app.extensions["jimboled"]
        self.ctx.store.update(lambda c: c["wled"].update({"poll_interval_s": 1, "offline_poll_interval_s": 3,
                                                          "request_timeout_s": 2}))
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._srv = create_server(self.app, host="127.0.0.1", port=self.port, threads=8)
        self._thread = threading.Thread(target=self._srv.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._srv.close()
        for svc in ("scheduler", "notifier", "gpio", "devices", "discovery"):
            try:
                getattr(self.ctx, svc).stop()
            except Exception:
                pass


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        exe = os.environ.get("JIMBOLED_CHROMIUM") or None
        try:
            b = p.chromium.launch(executable_path=exe)
        except Exception as exc:  # browser not installed
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def fake():
    from tests.fake_wled import FakeWLEDServer

    s = FakeWLEDServer("Desk strip", 60, "aabbcc000001").start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def server(tmp_path_factory, fake):
    """A set-up JimboLED: one controller, two relays, a scene, a group, a schedule."""
    import requests

    srv = Server(tmp_path_factory.mktemp("e2e") / "data")
    api = requests.Session()
    api.headers.update(H)
    base = srv.url

    def post(path, payload):
        r = api.post(base + path, json=payload, timeout=10)
        assert r.status_code in (200, 201), (path, r.text)
        return r.json()

    dev = post("/api/devices", {"host": fake.host, "name": "Desk strip"})["device"]
    up = post("/api/gpio/switches", {"name": "Bed up", "pin": 17, "mode": "momentary", "interlock_group": "bed"})
    post("/api/gpio/switches", {"name": "Fan", "pin": 22, "mode": "toggle"})
    post("/api/scenes", {"name": "Goodnight", "actions": [{"type": "all", "state": {"on": False}}]})
    post("/api/groups", {"name": "Bedroom", "devices": [dev["id"]]})
    post("/api/schedules", {"name": "Night", "when": {"type": "time", "time": "23:30"}, "days": [0, 1, 2, 3, 4],
                            "action": {"type": "all_off"}})
    api.put(base + "/api/settings", json={"setup_complete": True}, timeout=10)
    srv.device_id = dev["id"]
    srv.switch = up
    srv.api = api
    # let the first poll of the controller land
    deadline = time.time() + 10
    while time.time() < deadline:
        devs = api.get(base + "/api/state", timeout=5).json()["devices"]
        if devs and devs[0].get("online"):
            break
        time.sleep(0.2)
    yield srv
    srv.stop()


class Page:
    """A browser page that records every JavaScript error and failed API call."""

    def __init__(self, browser, base, **ctx_kwargs):
        self.context = browser.new_context(**ctx_kwargs)
        self.page = self.context.new_page()
        self.base = base
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(f"JS error: {e}"))
        self.page.on("console", lambda m: m.type == "error" and self.errors.append(f"console: {m.text}"))

        def on_response(resp):
            if "/api/" in resp.url and resp.status >= 500:
                self.errors.append(f"{resp.status} from {resp.url}")
        self.page.on("response", on_response)

    def go(self, hash_="#/", settle=600):
        self.page.goto(self.base + "/" + hash_)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(settle)
        return self.page

    def assert_clean(self):
        assert not self.errors, "\n".join(self.errors)

    def no_sideways_scroll(self):
        overflow = self.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"page scrolls sideways by {overflow}px"

    def close(self):
        self.context.close()


@pytest.fixture
def desktop(browser, server):
    p = Page(browser, server.url, viewport={"width": 1280, "height": 900})
    yield p
    p.close()


@pytest.fixture
def phone(browser, server):
    p = Page(browser, server.url, viewport={"width": 375, "height": 740}, is_mobile=True, has_touch=True)
    yield p
    p.close()


# ----------------------------------------------------------------- tests
def test_dashboard_shows_every_tile(desktop, server):
    page = desktop.go()
    page.wait_for_selector(".tile")
    text = page.inner_text("#main")
    for name in ("Desk strip", "Bed up", "Fan", "Goodnight", "Bedroom"):
        assert name in text, name
    assert page.inner_text("#topbar-status").strip().startswith("1/1")
    desktop.assert_clean()


@pytest.mark.parametrize("section", SETTINGS_SECTIONS)
def test_every_settings_page_renders(desktop, section):
    page = desktop.go(f"#/settings/{section}")
    # Every page opens with a plain-language explanation of what it is for.
    page.wait_for_selector(".settings-intro")
    desktop.assert_clean()


@pytest.mark.parametrize("section", SETTINGS_SECTIONS)
def test_every_settings_page_fits_a_phone(phone, section):
    phone.go(f"#/settings/{section}")
    phone.page.wait_for_selector(".settings-intro")
    phone.no_sideways_scroll()
    phone.assert_clean()


def test_dashboard_fits_a_phone(phone):
    page = phone.go()
    page.wait_for_selector(".tile")
    phone.no_sideways_scroll()
    phone.assert_clean()


def test_power_toggle_reaches_the_controller(desktop, server, fake):
    page = desktop.go()
    tile = page.locator(".tile", has_text="Desk strip").first
    before = fake.fake["state"]["on"] if hasattr(fake, "fake") and isinstance(fake.fake, dict) else None
    tile.locator(".toggle").first.click()
    page.wait_for_timeout(800)
    after = server.api.get(server.url + "/api/state", timeout=5).json()["devices"][0]["state"]["on"]
    if before is not None:
        assert after != before
    tile.locator(".toggle").first.click()
    page.wait_for_timeout(800)
    again = server.api.get(server.url + "/api/state", timeout=5).json()["devices"][0]["state"]["on"]
    assert again != after
    desktop.assert_clean()


def test_controller_panel_and_all_its_tabs(desktop, server):
    page = desktop.go(f"#/device/{server.device_id}", settle=1200)
    page.wait_for_selector(".modal")
    tabs = page.locator(".modal .tab")
    count = tabs.count()
    assert count >= 3
    for i in range(count):
        tabs.nth(i).click()
        page.wait_for_timeout(300)
    desktop.assert_clean()


def test_lights_panel_opens(desktop):
    page = desktop.go()
    page.get_by_role("button", name="Lights").first.click()
    page.wait_for_selector(".modal")
    assert "Desk strip" in page.inner_text(".modal") or "light" in page.inner_text(".modal").lower()
    page.keyboard.press("Escape")
    desktop.assert_clean()


def test_edit_layout_mode(desktop):
    page = desktop.go()
    page.get_by_role("button", name="Edit layout").click()
    page.wait_for_timeout(300)
    assert page.locator(".editing").count() >= 1
    page.get_by_role("button", name="Done").click()
    page.wait_for_timeout(300)
    assert page.locator(".editing").count() == 0
    desktop.assert_clean()


def _switch(server, name):
    return [s for s in server.api.get(server.url + "/api/state", timeout=5).json()["gpio"]["switches"]
            if s["name"] == name][0]


def test_toggle_switch_tile(desktop, server):
    page = desktop.go()
    btn = page.locator(".tile", has_text="Fan").locator(".hold-btn").first
    btn.click()
    page.wait_for_timeout(600)
    assert _switch(server, "Fan")["on"] is True
    btn.click()
    page.wait_for_timeout(600)
    assert _switch(server, "Fan")["on"] is False
    desktop.assert_clean()


def test_hold_to_run_only_runs_while_held(desktop, server):
    page = desktop.go()
    btn = page.locator(".tile", has_text="Bed up").locator(".hold-btn").first
    box = btn.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(900)
    assert _switch(server, "Bed up")["on"] is True
    page.mouse.up()
    page.wait_for_timeout(700)
    assert _switch(server, "Bed up")["on"] is False
    desktop.assert_clean()


def test_appearance_preview_save_and_branding(desktop, server, tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(PNG)
    page = desktop.go("#/settings/appearance")
    page.locator("input[type=file]").first.set_input_files(str(logo))
    page.wait_for_selector("#brand-logo:not([hidden])")
    assert "/branding/logo?v=" in page.get_attribute("#favicon", "href")
    # previews live …
    page.get_by_role("button", name="Paper", exact=True).click()
    assert page.evaluate("document.documentElement.dataset.theme") == "paper"
    page.get_by_role("button", name="Serif").click()
    page.get_by_role("button", name="Glass (see-through)").click()
    assert page.evaluate("document.documentElement.dataset.font") == "serif"
    # … is undone by leaving without saving …
    page.goto(server.url + "/#/")
    page.wait_for_timeout(300)
    assert page.evaluate("document.documentElement.dataset.theme") == "midnight"
    # … and kept by saving.
    page = desktop.go("#/settings/appearance")
    page.get_by_role("button", name="Serif").click()
    page.locator("input[placeholder^='e.g. Questions']").fill("Ask Seth")
    page.get_by_role("button", name="Save appearance").click()
    page.wait_for_timeout(800)
    dash = server.api.get(server.url + "/api/dashboard", timeout=5).json()["dashboard"]
    assert dash["font"] == "serif" and dash["footer_text"] == "Ask Seth"
    page = desktop.go("#/")
    assert "Ask Seth" in page.inner_text("#footer")
    assert page.evaluate("document.documentElement.dataset.font") == "serif"
    # Reset puts the look back but keeps the logo.
    page = desktop.go("#/settings/appearance")
    page.get_by_role("button", name="Reset to defaults").click()
    page.locator(".modal .btn.danger, .modal .btn.primary").last.click()
    page.wait_for_timeout(800)
    dash = server.api.get(server.url + "/api/dashboard", timeout=5).json()
    assert dash["dashboard"]["font"] == "system" and dash["branding"]["logo"] is not None
    server.api.delete(server.url + "/api/branding/logo", timeout=5)
    server.api.put(server.url + "/api/dashboard", json={"footer_text": ""}, timeout=5)
    desktop.assert_clean()


def test_quick_looks_and_logo_colours(desktop, server):
    import io
    import struct
    import zlib

    # A 4x4 PNG in one strong colour, so the logo palette is predictable.
    def png(rgb):
        raw = b"".join(b"\x00" + bytes(rgb) * 4 for _ in range(4))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    r = server.api.post(server.url + "/api/branding/logo", files={"file": ("l.png", io.BytesIO(png((230, 40, 90))), "image/png")},
                        timeout=10)
    assert r.status_code == 201, r.text
    try:
        page = desktop.go("#/settings/appearance", settle=1000)
        swatch = page.locator(".logo-colors .swatch").first
        swatch.wait_for()
        colour = swatch.get_attribute("data-color")
        r_, g_, b_ = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        assert abs(r_ - 230) < 12 and abs(g_ - 40) < 12 and abs(b_ - 90) < 12, colour
        swatch.click()
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent')").strip() == colour
        # A quick look changes several settings at once, all previewed …
        page.locator(".look", has_text="Neon night").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "oled"
        assert page.evaluate("document.documentElement.dataset.surface") == "glass"
        assert page.locator(".chip.active", has_text="Condensed").count() == 1
        # … and saved with one button.
        page.get_by_role("button", name="Save appearance").click()
        page.wait_for_timeout(800)
        dash = server.api.get(server.url + "/api/dashboard", timeout=5).json()["dashboard"]
        assert (dash["theme"], dash["accent"], dash["font"], dash["surface"], dash["radius"]) == \
            ("oled", "#22d3ee", "condensed", "glass", "sharp")
        # Photo frame needs a background picture first.
        page.locator(".look", has_text="Photo frame").click()
        assert page.evaluate("document.documentElement.dataset.backdrop") != "image"
        desktop.assert_clean()
    finally:
        server.api.post(server.url + "/api/dashboard/appearance/reset", json={"pictures": True}, timeout=5)


def test_wall_tablet_mode(desktop, server):
    page = desktop.go("#/?kiosk=1", settle=1000)
    page.wait_for_selector(".tile")
    assert page.evaluate("document.documentElement.hasAttribute('data-kiosk')")
    assert page.get_by_role("button", name="Edit layout").count() == 0
    assert page.locator('a[href="#/settings"]').count() == 0
    assert page.locator("#pill-clock").is_visible()
    # Safety controls stay.
    assert page.get_by_role("button", name="All off").count() == 1
    assert page.locator(".estop-btn").count() == 1
    # Settings can't be reached, even by typing the address.
    page.goto(server.url + "/#/settings/security")
    page.wait_for_timeout(500)
    assert page.url.endswith("#/") and page.locator(".settings-nav").count() == 0
    # The controller panel has no Device tab and no reboot/remove.
    page.goto(server.url + f"/#/device/{server.device_id}")
    page.wait_for_selector(".modal .tab")
    tabs = [t.strip() for t in page.locator(".modal .tab").all_inner_texts()]
    assert "Device" not in tabs and "Control" in tabs
    page.locator(".modal [aria-label=More]").click()
    menu = page.locator(".menu").inner_text()
    assert "Reboot" not in menu and "Remove" not in menu
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    # It survives a reload (it is this screen's setting) …
    page.reload()
    page.wait_for_selector(".tile")
    assert page.evaluate("document.documentElement.hasAttribute('data-kiosk')")
    # … and a 3-second press on the logo, confirmed, leaves it.
    box = page.locator("#brand").bounding_box()
    page.mouse.move(box["x"] + 12, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(3300)
    page.mouse.up()
    page.locator(".modal .btn.primary").last.click()
    page.wait_for_timeout(400)
    assert not page.evaluate("document.documentElement.hasAttribute('data-kiosk')")
    assert page.get_by_role("button", name="Edit layout").count() == 1
    desktop.assert_clean()


def test_splash_screen_shows_then_gets_out_of_the_way(desktop, server):
    page = desktop.page
    page.goto(server.url + "/")
    assert page.locator("#splash .splash-title").inner_text() == page.locator("#brand-title").inner_text()
    page.wait_for_selector("#splash", state="detached", timeout=8000)
    page.wait_for_selector(".tile")
    desktop.assert_clean()


def test_wall_tablet_screensaver(desktop, server):
    page = desktop.go("#/?kiosk=1&idle=2", settle=500)
    page.wait_for_selector("#screensaver", timeout=6000)
    assert ":" in page.locator(".saver-time").inner_text()
    assert page.locator(".saver-title").inner_text().strip()
    # The waking tap must not also press whatever is underneath.
    before = _switch(server, "Fan")["on"]
    box = page.locator(".tile", has_text="Fan").locator(".hold-btn").first.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_selector("#screensaver", state="detached")
    page.wait_for_timeout(500)
    assert _switch(server, "Fan")["on"] == before
    # Touching the dashboard keeps it away; leaving it alone brings it back.
    page.wait_for_selector("#screensaver", timeout=6000)
    page.mouse.click(5, 300)
    # Not on a normal (non-kiosk) screen.
    p2 = desktop.context.new_page()
    p2.goto(server.url + "/#/?kiosk=0")
    p2.wait_for_timeout(3500)
    assert p2.locator("#screensaver").count() == 0
    p2.close()
    desktop.assert_clean()


def test_emergency_stop_engage_and_reset(desktop, server):
    page = desktop.go()
    page.locator(".estop-btn").first.click()
    page.wait_for_timeout(400)
    if page.locator(".modal").count():  # a zone picker or confirmation
        page.locator(".modal .btn.danger").first.click()
        page.wait_for_timeout(400)
    st = server.api.get(server.url + "/api/estop", timeout=5).json()
    engaged = [z for z in st["zones"] if z.get("engaged")]
    assert engaged, st
    # Latched: nothing can switch a relay on, from anywhere.
    fan = _switch(server, "Fan")
    r = server.api.post(f"{server.url}/api/gpio/switches/{fan['id']}/action", json={"action": "on"}, timeout=5)
    assert r.status_code == 409 and r.json()["estop"] is True
    # The dashboard says so in the header.
    page.wait_for_selector("#pill-estop")
    for z in engaged:
        assert server.api.post(server.url + "/api/estop/reset", json={"zone": z["id"]}, timeout=5).status_code == 200
    st = server.api.get(server.url + "/api/estop", timeout=5).json()
    assert not any(z.get("engaged") for z in st["zones"])
    page.wait_for_selector("#pill-estop", state="detached")
    desktop.assert_clean()


def test_login_page_and_sign_in(browser, server):
    from werkzeug.security import generate_password_hash

    server.ctx.store.update(lambda c: c["server"].__setitem__("password_hash", generate_password_hash("secret")))
    try:
        p = Page(browser, server.url, viewport={"width": 390, "height": 740})
        page = p.go()
        assert "/login" in page.url
        page.fill("input[name=password]", "wrong")
        page.click("button[type=submit]")
        assert "Wrong password" in page.inner_text("body")
        page.fill("input[name=password]", "secret")
        page.click("button[type=submit]")
        page.wait_for_selector(".tile")
        p.no_sideways_scroll()
        # the 401 from the wrong password is expected; nothing else may fail
        p.errors = [e for e in p.errors if "401" not in e]
        p.assert_clean()
        p.close()
    finally:
        server.ctx.store.update(lambda c: c["server"].__setitem__("password_hash", None))


def test_first_run_wizard(browser, tmp_path):
    srv = Server(tmp_path / "fresh")
    try:
        p = Page(browser, srv.url, viewport={"width": 1024, "height": 800})
        page = p.go(settle=1000)
        page.wait_for_selector(".modal")
        assert "Welcome" in page.inner_text(".modal")
        p.assert_clean()
        p.close()
    finally:
        srv.stop()


# ------------------------------------------------- relay buttons, the easy way
def _switches_named(server, *names):
    return [s for s in server.api.get(server.url + "/api/gpio/switches", timeout=5).json()["switches"] if s["name"] in names]


def _remove_switches(server, *names):
    for s in _switches_named(server, *names):
        server.api.delete(f"{server.url}/api/gpio/switches/{s['id']}", timeout=5)


def test_settings_opens_on_an_overview_of_every_page(desktop):
    page = desktop.go("#/settings")
    page.wait_for_selector(".settings-home")
    links = page.locator(".settings-home a")
    assert links.count() == 11
    page.locator(".settings-home a", has_text="Relays & switches").click()
    page.wait_for_selector("text=Dashboard buttons")
    assert page.url.endswith("#/settings/switches")
    desktop.assert_clean()


def test_a_lone_bed_up_offers_its_down_button(desktop, server):
    """The setup that started this: Bed up on the dashboard, no Bed down."""
    try:
        page = desktop.go("#/settings/switches")
        page.get_by_role("button", name="Add Bed down").click()
        modal = page.locator(".modal")
        modal.wait_for()
        # Pre-filled as the other half of the motor: name, hold-to-run, same group.
        assert modal.locator(".choice.active").inner_text().startswith("Move a motor")
        assert modal.get_by_label("Name on the button").input_value() == "Bed down"
        modal.locator("details.more-options summary").click()
        assert modal.get_by_label("Never runs at the same time as").input_value() == "bed"
        modal.get_by_label("GPIO pin", exact=True).select_option("27")
        modal.get_by_role("button", name="Add button").click()
        page.wait_for_selector(".modal", state="detached")
        (down,) = _switches_named(server, "Bed down")
        (up,) = _switches_named(server, "Bed up")
        assert down["mode"] == "momentary" and down["pin"] == 27
        assert down["interlock_group"] == up["interlock_group"] == "bed"
        assert down["active_high"] == up["active_high"] and down["icon"] == "down"
        # The suggestion goes away once the pair is complete …
        page.wait_for_timeout(400)
        assert page.get_by_role("button", name="Add Bed down").count() == 0
        assert "never together with Bed down" in page.inner_text(".settings-body")
        # … and Bed down is on the dashboard, as a hold-to-run tile.
        page = desktop.go("#/")
        page.wait_for_selector(".tile")
        assert "Bed down" in page.inner_text("#main")
        desktop.assert_clean()
    finally:
        _remove_switches(server, "Bed down")


def test_add_a_motor_makes_an_interlocked_pair(desktop, server):
    try:
        page = desktop.go("#/settings/switches")
        page.get_by_role("button", name="Add a motor (Up + Down)").click()
        modal = page.locator(".modal")
        modal.wait_for()
        modal.get_by_label("What does it move?").fill("Recliner")
        modal.get_by_label("The two directions").select_option(label="Raise and Lower")
        assert "Recliner raise" in modal.inner_text() and "Recliner lower" in modal.inner_text()
        # Both buttons on the same pin is caught before anything is saved.
        modal.get_by_label("GPIO pin", exact=True).select_option("5")
        modal.get_by_label("GPIO pin for the second button").select_option("5")
        modal.get_by_role("button", name="Add both buttons").click()
        page.wait_for_selector("text=different GPIO numbers")
        assert _switches_named(server, "Recliner raise", "Recliner lower") == []
        desktop.errors = [e for e in desktop.errors if "different GPIO numbers" not in e]  # shown on purpose
        modal.get_by_label("GPIO pin for the second button").select_option("6")
        modal.get_by_role("button", name="Add both buttons").click()
        page.wait_for_selector(".modal", state="detached")
        pair = {s["name"]: s for s in _switches_named(server, "Recliner raise", "Recliner lower")}
        assert set(pair) == {"Recliner raise", "Recliner lower"}
        assert {s["mode"] for s in pair.values()} == {"momentary"}
        assert {s["interlock_group"] for s in pair.values()} == {"recliner"}
        assert pair["Recliner raise"]["pin"] == 5 and pair["Recliner raise"]["icon"] == "up"
        assert pair["Recliner lower"]["pin"] == 6 and pair["Recliner lower"]["icon"] == "down"
        desktop.assert_clean()
    finally:
        _remove_switches(server, "Recliner raise", "Recliner lower")


def test_add_a_button_explains_each_kind(desktop, server):
    page = desktop.go("#/settings/switches")
    page.get_by_role("button", name="Add a button").click()
    modal = page.locator(".modal")
    modal.wait_for()
    tap_length = modal.get_by_label("How long to press it (milliseconds)")
    pair = modal.locator(".pair-check")
    save = modal.locator(".modal-foot .btn.primary, .btn.primary").last
    modal.locator(".choice", has_text="Turn something on and off").click()
    assert not tap_length.is_visible() and not pair.is_visible()
    modal.locator(".choice", has_text="Press for a moment").click()
    assert tap_length.is_visible()
    modal.locator(".choice", has_text="Move a motor").click()
    assert pair.is_visible() and pair.locator("input").is_checked()
    assert save.inner_text() == "Add both buttons"
    pair.locator("input").uncheck()
    assert save.inner_text() == "Add button"
    assert modal.get_by_label("Name on the button").is_visible()
    desktop.assert_clean()


def test_copy_a_button(desktop, server):
    try:
        page = desktop.go("#/settings/switches")
        page.get_by_role("button", name="Copy Fan").click()
        modal = page.locator(".modal")
        modal.wait_for()
        assert modal.get_by_label("Name on the button").input_value() == "Fan copy"
        modal.get_by_label("GPIO pin", exact=True).select_option("23")
        modal.get_by_role("button", name="Add button").click()
        page.wait_for_selector(".modal", state="detached")
        (copy,) = _switches_named(server, "Fan copy")
        assert copy["mode"] == "toggle" and copy["pin"] == 23
        desktop.assert_clean()
    finally:
        _remove_switches(server, "Fan copy")


def test_a_hidden_switch_can_be_put_back_on_the_dashboard(desktop, server):
    dash = server.api.get(server.url + "/api/dashboard", timeout=5).json()["dashboard"]
    fan = _switches_named(server, "Fan")[0]
    tile = next(t for t in dash["tiles"] if t["type"] == "switch" and t["ref"] == fan["id"])
    server.api.put(f"{server.url}/api/dashboard/tiles/{tile['id']}", json={"hidden": True}, timeout=5)
    try:
        page = desktop.go("#/settings/switches")
        page.get_by_role("button", name="Show on dashboard").click()
        page.wait_for_timeout(500)
        dash = server.api.get(server.url + "/api/dashboard", timeout=5).json()["dashboard"]
        assert next(t for t in dash["tiles"] if t["id"] == tile["id"])["hidden"] is False
        desktop.assert_clean()
    finally:
        server.api.put(f"{server.url}/api/dashboard/tiles/{tile['id']}", json={"hidden": False}, timeout=5)


def test_the_motor_form_fits_a_phone(phone):
    page = phone.go("#/settings/switches")
    page.get_by_role("button", name="Add a motor (Up + Down)").click()
    page.locator(".modal").wait_for()
    page.wait_for_timeout(300)
    overflow = page.evaluate("(() => { const m = document.querySelector('.modal'); return m.scrollWidth - m.clientWidth; })()")
    assert overflow <= 1, f"the form scrolls sideways by {overflow}px"
    phone.no_sideways_scroll()
    phone.assert_clean()
