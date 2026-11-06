"""Custom branding images: the logo and the dashboard background.

The images live as plain files next to ``config.json`` (``branding/logo.png``
and so on) rather than inside it.  ``config.json`` is read on every request
and copied into every automatic snapshot; a few hundred kilobytes of base64
in there would slow the whole dashboard down on a Pi Zero.

The file on disk is the source of truth: if it is there, there is a logo.
That way restoring an older snapshot can never point at an image that no
longer exists.  The configuration only carries a revision number
(``dashboard.branding_rev``) so other open dashboards notice the change and
browsers fetch the new image instead of a cached one.

Every upload is checked by its content, not its name or the browser's
claimed type.  SVG is text that a browser will happily execute scripts from,
so an SVG is only accepted when it contains nothing active, and it is always
served with a Content-Security-Policy that forbids scripts anyway.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from .atomicio import write_bytes_atomic

log = logging.getLogger(__name__)

KINDS = ("logo", "background")
# Generous for a logo, and the browser shrinks big photos before uploading a
# background, so these limits are only ever hit by something unusual.
MAX_BYTES = {"logo": 1024 * 1024, "background": 1536 * 1024}

TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}

# Served with every branding image.  An SVG opened directly in a tab must not
# be able to run anything with the dashboard's origin.
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"

_SVG_FORBIDDEN = [
    (re.compile(r"<\s*script", re.I), "scripts"),
    (re.compile(r"<\s*foreignObject", re.I), "embedded HTML"),
    (re.compile(r"<\s*(iframe|embed|object|audio|video|link|meta|base)\b", re.I), "embedded content"),
    (re.compile(r"<!\s*(ENTITY|DOCTYPE)", re.I), "a DOCTYPE or entities"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "event handlers"),
    (re.compile(r"javascript\s*:", re.I), "javascript: links"),
    (re.compile(r"@import", re.I), "stylesheet imports"),
]
# href / xlink:href may only point inside the file or at an inline image.
_SVG_HREF = re.compile(r"""(?:xlink:)?href\s*=\s*["']\s*([^"']*)["']""", re.I)
_SVG_URL = re.compile(r"""url\(\s*["']?\s*([^)"'\s]*)""", re.I)


class BrandingError(ValueError):
    pass


def sniff(data: bytes) -> str:
    """Return the file extension for ``data``, or raise :class:`BrandingError`."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower()):
        return "svg"
    raise BrandingError("That file isn't a picture JimboLED can use. Use a PNG, JPEG, GIF, WebP or SVG.")


def check_svg(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BrandingError("That SVG isn't valid UTF-8 text")
    for pattern, what in _SVG_FORBIDDEN:
        if pattern.search(text):
            raise BrandingError(f"That SVG contains {what}, which a logo doesn't need. Export it as a plain SVG or PNG.")
    for pattern in (_SVG_HREF, _SVG_URL):
        for ref in pattern.findall(text):
            ref = ref.strip()
            if ref and not ref.startswith("#") and not ref.lower().startswith("data:image/"):
                raise BrandingError("That SVG links to outside files. Export it with everything embedded, or as a PNG.")


def validate(kind: str, data: bytes) -> str:
    """Check an upload; returns its extension."""
    if kind not in KINDS:
        raise BrandingError(f"unknown image '{kind}'")
    if not data:
        raise BrandingError("The file is empty")
    if len(data) > MAX_BYTES[kind]:
        raise BrandingError(f"That picture is too big ({len(data) // 1024} KB). "
                            f"The {kind} can be at most {MAX_BYTES[kind] // 1024} KB.")
    ext = sniff(data)
    if ext == "svg":
        check_svg(data)
    return ext


class BrandingStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "branding"
        self._lock = threading.Lock()

    def _find(self, kind: str) -> Optional[Path]:
        for ext in TYPES:
            p = self.dir / f"{kind}.{ext}"
            if p.is_file():
                return p
        return None

    def get(self, kind: str) -> Optional[Tuple[Path, str]]:
        """``(path, mimetype)`` of the current image, or ``None``."""
        if kind not in KINDS:
            return None
        p = self._find(kind)
        if p is None:
            return None
        return p, TYPES[p.suffix[1:]]

    def info(self, kind: str) -> Optional[Dict[str, object]]:
        found = self.get(kind)
        if not found:
            return None
        path, mime = found
        try:
            st = path.stat()
        except OSError:
            return None
        tag = hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:10]
        return {"url": f"/branding/{kind}?v={tag}", "type": mime, "size": st.st_size}

    def summary(self) -> Dict[str, Optional[Dict[str, object]]]:
        return {k: self.info(k) for k in KINDS}

    def save(self, kind: str, data: bytes) -> str:
        ext = validate(kind, data)
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / f"{kind}.{ext}"
            write_bytes_atomic(target, data)
            # Only one file per kind: drop the old one if the format changed.
            for other in TYPES:
                if other != ext:
                    try:
                        (self.dir / f"{kind}.{other}").unlink()
                    except FileNotFoundError:
                        pass
        log.info("branding: new %s (%s, %d bytes)", kind, ext, len(data))
        return ext

    def delete(self, kind: str) -> bool:
        if kind not in KINDS:
            raise BrandingError(f"unknown image '{kind}'")
        removed = False
        with self._lock:
            for ext in TYPES:
                try:
                    (self.dir / f"{kind}.{ext}").unlink()
                    removed = True
                except FileNotFoundError:
                    pass
        return removed

    # ------------------------------------------------------- backup support
    def export(self) -> Dict[str, Dict[str, str]]:
        out = {}
        for kind in KINDS:
            found = self.get(kind)
            if not found:
                continue
            path, mime = found
            try:
                out[kind] = {"type": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")}
            except OSError as exc:
                log.warning("branding: could not read %s for the backup: %s", kind, exc)
        return out

    def import_(self, raw: object) -> None:
        """Put back the images from a backup.  A bad one is skipped, never fatal.

        A backup without a ``branding`` section came from before images were
        part of backups, so the images in place are left alone.
        """
        if not isinstance(raw, dict):
            return
        for kind in KINDS:
            entry = raw.get(kind)
            if entry is None:
                if kind in raw:
                    self.delete(kind)
                continue
            try:
                data = base64.b64decode(str(entry.get("data") or ""), validate=True)
                self.save(kind, data)
            except (BrandingError, ValueError, AttributeError) as exc:
                log.warning("branding: skipping the %s in the backup: %s", kind, exc)
