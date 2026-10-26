"""Device-agnostic light intents.

A *light intent* says what a person wants – "every light red", "the preset
called Movie", "Fire 2012 at half speed" – without knowing anything about a
particular controller.  That matters because WLED numbers its effects,
palettes and presets per build and per device: effect 66 on one controller is
not effect 66 on the next, and a preset slot is just a number in that one
device's ``presets.json``.  A colour sent as ``"seg": {...}`` without an id
also only reaches the *selected* segments, so a strip split into zones would
only half change.

This module turns an intent into the exact payload each controller needs,
using what the poller already knows about it (segments, capabilities, effect,
palette and preset names), and says plainly what it had to leave out.

Intent fields (all optional, at least one required)::

    on            true | false | "toggle"
    bri           1-255            brightness ("brightness" 1-100 % also accepted)
    color         "#rrggbb" | [r,g,b]          primary colour
    colors        up to 3 of the above (null = leave that slot alone)
    white         0-255            white channel, on controllers that have one
    cct           0-255            warm (0) … cool (255), on CCT-capable strips
    effect        name | "solid"   matched by name on every controller
    palette       name             matched by name
    speed, intensity  0-255
    custom        {"c1","c2","c3" (0-31),"o1","o2","o3"}
    preset        name             each controller's own preset with that name
    preset_slot   1-250            the same slot number everywhere
    transition    seconds 0-60     fade time for this change only
    nightlight    {"on", "minutes" 1-255, "mode" fade|instant|color|sunrise, "target" 0-255}
    sync          true             restart effect clocks together so animations line up
    segments      "all" (default) | "main"

Setting a look (colour, effect, palette or preset) turns the light on unless
``on`` says otherwise – "make everything red" should not leave a dark room.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")
NIGHTLIGHT_MODES = {"instant": 0, "fade": 1, "color": 2, "colour": 2, "sunrise": 3}
LOOK_KEYS = ("color", "colors", "white", "cct", "effect", "palette", "speed", "intensity", "custom", "preset", "preset_slot")
# Segment fields a snapshot keeps.  Geometry is included so restoring a
# snapshot puts a split strip back exactly as it was, like a WLED preset saved
# with "include segment bounds".
SNAPSHOT_SEG_KEYS = ("id", "start", "stop", "startY", "stopY", "grp", "spc", "of", "on", "frz", "bri", "cct", "set",
                     "n", "col", "fx", "sx", "ix", "pal", "c1", "c2", "c3", "sel", "rev", "mi", "rY", "mY", "tp",
                     "o1", "o2", "o3", "si", "m12", "bm")
# Segment fields that describe a *look* and travel between controllers.
LOOK_SEG_KEYS = ("on", "bri", "cct", "col", "fx", "sx", "ix", "pal", "c1", "c2", "c3", "o1", "o2", "o3", "rev", "mi", "si", "m12")


class IntentError(ValueError):
    """The intent itself is malformed (a 400, not a device problem)."""


# ------------------------------------------------------------------ parsing
def _int(value: Any, lo: int, hi: int, name: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise IntentError(f"{name} must be a number")
    if math.isnan(number) or math.isinf(number):
        raise IntentError(f"{name} must be a number")
    return int(max(lo, min(hi, round(number))))


def parse_color(value: Any, name: str = "color") -> List[int]:
    """``"#ff8800"``, ``"ff8800"``, ``[255, 136, 0]`` → ``[255, 136, 0]``."""
    if isinstance(value, str):
        m = HEX_COLOR.match(value.strip())
        if not m:
            raise IntentError(f"{name} must look like #ff8800")
        text = m.group(1)
        return [int(text[i:i + 2], 16) for i in (0, 2, 4)]
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return [_int(v, 0, 255, name) for v in value[:3]]
    raise IntentError(f"{name} must be a hex colour or [r, g, b]")


def _bool_or_toggle(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in ("toggle", "t"):
        return "t"
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def normalise_intent(raw: Any) -> Dict[str, Any]:
    """Validate an intent from the API, a scene or a schedule."""
    if not isinstance(raw, dict):
        raise IntentError("the light settings must be an object")
    out: Dict[str, Any] = {}
    if raw.get("on") is not None:
        out["on"] = _bool_or_toggle(raw["on"])
    if raw.get("bri") is not None:
        out["bri"] = _int(raw["bri"], 1, 255, "bri")
    elif raw.get("brightness") is not None:
        out["bri"] = max(1, round(_int(raw["brightness"], 1, 100, "brightness") * 2.55))
    colors: List[Optional[List[int]]] = []
    if raw.get("colors") is not None:
        if not isinstance(raw["colors"], list) or not raw["colors"]:
            raise IntentError("colors must be a list of up to three colours")
        colors = [None if c is None else parse_color(c, f"colors[{i}]") for i, c in enumerate(raw["colors"][:3])]
    if raw.get("color") is not None:
        first = parse_color(raw["color"])
        colors = [first] + colors[1:] if colors else [first]
    if colors and any(c is not None for c in colors):
        out["colors"] = colors
    if raw.get("white") is not None:
        out["white"] = _int(raw["white"], 0, 255, "white")
    if raw.get("cct") is not None:
        out["cct"] = _int(raw["cct"], 0, 255, "cct")
    for key in ("effect", "palette", "preset"):
        if raw.get(key) not in (None, ""):
            value = raw[key]
            if key == "effect" and isinstance(value, (int, float)) and int(value) == 0:
                value = "Solid"
            if not isinstance(value, str):
                raise IntentError(f"{key} must be a name, e.g. \"Rainbow\"")
            out[key] = value.strip()[:64]
    if raw.get("preset_slot") is not None:
        out["preset_slot"] = _int(raw["preset_slot"], 1, 250, "preset_slot")
    if raw.get("speed") is not None:
        out["speed"] = _int(raw["speed"], 0, 255, "speed")
    if raw.get("intensity") is not None:
        out["intensity"] = _int(raw["intensity"], 0, 255, "intensity")
    if raw.get("custom") is not None:
        if not isinstance(raw["custom"], dict):
            raise IntentError("custom must be an object")
        custom = {}
        for k, v in raw["custom"].items():
            if k in ("c1", "c2"):
                custom[k] = _int(v, 0, 255, k)
            elif k == "c3":
                custom[k] = _int(v, 0, 31, k)
            elif k in ("o1", "o2", "o3"):
                custom[k] = bool(v)
        if custom:
            out["custom"] = custom
    if raw.get("transition") is not None:
        out["transition"] = _int(_seconds(raw["transition"]) * 10, 0, 600, "transition") / 10.0
    if raw.get("nightlight") is not None:
        out["nightlight"] = _nightlight(raw["nightlight"])
    if raw.get("sync"):
        out["sync"] = True
    seg_mode = str(raw.get("segments") or "all")
    if seg_mode not in ("all", "main"):
        raise IntentError("segments must be \"all\" or \"main\"")
    if seg_mode != "all":
        out["segments"] = seg_mode
    if not any(k in out for k in ("on", "bri", "transition", "nightlight", "sync") + LOOK_KEYS):
        raise IntentError("say what to change: a colour, brightness, preset, effect or on/off")
    return out


def _seconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise IntentError("transition must be a number of seconds")
    if math.isnan(number) or math.isinf(number):
        raise IntentError("transition must be a number of seconds")
    return number


def _nightlight(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise IntentError("nightlight must be an object")
    nl: Dict[str, Any] = {"on": bool(raw.get("on", True))}
    if raw.get("minutes") is not None:
        nl["dur"] = _int(raw["minutes"], 1, 255, "nightlight.minutes")
    mode = raw.get("mode")
    if mode is not None:
        if isinstance(mode, str) and mode.lower() in NIGHTLIGHT_MODES:
            nl["mode"] = NIGHTLIGHT_MODES[mode.lower()]
        else:
            nl["mode"] = _int(mode, 0, 3, "nightlight.mode")
    if raw.get("target") is not None:
        nl["tbri"] = _int(raw["target"], 0, 255, "nightlight.target")
    return nl


def describe_intent(intent: Dict[str, Any]) -> str:
    """Short human summary, for scene and schedule lists."""
    parts: List[str] = []
    if intent.get("preset"):
        parts.append(f"preset “{intent['preset']}”")
    if intent.get("preset_slot"):
        parts.append(f"preset #{intent['preset_slot']}")
    if intent.get("colors"):
        first = next((c for c in intent["colors"] if c), None)
        if first:
            parts.append("#%02x%02x%02x" % tuple(first))
    if intent.get("effect"):
        parts.append(intent["effect"])
    if intent.get("palette"):
        parts.append(f"{intent['palette']} palette")
    if intent.get("bri") is not None:
        parts.append(f"{round(intent['bri'] / 2.55)}%")
    if intent.get("nightlight"):
        nl = intent["nightlight"]
        parts.append(f"sleep timer {nl.get('dur', 60)} min" if nl.get("on", True) else "sleep timer off")
    if "on" in intent:
        word = {"t": "toggle", True: "on", False: "off"}.get(intent["on"], "on")
        if not parts or intent["on"] is not True:
            parts.insert(0, word)
    return ", ".join(parts) or "change lights"


# ------------------------------------------------------------- per device
class DeviceView:
    """What the planner needs to know about one controller (read-only)."""

    def __init__(self, *, name: str, online: bool, state: Dict[str, Any], info: Dict[str, Any],
                 effects: List[Dict[str, Any]], palettes: List[Dict[str, Any]], presets: Dict[str, Dict[str, Any]]):
        self.name = name
        self.online = online
        self.state = state if isinstance(state, dict) else {}
        self.info = info if isinstance(info, dict) else {}
        self.effects = effects or []
        self.palettes = palettes or []
        self.presets = presets or {}

    # capabilities ---------------------------------------------------------
    @property
    def light_caps(self) -> int:
        leds = self.info.get("leds") if isinstance(self.info.get("leds"), dict) else {}
        try:
            return int(leds.get("lc") or 1)
        except (TypeError, ValueError):
            return 1

    @property
    def has_white(self) -> bool:
        return bool(self.light_caps & 2)

    @property
    def has_cct(self) -> bool:
        return bool(self.light_caps & 4)

    def segments(self) -> List[Dict[str, Any]]:
        segs = self.state.get("seg")
        if not isinstance(segs, list):
            return []
        return [s for s in segs if isinstance(s, dict) and isinstance(s.get("id"), int)]

    # name lookups ---------------------------------------------------------
    def effect_id(self, name: str) -> Optional[int]:
        if name.strip().lower() == "solid":
            return 0
        return _find_id(self.effects, name)

    def palette_id(self, name: str) -> Optional[int]:
        return _find_id(self.palettes, name)

    def preset_slot(self, name: str) -> Optional[int]:
        want = _fold(name)
        best = None
        for key, val in self.presets.items():
            if not isinstance(val, dict) or not val:
                continue
            if _fold(str(val.get("n") or "")) == want:
                try:
                    slot = int(key)
                except ValueError:
                    continue
                if best is None or slot < best:
                    best = slot
        return best

    def effect_name(self, fx: Any) -> Optional[str]:
        return _name_for(self.effects, fx)

    def palette_name(self, pal: Any) -> Optional[str]:
        return _name_for(self.palettes, pal)


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold())


def _find_id(items: Iterable[Dict[str, Any]], name: str) -> Optional[int]:
    want = _fold(name)
    for item in items:
        if isinstance(item, dict) and _fold(str(item.get("name") or "")) == want:
            try:
                return int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _name_for(items: Iterable[Dict[str, Any]], ident: Any) -> Optional[str]:
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == ident:
            return str(item.get("name") or "")
    return None


def plan(view: DeviceView, intent: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build the WLED state payload for one controller.

    Returns ``(payload, notes)``.  ``payload`` is ``None`` when nothing in the
    intent applies to this controller (the notes say why).  Notes also list
    parts that were dropped while the rest still applies.
    """
    notes: List[str] = []
    payload: Dict[str, Any] = {}
    sets_look = any(k in intent for k in LOOK_KEYS)

    if intent.get("preset") or intent.get("preset_slot"):
        slot = view.preset_slot(intent["preset"]) if intent.get("preset") else intent["preset_slot"]
        if slot is None:
            notes.append(f"has no preset called “{intent['preset']}”")
        elif intent.get("preset_slot") and str(slot) not in {k for k, v in view.presets.items() if v}:
            notes.append(f"has no preset #{slot}")
        else:
            payload["ps"] = slot

    seg_fields: Dict[str, Any] = {}
    if intent.get("effect"):
        fx = view.effect_id(intent["effect"])
        if fx is None:
            notes.append(f"has no effect called “{intent['effect']}”")
        else:
            seg_fields["fx"] = fx
            if not any(k in intent for k in ("speed", "intensity", "custom")):
                seg_fields["fxdef"] = True  # load the effect's own recommended settings
    if intent.get("palette"):
        pal = view.palette_id(intent["palette"])
        if pal is None:
            notes.append(f"has no palette called “{intent['palette']}”")
        else:
            seg_fields["pal"] = pal
    if "speed" in intent:
        seg_fields["sx"] = intent["speed"]
    if "intensity" in intent:
        seg_fields["ix"] = intent["intensity"]
    for k, v in (intent.get("custom") or {}).items():
        seg_fields[k] = v
    # A copied look carries white/CCT whenever the source had them; on a
    # target without them that is expected, not worth a warning.
    quiet = bool(intent.get("best_effort"))
    if "cct" in intent:
        if view.has_cct:
            seg_fields["cct"] = intent["cct"]
        elif not quiet:
            notes.append("has no adjustable white temperature")
    wants_white = "white" in intent and view.has_white
    if "white" in intent and not view.has_white and not quiet:
        notes.append("has no separate white LEDs")

    segs = view.segments()
    if intent.get("segments") == "main":
        main_id = view.state.get("mainseg", 0)
        segs = [s for s in segs if s.get("id") == main_id] or segs[:1]

    colors = intent.get("colors")
    if seg_fields or colors or wants_white:
        if segs:
            payload["seg"] = [_segment_payload(s, seg_fields, colors, intent.get("white") if wants_white else None, view.has_white)
                              for s in segs]
        else:
            # Nothing known about the segments yet (first poll pending): the
            # bare object reaches every *selected* segment, which is the best
            # WLED offers without ids.
            payload["seg"] = _segment_payload({}, seg_fields, colors, intent.get("white") if wants_white else None, view.has_white)

    if "on" in intent:
        payload["on"] = intent["on"]
    elif sets_look and (payload.get("seg") or payload.get("ps")):
        payload["on"] = True
    if "bri" in intent:
        payload["bri"] = intent["bri"]
    if "transition" in intent:
        payload["tt"] = int(round(intent["transition"] * 10))
    if intent.get("nightlight"):
        payload["nl"] = dict(intent["nightlight"])
        if payload["nl"].get("on") and "on" not in payload:
            payload["on"] = True  # a sleep timer on a dark light has nothing to fade
    if intent.get("sync"):
        payload["tb"] = 0

    if not payload or set(payload) <= {"tt", "tb"}:
        return None, notes or ["nothing to change"]
    return payload, notes


def _segment_payload(seg: Dict[str, Any], fields: Dict[str, Any], colors: Optional[List[Optional[List[int]]]],
                     white: Optional[int], has_white: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "id" in seg:
        out["id"] = seg["id"]
    out.update(fields)
    if colors or white is not None:
        current = seg.get("col") if isinstance(seg.get("col"), list) else []
        slots: List[Any] = []
        wanted = list(colors or [])
        if white is not None and not wanted:
            # White on its own: keep each segment's colour, change only W.
            base = current[0] if current and isinstance(current[0], list) else [0, 0, 0]
            wanted = [list(base[:3])]
        for i, rgb in enumerate(wanted):
            if rgb is None:
                slots.append([])  # WLED: leave this slot alone
                continue
            value = list(rgb[:3])
            if has_white:
                if i == 0 and white is not None:
                    value.append(white)
                else:
                    # Keep whatever the white channel was doing in this slot.
                    cur = current[i] if i < len(current) and isinstance(current[i], list) else []
                    value.append(int(cur[3]) if len(cur) > 3 and isinstance(cur[3], (int, float)) else 0)
            slots.append(value)
        while slots and slots[-1] == []:
            slots.pop()
        if slots:
            out["col"] = slots
    return out


# ------------------------------------------------------------ snapshots
def snapshot(view: DeviceView) -> Optional[Dict[str, Any]]:
    """The controller's full current look, as a payload that restores it."""
    st = view.state
    if not st:
        return None
    out: Dict[str, Any] = {}
    for key in ("on", "bri", "mainseg"):
        if key in st:
            out[key] = st[key]
    segs = []
    for seg in view.segments():
        clean = {k: seg[k] for k in SNAPSHOT_SEG_KEYS if k in seg}
        if "n" not in clean:
            clean["n"] = ""  # changing bounds without a name clears it; keep it explicit
        segs.append(clean)
    if segs:
        out["seg"] = segs
    return out or None


def look_intent(view: DeviceView, *, include_power: bool = True) -> Dict[str, Any]:
    """What a controller is showing, as an intent another controller can follow.

    Taken from its main segment: colours, effect and palette (by name, so they
    translate), speed, intensity and the effect's custom controls.
    """
    st = view.state
    segs = view.segments()
    main_id = st.get("mainseg", 0)
    main = next((s for s in segs if s.get("id") == main_id), segs[0] if segs else {})
    intent: Dict[str, Any] = {"best_effort": True}
    if include_power:
        if "on" in st:
            intent["on"] = bool(st["on"])
        if isinstance(st.get("bri"), int):
            intent["bri"] = max(1, min(255, st["bri"]))
    cols = main.get("col") if isinstance(main.get("col"), list) else []
    colors = [[int(max(0, min(255, v))) for v in c[:3]] for c in cols[:3] if isinstance(c, list) and len(c) >= 3]
    if colors:
        intent["colors"] = colors
        first = cols[0]
        if view.has_white and len(first) > 3 and isinstance(first[3], (int, float)):
            intent["white"] = int(first[3])
    fx_name = view.effect_name(main.get("fx"))
    if fx_name:
        intent["effect"] = fx_name
    pal_name = view.palette_name(main.get("pal"))
    if pal_name:
        intent["palette"] = pal_name
    if isinstance(main.get("sx"), int):
        intent["speed"] = main["sx"]
    if isinstance(main.get("ix"), int):
        intent["intensity"] = main["ix"]
    custom = {k: main[k] for k in ("c1", "c2", "c3", "o1", "o2", "o3") if k in main}
    if custom:
        intent["custom"] = custom
    if view.has_cct and isinstance(main.get("cct"), int):
        intent["cct"] = main["cct"]
    return intent


def portable_preset(view: DeviceView, slot: int) -> Dict[str, Any]:
    """A preset from one controller in a form another controller can store.

    Effects and palettes travel by name; segment geometry does not (another
    strip has a different length), so each target segment gets the look of
    the source segment with the same id, or of the source's main segment.
    """
    raw = view.presets.get(str(slot))
    if not isinstance(raw, dict) or not raw:
        raise IntentError(f"preset #{slot} does not exist")
    if "playlist" in raw:
        raise IntentError("playlists point at other presets on the same controller, so they can't be copied")
    if "win" in raw and "seg" not in raw:
        raise IntentError("this preset is an HTTP-API command, not a look, so it can't be copied")
    segs = []
    for i, seg in enumerate(raw.get("seg") or []):
        if not isinstance(seg, dict) or seg.get("stop") == 0:
            continue
        look = {k: seg[k] for k in LOOK_SEG_KEYS if k in seg and k not in ("fx", "pal")}
        if "fx" in seg:
            look["effect"] = view.effect_name(seg["fx"])
        if "pal" in seg:
            look["palette"] = view.palette_name(seg["pal"])
        look["id"] = seg.get("id", i)
        segs.append(look)
    return {
        "name": str(raw.get("n") or f"Preset {slot}"),
        "quick_label": str(raw.get("ql") or "")[:2],
        "master": {k: raw[k] for k in ("on", "bri", "transition") if k in raw},
        "segs": segs,
        "main": raw.get("mainseg", 0),
    }


def preset_state_for(view: DeviceView, portable: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The state to store as a preset on ``view``, plus notes on what didn't fit."""
    notes: List[str] = []
    state: Dict[str, Any] = dict(portable.get("master") or {})
    src = {s["id"]: s for s in portable.get("segs") or []}
    fallback = src.get(portable.get("main", 0)) or next(iter(src.values()), None)
    out_segs = []
    targets = view.segments() or [{"id": 0}]
    for seg in targets:
        look = src.get(seg["id"]) or fallback
        if not look:
            continue
        entry: Dict[str, Any] = {"id": seg["id"]}
        for k, v in look.items():
            if k in ("id", "effect", "palette"):
                continue
            if k == "col" and isinstance(v, list):
                v = [list(c[:4] if view.has_white else c[:3]) if isinstance(c, list) else c for c in v]
            entry[k] = v
        if look.get("effect"):
            fx = view.effect_id(look["effect"])
            if fx is None:
                if f"no effect {look['effect']}" not in notes:
                    notes.append(f"has no effect called “{look['effect']}” (kept its current one)")
            else:
                entry["fx"] = fx
        if look.get("palette"):
            pal = view.palette_id(look["palette"])
            if pal is None:
                notes.append(f"has no palette called “{look['palette']}”")
            else:
                entry["pal"] = pal
        out_segs.append(entry)
    if out_segs:
        state["seg"] = out_segs
    if not state:
        raise IntentError("that preset has nothing a controller can store")
    return state, sorted(set(notes))


# ------------------------------------------------------------- catalogue
def union_catalog(views: List[DeviceView]) -> Dict[str, Any]:
    """Effects, palettes and presets across several controllers, by name.

    Each entry carries how many of the online controllers have it, so the UI
    can say "4 of 5" rather than promise something that only half works.
    """
    online = [v for v in views if v.online]
    effects: Dict[str, Dict[str, Any]] = {}
    palettes: Dict[str, Dict[str, Any]] = {}
    presets: Dict[str, Dict[str, Any]] = {}
    for v in online:
        seen = set()
        for fx in v.effects:
            name = str(fx.get("name") or "")
            key = _fold(name)
            if not name or key in seen:
                continue
            seen.add(key)
            entry = effects.setdefault(key, {"name": name, "count": 0, "flags": fx.get("flags") or {},
                                             "sliders": fx.get("sliders") or [], "palette": fx.get("palette") or {}})
            entry["count"] += 1
        seen = set()
        for pal in v.palettes:
            name = str(pal.get("name") or "")
            key = _fold(name)
            if not name or key in seen or pal.get("custom"):
                continue
            seen.add(key)
            entry = palettes.setdefault(key, {"name": name, "count": 0})
            if pal.get("stops") and "stops" not in entry:
                entry["stops"] = pal["stops"]
            entry["count"] += 1
        seen = set()
        for slot, val in v.presets.items():
            if not isinstance(val, dict) or not val:
                continue
            name = str(val.get("n") or f"Preset {slot}")
            key = _fold(name)
            if key in seen:
                continue
            seen.add(key)
            entry = presets.setdefault(key, {"name": name, "count": 0, "devices": [], "playlist": False, "quick_label": ""})
            entry["count"] += 1
            entry["devices"].append(v.name)
            entry["playlist"] = entry["playlist"] or "playlist" in val
            entry["quick_label"] = entry["quick_label"] or str(val.get("ql") or "")
    fx_list = sorted(effects.values(), key=lambda e: (e["name"].lower() != "solid", -e["count"], e["name"].lower()))
    pal_list = sorted(palettes.values(), key=lambda p: (-p["count"], p["name"].lower()))
    preset_list = sorted(presets.values(), key=lambda p: (-p["count"], p["name"].lower()))
    total_leds = 0
    power = 0
    for v in online:
        leds = v.info.get("leds") if isinstance(v.info.get("leds"), dict) else {}
        total_leds += int(leds.get("count") or 0) if isinstance(leds.get("count"), (int, float)) else 0
        power += int(leds.get("pwr") or 0) if isinstance(leds.get("pwr"), (int, float)) else 0
    return {
        "total": len(views),
        "online": len(online),
        "effects": fx_list,
        "palettes": pal_list,
        "presets": preset_list,
        "caps": {"white": any(v.has_white for v in online), "cct": any(v.has_cct for v in online)},
        "led_count": total_leds,
        "power_ma": power,
    }
