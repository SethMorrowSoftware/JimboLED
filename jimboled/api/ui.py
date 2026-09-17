"""HTML pages: the single-page dashboard and the optional login screen."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .. import __version__, get_ctx

bp = Blueprint("ui", __name__)


# Browser chrome colour per theme, so the phone's status bar matches the page.
THEME_COLORS = {
    "midnight": "#0b0d14", "graphite": "#111214", "oled": "#000000", "ocean": "#061019",
    "daylight": "#f4f5f9", "paper": "#efece6", "auto": "#0b0d14",
}


def _appearance(dash: dict) -> dict:
    """The handful of values the first paint needs, so nothing flashes."""
    theme = dash.get("theme") or "midnight"
    try:
        scale = max(85, min(150, int(dash.get("text_scale") or 100))) / 100.0
    except (TypeError, ValueError):
        scale = 1.0
    return {
        "title": dash.get("title") or "JimboLED",
        "accent": dash.get("accent") or "#7c5cff",
        "theme": theme,
        "density": dash.get("density") or "comfortable",
        "radius": dash.get("radius") or "soft",
        "text_scale": f"{scale:.3f}",
        "theme_color": THEME_COLORS.get(theme, "#0b0d14"),
    }


@bp.get("/")
def index():
    ctx = get_ctx()
    dash = ctx.store.section("dashboard") or {}
    return render_template("index.html", version=__version__, **_appearance(dash))


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
            session["sv"] = int(server_cfg.get("session_version") or 0)
            nxt = request.args.get("next") or url_for("ui.index")
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or ":" in nxt.split("?")[0]:
                nxt = url_for("ui.index")
            return redirect(nxt)
        error = "Wrong password, try again."
    dash = ctx.store.section("dashboard") or {}
    return render_template("login.html", error=error, **_appearance(dash)), (401 if error else 200)


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
