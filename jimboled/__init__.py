"""JimboLED – a dark-themed dashboard for WLED controllers and Pi GPIO relays."""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, g, jsonify, redirect, request, session, url_for

from .config import ConfigStore

__version__ = "1.0.0"

log = logging.getLogger(__name__)


class AppContext:
    """Everything the request handlers need, hung off ``app.extensions``."""

    def __init__(self, store: ConfigStore):
        self.store = store
        self.devices = None
        self.gpio = None
        self.discovery = None
        self.started_at = time.time()
        self._rev = 0
        self._rev_lock = threading.Lock()
        self.log_buffer = None

    def bump(self, *_args) -> None:
        with self._rev_lock:
            self._rev += 1

    @property
    def rev(self) -> int:
        with self._rev_lock:
            return self._rev


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
    store.on_change(ctx.bump)

    from .api import register_blueprints

    register_blueprints(app)
    _install_security(app, ctx)

    if start_services:
        ctx.devices.start()
        ctx.gpio.start()

    @app.teardown_appcontext
    def _noop(_exc):  # pragma: no cover
        pass

    return app


def get_ctx() -> AppContext:
    from flask import current_app

    return current_app.extensions["jimboled"]


# ---------------------------------------------------------------- security
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
        if path.startswith("/static/") or path in ("/healthz", "/favicon.ico", "/manifest.webmanifest"):
            return None
        server_cfg = ctx.store.section("server") or {}
        needs_login = bool(server_cfg.get("password_hash"))
        authed = session.get("auth") == "ok" or not needs_login
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
