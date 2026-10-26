"""JimboLED – a dark-themed dashboard for WLED controllers and Pi GPIO relays."""
from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import ipaddress
import re

from flask import Flask, g, jsonify, redirect, request, session, url_for

from .config import ConfigStore

# Host names that can only be reached from inside a home network. Anything
# else (a public domain an attacker could point at this Pi – "DNS rebinding")
# is refused unless listed in server.allowed_hosts.
PRIVATE_SUFFIXES = ("local", "lan", "home", "internal", "intranet", "localdomain", "arpa", "box", "private", "homenet", "localhost")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


def host_is_allowed(host_header: str, allowed: list) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # IPv6 literal
        host = host.split("]", 1)[0].lstrip("[")
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.rstrip(".")
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host in (allowed or []):
        return True
    labels = host.split(".")
    if len(labels) == 1:
        return _HOSTNAME_RE.match(host) is not None
    return labels[-1] in PRIVATE_SUFFIXES

__version__ = "1.0.0"

log = logging.getLogger(__name__)


class AppContext:
    """Everything the request handlers need, hung off ``app.extensions``."""

    def __init__(self, store: ConfigStore):
        self.store = store
        self.devices = None
        self.gpio = None
        self.discovery = None
        self.scheduler = None
        # API token id -> last time it was used (memory only: writing the
        # config on every scripted request would wear the SD card).
        self.token_last_used: Dict[str, float] = {}
        self.started_at = time.time()
        self._rev = 0
        self._cfg_rev = 0
        self._rev_lock = threading.Lock()
        self.log_buffer = None

    def bump(self, *_args) -> None:
        """Something observable changed (device state, relay, config)."""
        with self._rev_lock:
            self._rev += 1

    def bump_config(self, *_args) -> None:
        """The configuration (devices, switches, dashboard layout) changed."""
        with self._rev_lock:
            self._rev += 1
            self._cfg_rev += 1

    @property
    def rev(self) -> int:
        with self._rev_lock:
            return self._rev

    @property
    def cfg_rev(self) -> int:
        with self._rev_lock:
            return self._cfg_rev


def create_app(data_dir: Optional[str] = None, *, start_services: bool = True, testing: bool = False) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates", static_url_path="/static")
    app.config["JSON_SORT_KEYS"] = False
    app.json.sort_keys = False  # type: ignore[attr-defined]
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.config["TESTING"] = testing

    store = ConfigStore(data_dir)
    ctx = AppContext(store)
    app.extensions["jimboled"] = ctx

    # Secret key persists across restarts so logins survive a reboot.
    server_cfg = store.section("server") or {}
    secret = server_cfg.get("secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        store.update(lambda c: c["server"].__setitem__("secret_key", secret))
    app.secret_key = secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30

    from .logbuffer import install_log_buffer

    ctx.log_buffer = install_log_buffer()

    from .wled.manager import DeviceManager
    from .gpio.manager import GPIOManager
    from .wled.discovery import DiscoveryService

    ctx.devices = DeviceManager(store, on_change=ctx.bump)
    ctx.gpio = GPIOManager(store, on_event=ctx.bump)
    ctx.discovery = DiscoveryService(store, ctx.devices)
    store.on_change(ctx.bump_config)

    from .scheduler import Scheduler

    ctx.scheduler = Scheduler(store, lambda action: run_scheduled_action(ctx, action))

    from .api import register_blueprints

    register_blueprints(app)
    _install_security(app, ctx)

    # Physical buttons run the same actions as schedules, one at a time, on
    # their own thread so a slow light never holds up the relay watchdog.
    from concurrent.futures import ThreadPoolExecutor

    from .api.dashboard import clean_lights_action
    from .scheduler import normalise_action

    ctx.gpio.action_cleaner = lambda action: normalise_action(action, clean_action=clean_lights_action)
    button_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="button")

    def _run_button(item, kind):
        result = run_scheduled_action(ctx, getattr(item, kind))
        log.info("button '%s' %s: %s", item.name, "tap" if kind == "press" else "hold",
                 result.get("error") or result.get("message"))

    ctx.gpio.on_button = lambda item, kind: button_pool.submit(_run_button, item, kind)

    if start_services:
        ctx.devices.start()
        ctx.gpio.start()
        ctx.scheduler.start()

    @app.teardown_appcontext
    def _noop(_exc):  # pragma: no cover
        pass

    return app


def run_scheduled_action(ctx: AppContext, action: Dict[str, Any]) -> Dict[str, Any]:
    """Carry out one schedule's action and sum it up as ``{"message"|"error"}``."""
    from .api.dashboard import run_action, run_scene_actions

    if action.get("type") == "scene":
        scenes = (ctx.store.section("dashboard") or {}).get("scenes", [])
        scene = next((s for s in scenes if s.get("id") == action.get("ref")), None)
        if scene is None:
            return {"error": "its scene has been deleted"}
        results = run_scene_actions(ctx, scene, source="schedule")
        failed = [r for r in results if r.get("error")]
        if failed:
            return {"error": f"scene '{scene.get('name')}': " + "; ".join(str(r['error']) for r in failed)[:280]}
        return {"message": f"ran scene '{scene.get('name')}'"}
    if action.get("type") == "switch":
        switches = ((ctx.store.section("gpio") or {}).get("switches") or [])
        sw = next((s for s in switches if s.get("id") == action.get("ref")), None)
        if sw is None:
            return {"error": "its switch has been deleted"}
        if sw.get("mode") == "momentary" and action.get("action") != "off":
            # The switch may have become hold-to-run after the schedule was made.
            return {"error": f"'{sw.get('name')}' is hold-to-run, so a schedule can only turn it off"}
    entry = run_action(ctx, action, source="schedule")
    if entry.get("error"):
        return {"error": str(entry["error"])}
    if entry.get("message"):
        return {"message": entry["message"]}
    return {"message": "done"}


def get_ctx() -> AppContext:
    from flask import current_app

    return current_app.extensions["jimboled"]


# ---------------------------------------------------------------- security
# API tokens are for automations, so they may *operate* things – lights,
# scenes, timers, switches, the emergency stop's engage button – but not change
# how the system is set up: no settings, switches, stops, schedules or tokens,
# no resetting an emergency stop, no holding a hold-to-run switch, and no
# reading backups (they contain the password hash).
TOKEN_DENIED_READS = ("/api/tokens", "/api/settings", "/api/backup", "/api/backups")
TOKEN_ALLOWED_WRITES = tuple(re.compile(p) for p in (
    r"^/api/lights/(apply|capture)$",
    r"^/api/lights/undo/[^/]+$",
    r"^/api/devices/[^/]+/identify$",
    r"^/api/devices/[^/]+/(state|refresh|copy)$",
    r"^/api/devices/[^/]+/presets/\d+/copy$",
    r"^/api/scenes/[^/]+/run$",
    r"^/api/schedules/[^/]+/run$",
    r"^/api/timers$",
    r"^/api/gpio/switches/[^/]+/action$",
    r"^/api/gpio/all-off$",
    r"^/api/system/shutdown-all$",
    r"^/api/estop/engage$",
))
# Hold-to-run needs a person on the button; a script is not one.
TOKEN_SWITCH_ACTIONS = ("on", "off", "toggle", "pulse")


def _token_allowed(method: str, path: str) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return not any(path == p or path.startswith(p + "/") for p in TOKEN_DENIED_READS)
    if method != "POST" or not any(rx.match(path) for rx in TOKEN_ALLOWED_WRITES):
        return False
    if path.startswith("/api/gpio/switches/"):
        data = request.get_json(silent=True)
        return isinstance(data, dict) and str(data.get("action") or "").lower() in TOKEN_SWITCH_ACTIONS
    return True


def match_token(server_cfg: Dict[str, Any], token: str) -> Optional[Dict[str, Any]]:
    """The stored token record ``token`` belongs to, if any (constant-time compare)."""
    import hashlib
    import hmac

    if not token or len(token) > 200:
        return None
    digest = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
    found = None
    for rec in server_cfg.get("api_tokens") or []:
        if isinstance(rec, dict) and isinstance(rec.get("hash"), str):
            if hmac.compare_digest(rec["hash"], digest):
                found = rec
    return found


def _install_security(app: Flask, ctx: AppContext) -> None:
    """Optional password + CSRF hardening.

    * If a password is configured, every page and API call needs a session.
    * State-changing API calls must carry ``X-Requested-With: JimboLED`` –
      browsers cannot add custom headers cross-origin without CORS, so this
      blocks CSRF from other sites even when no password is set.
    """

    @app.before_request
    def _guard():
        path = request.path
        server_cfg = ctx.store.section("server") or {}
        if not app.config.get("TESTING") or app.config.get("CHECK_HOST"):
            if not host_is_allowed(request.host, server_cfg.get("allowed_hosts") or []):
                msg = (f"JimboLED refused the address '{request.host}'. Open it via its IP address or "
                       f"{socket.gethostname()}.local, or add this name under Settings → Security → Allowed names.")
                if path.startswith("/api/"):
                    return jsonify({"error": msg}), 421
                return msg, 421, {"Content-Type": "text/plain; charset=utf-8"}
        if path.startswith("/static/") or path in ("/healthz", "/favicon.ico", "/manifest.webmanifest"):
            return None
        auth_header = request.headers.get("Authorization", "")
        if path.startswith("/api/") and auth_header[:7].lower() == "bearer ":
            # Scripts, iOS Shortcuts, Home Assistant.  A browser never attaches
            # this header to a cross-site request by itself, so a valid token
            # stands in for both the login and the CSRF header.
            rec = match_token(server_cfg, auth_header[7:].strip())
            if rec is None:
                return jsonify({"error": "invalid API token"}), 401
            if not _token_allowed(request.method, path):
                return jsonify({"error": "API tokens can operate lights, scenes, timers and switches, but not "
                                         "change settings or reset emergency stops; use the dashboard for that"}), 403
            ctx.token_last_used[rec["id"]] = time.time()
            g.authed = True
            g.token = rec["id"]
            return None
        needs_login = bool(server_cfg.get("password_hash"))
        session_ok = session.get("auth") == "ok" and int(session.get("sv") or 0) == int(server_cfg.get("session_version") or 0)
        authed = session_ok or not needs_login
        g.authed = authed
        if path.startswith("/api/"):
            if path in ("/api/login", "/api/auth"):
                return None
            if not authed:
                return jsonify({"error": "login required", "login": True}), 401
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get("X-Requested-With") != "JimboLED":
                    return jsonify({"error": "missing X-Requested-With header"}), 403
            return None
        if not authed and path not in ("/login",):
            return redirect(url_for("ui.login", next=path))
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
