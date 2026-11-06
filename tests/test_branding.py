"""Branding: logo / background uploads, appearance options, backups."""
import base64
import io
import json

import pytest

from jimboled.branding import BrandingError, check_svg, sniff, validate

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><defs><linearGradient id="g"/></defs><rect fill="url(#g)" width="10" height="10"/></svg>'


def upload(client, kind, data, name="logo.png"):
    return client.post(f"/api/branding/{kind}", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


# ------------------------------------------------------------ validation
@pytest.mark.parametrize("data,ext", [(PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp"), (SVG, "svg"),
                                      (b'<?xml version="1.0"?>\n' + SVG, "svg"), (b"\xef\xbb\xbf" + SVG, "svg")])
def test_sniff_recognises_formats(data, ext):
    assert sniff(data) == ext


@pytest.mark.parametrize("data", [b"hello", b"<html><script>alert(1)</script></html>", b"%PDF-1.4", b"\x00" * 20])
def test_sniff_rejects_non_images(data):
    with pytest.raises(BrandingError):
        sniff(data)


@pytest.mark.parametrize("evil", [
    b'<svg><script>alert(1)</script></svg>',
    b'<svg onload="alert(1)"></svg>',
    b'<svg><a href="javascript:alert(1)"><rect/></a></svg>',
    b'<svg><foreignObject><div>x</div></foreignObject></svg>',
    b'<svg><image href="http://evil.example/x.png"/></svg>',
    b'<svg><use xlink:href="other.svg#a"/></svg>',
    b'<svg><style>@import url(http://evil.example/a.css);</style></svg>',
    b'<svg><rect style="fill:url(http://evil.example/track)"/></svg>',
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg>&x;</svg>',
    b'<svg><iframe src="x"/></svg>',
])
def test_svg_with_active_content_is_refused(evil):
    with pytest.raises(BrandingError):
        check_svg(evil)


def test_svg_with_inline_references_is_fine():
    check_svg(SVG)
    check_svg(b'<svg><use href="#a"/><image href="data:image/png;base64,AAAA"/></svg>')


def test_validate_size_limits():
    with pytest.raises(BrandingError, match="too big"):
        validate("logo", PNG + b"\x00" * (1024 * 1024))
    validate("background", PNG + b"\x00" * (1024 * 1024))  # backgrounds may be bigger
    with pytest.raises(BrandingError, match="empty"):
        validate("logo", b"")
    with pytest.raises(BrandingError):
        validate("favicon", PNG)


# ------------------------------------------------------------- endpoints
def test_no_logo_by_default(client):
    r = client.get("/api/dashboard").get_json()
    assert r["branding"] == {"logo": None, "background": None}
    assert client.get("/branding/logo").status_code == 404
    page = client.get("/").get_data(as_text=True)
    assert 'id="brand-logo" alt="" hidden' in page
    assert "data-has-logo" not in page
    assert 'href="/static/img/icon.svg"' in page


def test_upload_logo_shows_everywhere(client):
    r = upload(client, "logo", PNG)
    assert r.status_code == 201, r.get_json()
    info = r.get_json()["branding"]["logo"]
    assert info["type"] == "image/png" and info["url"].startswith("/branding/logo?v=")
    assert r.get_json()["dashboard"]["branding_rev"] == 1

    img = client.get(info["url"])
    assert img.status_code == 200 and img.data == PNG and img.mimetype == "image/png"
    assert "script-src" not in img.headers["Content-Security-Policy"]
    assert "default-src 'none'" in img.headers["Content-Security-Policy"]
    assert "max-age" in img.headers["Cache-Control"]

    page = client.get("/").get_data(as_text=True)
    assert f'src="{info["url"]}"' in page and "data-has-logo" in page
    assert f'rel="icon" id="favicon" href="{info["url"]}" type="image/png"' in page

    man = client.get("/manifest.webmanifest").get_json()
    assert man["icons"][0]["src"] == info["url"]

    fav = client.get("/favicon.ico")
    assert fav.status_code == 200 and fav.data == PNG


def test_favicon_without_logo_redirects_to_default(client):
    r = client.get("/favicon.ico")
    assert r.status_code in (301, 302) and r.headers["Location"].endswith("/static/img/icon.svg")


def test_replacing_logo_with_another_format_keeps_one_file(client, app):
    upload(client, "logo", PNG)
    r = upload(client, "logo", SVG, "logo.svg")
    assert r.status_code == 201
    ctx = app.extensions["jimboled"]
    files = sorted(p.name for p in ctx.branding.dir.iterdir())
    assert files == ["logo.svg"]
    served = client.get(r.get_json()["branding"]["logo"]["url"])
    assert served.mimetype == "image/svg+xml"
    assert "sandbox" in served.headers["Content-Security-Policy"]


def test_upload_rejects_bad_files(client):
    assert upload(client, "logo", b"not a picture").status_code == 400
    assert "SVG contains scripts" in upload(client, "logo", b"<svg><script>x</script></svg>", "x.svg").get_json()["error"]
    assert upload(client, "banner", PNG).status_code == 404
    r = client.post("/api/branding/logo", data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and "Choose a picture" in r.get_json()["error"]
    # nothing was stored
    assert client.get("/api/dashboard").get_json()["branding"]["logo"] is None


def test_upload_needs_csrf_header_and_login(app, client):
    plain = app.test_client()
    r = plain.post("/api/branding/logo", data={"file": (io.BytesIO(PNG), "l.png")}, content_type="multipart/form-data")
    assert r.status_code == 403
    from werkzeug.security import generate_password_hash
    ctx = app.extensions["jimboled"]
    upload(client, "logo", PNG)
    ctx.store.update(lambda c: c["server"].__setitem__("password_hash", generate_password_hash("pw")))
    anon = app.test_client()
    anon.environ_base["HTTP_X_REQUESTED_WITH"] = "JimboLED"
    assert anon.post("/api/branding/logo", data={"file": (io.BytesIO(PNG), "l.png")},
                     content_type="multipart/form-data").status_code == 401
    assert anon.delete("/api/branding/logo").status_code == 401
    # The logo itself stays public: the login page shows it.
    assert anon.get("/branding/logo").status_code == 200
    login = anon.get("/login").get_data(as_text=True)
    assert "/branding/logo?v=" in login


def test_delete_logo(client):
    upload(client, "logo", PNG)
    r = client.delete("/api/branding/logo")
    assert r.status_code == 200 and r.get_json()["branding"]["logo"] is None
    assert client.get("/branding/logo").status_code == 404
    assert client.delete("/api/branding/logo").status_code == 200  # idempotent
    assert client.delete("/api/branding/nope").status_code == 404


def test_background_upload_switches_backdrop_and_delete_switches_back(client):
    r = upload(client, "background", JPEG, "bg.jpg")
    assert r.status_code == 201
    assert r.get_json()["dashboard"]["backdrop"] == "image"
    page = client.get("/").get_data(as_text=True)
    assert 'data-backdrop="image"' in page and "--backdrop-image: url('/branding/background?v=" in page
    r = client.delete("/api/branding/background")
    assert r.get_json()["dashboard"]["backdrop"] == "glow"


def test_image_backdrop_without_picture_falls_back(client):
    assert client.put("/api/dashboard", json={"backdrop": "image"}).status_code == 200
    assert 'data-backdrop="glow"' in client.get("/").get_data(as_text=True)


def test_missing_file_means_no_logo(client, app):
    """A snapshot or hand edit can't leave a dangling logo: the file is the truth."""
    upload(client, "logo", PNG)
    ctx = app.extensions["jimboled"]
    for p in ctx.branding.dir.iterdir():
        p.unlink()
    assert client.get("/api/dashboard").get_json()["branding"]["logo"] is None
    assert client.get("/branding/logo").status_code == 404


# ----------------------------------------------------------- appearance
def test_appearance_options_round_trip(client):
    payload = {"brand_mode": "logo", "logo_size": "l", "font": "rounded", "backdrop": "plain",
               "backdrop_dim": 70, "surface": "glass", "footer_text": "  Call   Jim\n on 204 ",
               "custom_css": ".tile { outline: 1px solid red; }\r\n"}
    r = client.put("/api/dashboard", json=payload)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["dashboard"]
    assert d["brand_mode"] == "logo" and d["logo_size"] == "l" and d["font"] == "rounded"
    assert d["backdrop"] == "plain" and d["backdrop_dim"] == 70 and d["surface"] == "glass"
    assert d["footer_text"] == "Call Jim on 204"
    assert d["custom_css"] == ".tile { outline: 1px solid red; }\n"
    page = client.get("/").get_data(as_text=True)
    for attr in ('data-font="rounded"', 'data-backdrop="plain"', 'data-surface="glass"', 'data-brand="logo"',
                 'data-logo-size="l"', "--backdrop-dim: 0.70"):
        assert attr in page
    assert ".tile { outline: 1px solid red; }" in page


@pytest.mark.parametrize("bad", [
    {"brand_mode": "huge"}, {"logo_size": "xl"}, {"font": "comic"}, {"backdrop": "video"},
    {"surface": "metal"}, {"backdrop_dim": 95}, {"backdrop_dim": -1}, {"backdrop_dim": "dark"},
    {"footer_text": "x" * 121}, {"custom_css": "a" * 20001},
])
def test_appearance_rejects_bad_values(client, bad):
    r = client.put("/api/dashboard", json=bad)
    assert r.status_code == 400, bad


def test_custom_css_cannot_escape_style_element(client):
    client.put("/api/dashboard", json={"custom_css": "a{}</style><script>alert(1)</script>"})
    page = client.get("/").get_data(as_text=True)
    # Still inside the <style> element, where "<script>" is just text.
    assert "a{}<\\/style><script>alert(1)<\\/script></style>" in page
    assert page.count("</style>") == 1


def test_title_and_footer_are_escaped(client):
    client.put("/api/dashboard", json={"title": "<b>x</b>", "subtitle": "<i>y</i>"})
    page = client.get("/").get_data(as_text=True)
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert "&lt;i&gt;y&lt;/i&gt;" in page


def test_reset_appearance(client):
    upload(client, "logo", PNG)
    client.put("/api/dashboard", json={"theme": "paper", "accent": "#ff0000", "font": "mono", "surface": "glass",
                                       "custom_css": "x{}", "title": "Jim's", "favorite_colors": ["#00ff00"]})
    r = client.post("/api/dashboard/appearance/reset", json={})
    d = r.get_json()["dashboard"]
    assert d["theme"] == "midnight" and d["accent"] == "#7c5cff" and d["font"] == "system"
    assert d["surface"] == "solid" and d["custom_css"] == ""
    assert d["title"] == "Jim's" and d["favorite_colors"] == ["#00ff00"]  # yours, not "appearance"
    assert r.get_json()["branding"]["logo"] is not None  # pictures kept unless asked
    r = client.post("/api/dashboard/appearance/reset", json={"pictures": True})
    assert r.get_json()["branding"] == {"logo": None, "background": None}


# ---------------------------------------------------------------- backup
def test_backup_carries_pictures_and_restore_puts_them_back(client, app):
    upload(client, "logo", PNG)
    upload(client, "background", JPEG, "bg.jpg")
    client.put("/api/dashboard", json={"font": "serif", "footer_text": "hello"})
    backup = json.loads(client.get("/api/backup").data)
    assert set(backup["branding"]) == {"logo", "background"}
    assert base64.b64decode(backup["branding"]["logo"]["data"]) == PNG

    client.delete("/api/branding/logo")
    client.delete("/api/branding/background")
    client.put("/api/dashboard", json={"font": "mono", "footer_text": ""})

    r = client.post("/api/restore", data={"file": (io.BytesIO(json.dumps(backup).encode()), "b.json")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    d = client.get("/api/dashboard").get_json()
    assert d["branding"]["logo"]["type"] == "image/png"
    assert d["branding"]["background"]["type"] == "image/jpeg"
    assert d["dashboard"]["font"] == "serif" and d["dashboard"]["footer_text"] == "hello"
    assert client.get(d["branding"]["logo"]["url"]).data == PNG
    # "branding" is not configuration and must not end up in config.json
    assert "branding" not in app.extensions["jimboled"].store.get()


def test_restore_skips_a_bad_picture_and_cleans_bad_appearance(client):
    backup = json.loads(client.get("/api/backup").data)
    backup["branding"] = {"logo": {"type": "image/png", "data": base64.b64encode(b"<svg onload=x>").decode()},
                          "background": {"data": "!!!not base64"}}
    backup["dashboard"].update({"font": "wingdings", "backdrop_dim": 400, "surface": 3, "custom_css": None,
                                "theme": "neon", "accent": "red"})
    r = client.post("/api/restore", data={"file": (io.BytesIO(json.dumps(backup).encode()), "b.json")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    d = client.get("/api/dashboard").get_json()
    assert d["branding"] == {"logo": None, "background": None}
    dash = d["dashboard"]
    assert dash["font"] == "system" and dash["backdrop_dim"] == 90 and dash["surface"] == "solid"
    assert dash["custom_css"] == "" and dash["theme"] == "midnight" and dash["accent"] == "#7c5cff"


def test_restoring_an_old_backup_keeps_current_pictures(client):
    upload(client, "logo", PNG)
    backup = json.loads(client.get("/api/backup").data)
    del backup["branding"]  # made before pictures were part of backups
    r = client.post("/api/restore", data={"file": (io.BytesIO(json.dumps(backup).encode()), "b.json")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert client.get("/api/dashboard").get_json()["branding"]["logo"] is not None


def test_backup_with_pictures_fits_the_upload_limit(client, app):
    upload(client, "logo", PNG + b"\x00" * (1000 * 1024))
    upload(client, "background", JPEG + b"\x00" * (1500 * 1024), "bg.jpg")
    raw = client.get("/api/backup").data
    assert len(raw) < app.config["MAX_CONTENT_LENGTH"]
    r = client.post("/api/restore", data={"file": (io.BytesIO(raw), "b.json")}, content_type="multipart/form-data")
    assert r.status_code == 200
