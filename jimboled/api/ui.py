"""HTML pages: the single-page dashboard and the optional login screen."""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, send_file, session, url_for
from markupsafe import Markup
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
    branding = get_ctx().branding.summary()
    logo = branding.get("logo")
    background = branding.get("background")
    try:
        dim = max(0, min(90, int(dash.get("backdrop_dim", 45))))
    except (TypeError, ValueError):
        dim = 45
    backdrop = dash.get("backdrop") or "glow"
    if backdrop == "image" and not background:
        backdrop = "glow"
    # Custom CSS goes inside a <style> element: the only way out of one is
    # "</style", so no "</" is let through.  "<\/" means the same in CSS.
    css = str(dash.get("custom_css") or "").replace("</", "<\\/")
    return {
        "logo_url": logo["url"] if logo else "",
        "icon_url": logo["url"] if logo else "/static/img/icon.svg",
        "icon_type": logo["type"] if logo else "image/svg+xml",
        "background_url": background["url"] if background else "",
        "brand_mode": dash.get("brand_mode") or "logo_title",
        "logo_size": dash.get("logo_size") or "m",
        "font": dash.get("font") or "system",
        "backdrop": backdrop,
        "backdrop_dim": f"{dim / 100:.2f}",
        "surface": dash.get("surface") or "solid",
        "custom_css": Markup(css),
        "subtitle": dash.get("subtitle") or "",
        "version": __version__,
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
    return render_template("index.html", **_appearance(dash))


@bp.route("/login", methods=["GET", "POST"])
def login():
    ctx = get_ctx()
    server_cfg = ctx.store.section("server") or {}
    if not server_cfg.get("password_hash"):
        return redirect(url_for("ui.index"))
    error = ""
    if request.method == "POST":
        # Same throttle as /api/login: without it this form was a way round
        # the lock-out, at full speed.
        from .system import _login_blocked, _login_failed, _login_ok

        addr = request.remote_addr or "?"
        dash = ctx.store.section("dashboard") or {}
        if _login_blocked(addr):
            return render_template("login.html", error="Too many attempts. Wait 30 seconds and try again.",
                                   **_appearance(dash)), 429
        password = request.form.get("password", "")
        if check_password_hash(server_cfg["password_hash"], password):
            _login_ok(addr)
            session.permanent = True
            session["auth"] = "ok"
            session["sv"] = int(server_cfg.get("session_version") or 0)
            nxt = request.args.get("next") or url_for("ui.index")
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or ":" in nxt.split("?")[0]:
                nxt = url_for("ui.index")
            return redirect(nxt)
        _login_failed(addr)
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


@bp.get("/branding/<kind>")
def branding_image(kind):
    from ..branding import SVG_CSP

    found = get_ctx().branding.get(kind)
    if not found:
        abort(404)
    path, mime = found
    # The URL carries a version, so the picture can be cached for a long time.
    resp = send_file(path, mimetype=mime, max_age=60 * 60 * 24 * 30, conditional=True)
    resp.headers["Content-Security-Policy"] = SVG_CSP
    return resp


@bp.get("/favicon.ico")
def favicon():
    from ..branding import SVG_CSP

    found = get_ctx().branding.get("logo")
    if found:
        path, mime = found
        resp = send_file(path, mimetype=mime, max_age=3600)
        resp.headers["Content-Security-Policy"] = SVG_CSP
        return resp
    return redirect("/static/img/icon.svg")


@bp.get("/manifest.webmanifest")
def manifest():
    ctx = get_ctx()
    dash = ctx.store.section("dashboard") or {}
    logo = ctx.branding.info("logo")
    if logo:
        icons = [{"src": logo["url"], "sizes": "any", "type": logo["type"], "purpose": "any"}]
    else:
        icons = [{"src": "/static/img/icon.svg", "sizes": "any", "type": "image/svg+xml"}]
    return {
        "name": dash.get("title") or "JimboLED",
        "short_name": dash.get("title") or "JimboLED",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1117",
        "theme_color": dash.get("accent") or "#7c5cff",
        "icons": icons,
    }
