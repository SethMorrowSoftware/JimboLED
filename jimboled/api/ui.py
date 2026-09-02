"""HTML pages: the single-page dashboard and the optional login screen."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .. import __version__, get_ctx

bp = Blueprint("ui", __name__)


@bp.get("/")
def index():
    ctx = get_ctx()
    dash = ctx.store.section("dashboard") or {}
    return render_template("index.html", version=__version__, title=dash.get("title") or "JimboLED",
                           accent=dash.get("accent") or "#7c5cff", theme=dash.get("theme") or "midnight")


@bp.route("/login", methods=["GET", "POST"])
def login():
    ctx = get_ctx()
    server_cfg = ctx.store.section("server") or {}
    if not server_cfg.get("password_hash"):
        return redirect(url_for("ui.index"))
    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password_hash(server_cfg["password_hash"], password):
            session.permanent = True
            session["auth"] = "ok"
            nxt = request.args.get("next") or url_for("ui.index")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("ui.index")
            return redirect(nxt)
        error = "Wrong password, try again."
    dash = ctx.store.section("dashboard") or {}
    return render_template("login.html", error=error, title=dash.get("title") or "JimboLED",
                           accent=dash.get("accent") or "#7c5cff", theme=dash.get("theme") or "midnight"), (401 if error else 200)


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("ui.index"))


@bp.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@bp.get("/manifest.webmanifest")
def manifest():
    ctx = get_ctx()
    dash = ctx.store.section("dashboard") or {}
    return {
        "name": dash.get("title") or "JimboLED",
        "short_name": dash.get("title") or "JimboLED",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1117",
        "theme_color": dash.get("accent") or "#7c5cff",
        "icons": [{"src": "/static/img/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }
