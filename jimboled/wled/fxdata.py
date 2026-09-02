"""Parser for WLED effect metadata (``/json/fxdata``).

Since WLED 0.14 every effect publishes a metadata string that tells a UI
which sliders, colours and options it actually uses::

    "Speed,Intensity,Custom 1,Custom 2,Custom 3,Check 1,Check 2,Check 3;Fx,Bg,Cs;!;1;sx=32,ix=128"
     └──────────── sliders / checkboxes ─────────────────────┘ └ colours ┘ │ │  └ defaults
                                                                palette ─┘ └ flags (0/1/2 dims, v/f audio)

The rules below mirror ``setEffectParameters`` in WLED's stock UI
(``wled00/data/index.js``):

* Whole string empty (e.g. *Solid*, *Oscillate*): show speed + intensity,
  all three colours, palette; flags ``1``.  The stock UI special-cases effect
  0 (*Solid*) to ``;!;`` – no sliders, one colour, no palette – and so do we.
* Sliders section: positional labels for ``sx, ix, c1, c2, c3, o1, o2, o3``.
  Empty field = hidden, ``!`` = default label, other text = that label.
  An empty *section* (with metadata present) means no sliders at all.
* Colours section: same rules for ``col[0..2]`` (defaults ``Fx``/``Bg``/``Cs``).
* Palette section: empty = hidden; ``!`` = shown; other text = custom label,
  which may carry ``=N`` (a default palette id) that is stripped.
* Flags: characters ``0 1 2 3 v f``; missing/empty = ``1``.
* Defaults: ``key=value`` pairs applied when the effect is chosen.
"""
from __future__ import annotations

from typing import Any, Dict, List

SLIDER_DEFAULTS = ["Speed", "Intensity", "Custom 1", "Custom 2", "Custom 3", "Check 1", "Check 2", "Check 3"]
SLIDER_KEYS = ["sx", "ix", "c1", "c2", "c3", "o1", "o2", "o3"]
SLIDER_MAX = {"sx": 255, "ix": 255, "c1": 255, "c2": 255, "c3": 31}
COLOR_DEFAULTS = ["Color 1", "Color 2", "Color 3"]
COLOR_KEYS = ["col0", "col1", "col2"]
SOLID_FXDATA = ";!;"


def _label(item: str, default: str) -> str:
    item = item.strip()
    return default if item == "!" else item


def parse_fxdata(raw: str, effect_id: int = -1) -> Dict[str, Any]:
    """Return a UI description for one effect."""
    raw = (raw or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if effect_id == 0 and raw == "":
        raw = SOLID_FXDATA
    empty = raw == ""
    sections = raw.split(";")
    while len(sections) < 5:
        sections.append("")
    sliders_sec, colors_sec, palette_sec, flags_sec, defaults_sec = sections[:5]

    # --- sliders / checkboxes
    if empty:
        parts = ["!", "!"]
    elif sliders_sec == "":
        parts = []
    else:
        parts = sliders_sec.split(",")
    sliders: List[Dict[str, Any]] = []
    for idx, key in enumerate(SLIDER_KEYS):
        if idx < len(parts):
            label = _label(parts[idx], SLIDER_DEFAULTS[idx])
            visible = parts[idx].strip() != ""
        else:
            label, visible = SLIDER_DEFAULTS[idx], False
        entry = {"key": key, "label": label, "visible": visible,
                 "type": "check" if key.startswith("o") else "slider"}
        if key in SLIDER_MAX:
            entry["max"] = SLIDER_MAX[key]
        sliders.append(entry)

    # --- colours
    if empty:
        cparts = ["1", "2", "3"]
    elif colors_sec == "":
        cparts = []
    else:
        cparts = colors_sec.split(",")
    colors: List[Dict[str, Any]] = []
    for idx, key in enumerate(COLOR_KEYS):
        if idx < len(cparts):
            label = _label(cparts[idx], COLOR_DEFAULTS[idx])
            visible = cparts[idx].strip() != ""
        else:
            label, visible = COLOR_DEFAULTS[idx], False
        colors.append({"key": key, "index": idx, "label": label, "visible": visible})

    # --- palette
    palette_label = "Palette"
    palette_default = None
    if empty:
        palette_visible = True
    else:
        head = palette_sec.split("=", 1)
        palette_visible = palette_sec.strip() != "" and not head[0].strip().lstrip("-").isdigit()
        if palette_visible:
            palette_label = _label(head[0], "Palette")
        if len(head) > 1 and head[1].strip().isdigit():
            palette_default = int(head[1])

    # --- flags
    flags = "1" if (empty or flags_sec.strip() == "") else flags_sec.strip()

    # --- defaults
    defaults: Dict[str, int] = {}
    for kv in defaults_sec.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                defaults[k] = int(v)
            except ValueError:
                continue
    if palette_default is not None and "pal" not in defaults:
        defaults["pal"] = palette_default

    return {
        "sliders": sliders,
        "colors": colors,
        "palette": {"visible": palette_visible, "label": palette_label},
        "flags": {
            "raw": flags,
            "zero_d": "0" in flags,
            "one_d": "1" in flags,
            "two_d": "2" in flags,
            "audio_volume": "v" in flags,
            "audio_frequency": "f" in flags,
        },
        "defaults": defaults,
    }


def build_effect_catalog(effects: List[str], fxdata: List[str]) -> List[Dict[str, Any]]:
    """Merge names with metadata; drop reserved slots."""
    out = []
    for idx, name in enumerate(effects):
        clean = name.split("@", 1)[0].strip()
        if not clean or clean.startswith("RSVD") or clean == "-":
            continue
        embedded = name.split("@", 1)[1] if "@" in name else ""  # 0.13 embedded metadata
        meta_raw = fxdata[idx] if idx < len(fxdata) and fxdata[idx] else embedded
        meta = parse_fxdata(meta_raw, idx)
        out.append({"id": idx, "name": clean, **meta})
    return out


def is_moonmodules(info: Dict[str, Any]) -> bool:
    return info.get("product") == "MoonModules" or "rel" in info or "-mdev" in str(info.get("ver", ""))


def is_v16_plus(info: Dict[str, Any]) -> bool:
    try:
        vid = int(info.get("vid") or 0)
    except (TypeError, ValueError):
        vid = 0
    return vid >= 2605010 and not is_moonmodules(info)


def palette_catalog(palettes: List[str], info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Built-in palettes plus synthesised names for custom palettes."""
    out = [{"id": i, "name": n.split("@", 1)[0]} for i, n in enumerate(palettes) if n and not n.startswith("RSVD")]
    cpal = int(info.get("cpalcount") or 0)
    if cpal > 0:
        # Upstream 16.0+ (build 2605010) counts custom palettes down from 200;
        # 0.14/0.15 and WLED-MM count down from 255.
        base = 200 if is_v16_plus(info) else 255
        for n in range(cpal):
            out.append({"id": base - n, "name": f"Custom {n}", "custom": True})
    umpal = int(info.get("umpalcount") or 0)
    names = info.get("umpalnames") or []
    for n in range(umpal):
        label = names[n] if n < len(names) else f"Usermod {n}"
        out.append({"id": 255 - n, "name": str(label), "custom": True})
    return out
