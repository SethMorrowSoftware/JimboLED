"""Device registry and background poller for WLED controllers.

One :class:`DeviceManager` owns every controller.  A poller thread refreshes
each device's state on a schedule (fast when online, slow when offline) using
a small worker pool so one dead controller never delays the others.  User
commands go through :meth:`set_state`, which updates the cache immediately
from WLED's verbose response so the UI never shows stale values.
"""
from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .client import fit_name, WLEDClient, WLEDError, WLEDUnreachable, normalise_host
from .fxdata import build_effect_catalog, palette_catalog
from . import intents as _intents

log = logging.getLogger(__name__)

PRESET_REFRESH_S = 300.0
INFO_SUMMARY_KEYS = ("ver", "vid", "name", "arch", "mac", "ip", "uptime", "freeheap", "fxcount", "palcount", "live", "lm", "lip", "ndc", "brand", "product")

# Keys the dashboard may send to WLED.  Anything else is dropped so a typo
# in a client can't wedge a controller.
ALLOWED_STATE_KEYS = {
    "on", "bri", "transition", "tt", "bs", "ps", "pd", "pl", "nl", "udpn", "lor", "live", "mainseg",
    "seg", "playlist", "np", "ledmap", "time", "tb", "psave", "pdel", "n", "ib", "sb", "sc", "ql", "o",
}
ALLOWED_SEG_KEYS = {
    "id", "start", "stop", "startY", "stopY", "len", "grp", "spc", "of", "on", "frz", "bri", "cct",
    "set", "n", "col", "fx", "fxdef", "sx", "ix", "pal", "c1", "c2", "c3", "sel", "rev", "mi", "rY", "mY",
    "tp", "o1", "o2", "o3", "si", "m12", "bm", "rpt", "i",
}


class DeviceError(Exception):
    pass


class PutBackError(DeviceError):
    """A preset was saved, but the lights could not be put back as they were."""

    def __init__(self, message: str, *, presets: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.presets = presets or []


@dataclass
class DeviceRecord:
    cfg: Dict[str, Any]
    client: WLEDClient
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Held while the effect/palette/preset catalogue downloads, which can take
    # dozens of requests: commands only wait for ``lock``, so they go first.
    catalog_lock: threading.Lock = field(default_factory=threading.Lock)
    online: bool = False
    last_try: float = 0.0
    state: Dict[str, Any] = field(default_factory=dict)
    info: Dict[str, Any] = field(default_factory=dict)
    effects: List[str] = field(default_factory=list)
    palettes: List[str] = field(default_factory=list)
    fxdata: List[str] = field(default_factory=list)
    catalog: List[Dict[str, Any]] = field(default_factory=list)
    presets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    palette_data: Dict[int, Any] = field(default_factory=dict)
    catalog_key: str = ""
    presets_key: str = ""
    presets_loaded_at: float = 0.0
    last_seen: float = 0.0
    last_error: str = ""
    last_poll: float = 0.0
    next_poll: float = 0.0
    failures: int = 0
    in_flight: bool = False
    changed_at: float = 0.0
    latency_ms: float = 0.0
    # Set when a controller with a different MAC address answers at this
    # address (DHCP gave it the old one).  Nothing is sent to it then.
    wrong_device: str = ""
    wrong_mac: str = ""  # the MAC of the controller that answers instead
    wrong_owner: str = ""  # …and the name of the light it belongs to, if any


def normalise_mac(value: Any) -> str:
    """``"AA:BB:cc-dd.ee ff"`` → ``"aabbccddeeff"``; anything else → ``""``."""
    text = re.sub(r"[^0-9a-fA-F]", "", str(value or "")).lower()
    return text if len(text) == 12 else ""


# Lights added before MAC addresses were remembered have ids made from the
# MAC's last six hex digits ("wled-dd0301") and no "mac" in their config.
_LEGACY_ID = re.compile(r"^wled-([0-9a-f]{6})$")


def is_controller(cfg: Any, mac: Any) -> bool:
    """Whether the light configured as ``cfg`` is the controller with ``mac``:
    by its remembered MAC, or – for a light from before MACs were remembered –
    by the MAC suffix in its id."""
    mac = normalise_mac(mac)
    if not mac or not isinstance(cfg, dict):
        return False
    mine = normalise_mac(cfg.get("mac"))
    if mine:
        return mine == mac
    legacy = _LEGACY_ID.match(str(cfg.get("id") or ""))
    return bool(legacy) and mac.endswith(legacy.group(1))


def normalise_device(raw: Dict[str, Any], existing_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate a device config object from the API."""
    if not isinstance(raw, dict):
        raise DeviceError("device must be an object")
    host = normalise_host(str(raw.get("host") or raw.get("ip") or ""))
    name = str(raw.get("name") or host).strip()[:60]
    return {
        "id": existing_id or str(raw.get("id") or "").strip(),
        "name": name,
        "host": host,
        "enabled": bool(raw.get("enabled", True)),
        "icon": str(raw.get("icon") or "bulb")[:32],
        "color": str(raw.get("color") or "")[:16],
        "notes": str(raw.get("notes") or "")[:200],
        "poll_interval_s": _clamp(raw.get("poll_interval_s"), 1.0, 120.0, None),
        # Remembered from the controller itself, to recognise it at a new address.
        "mac": normalise_mac(raw.get("mac")),
    }


def _clamp(value, lo, hi, default):
    if value in (None, ""):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def sanitise_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist keys and coerce obvious types for a WLED state POST."""
    if not isinstance(payload, dict):
        raise DeviceError("state must be an object")
    try:
        return _sanitise_state(payload)
    except DeviceError:
        raise
    except (TypeError, ValueError) as exc:
        raise DeviceError(f"invalid state value: {exc}") from exc


def _sanitise_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if key not in ALLOWED_STATE_KEYS:
            continue
        if key == "seg":
            # A bare object (no id) targets every *selected* segment on the
            # device; a list is positional / by id.  Keep the shape as sent.
            if isinstance(value, dict):
                out["seg"] = _clean_segment(value)
            elif isinstance(value, list):
                out["seg"] = [_clean_segment(seg) for seg in value]
            else:
                raise DeviceError("seg must be an object or a list")
        elif key == "bri":
            out["bri"] = int(max(0, min(255, int(value))))
        elif key in ("transition", "tt"):
            out[key] = int(max(0, min(65535, int(value))))
        elif key == "ps":
            # ints or WLED's cycle syntax ("1~5~", "~", "~-", "r")
            out[key] = value if isinstance(value, str) and _VALUE_STRING.match(value) else int(value)
        elif key in ("pl", "psave", "pdel", "pd", "mainseg", "lor", "ledmap", "bs", "tb", "time"):
            out[key] = int(value)
        elif key in ("on", "ib", "sb", "sc", "o", "np", "live"):
            out[key] = _strict_bool(value, key)
        elif key in ("nl", "udpn", "playlist"):
            if not isinstance(value, dict):
                raise DeviceError(f"{key} must be an object")
            out[key] = value
        else:
            out[key] = value
    if not out:
        raise DeviceError("nothing to change")
    return out


def _strict_bool(value: Any, key: str) -> Any:
    """``True``/``False``/``"t"`` – never ``bool("false")``, which is True."""
    if isinstance(value, bool):
        return value
    if value == "t":
        return "t"
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "on", "yes", "1"):
            return True
        if word in ("false", "off", "no", "0"):
            return False
        if word in ("t", "toggle"):
            return "t"
    raise DeviceError(f"{key} must be true or false")


_VALUE_STRING = re.compile(r"^[~w]?-?\d{0,4}~?\d{0,4}~?r?$")


def _clean_segment(seg: Any) -> Dict[str, Any]:
    if not isinstance(seg, dict):
        raise DeviceError("segment must be an object")
    clean = {k: v for k, v in seg.items() if k in ALLOWED_SEG_KEYS}
    if "col" in clean:
        clean["col"] = _clean_colors(clean["col"])
    if "n" in clean:
        clean["n"] = fit_name(str(clean["n"]))
    for k in ("start", "stop", "len", "grp", "spc", "of", "fx", "sx", "ix", "pal", "c1", "c2", "c3", "si", "m12", "bm", "set", "id", "cct", "bri", "startY", "stopY"):
        if k not in clean:
            continue
        v = clean[k]
        if isinstance(v, str):
            if v.lstrip("-").isdigit():
                clean[k] = int(v)
            elif not _VALUE_STRING.match(v):
                raise DeviceError(f"segment {k} must be a number")
            continue
        try:
            clean[k] = int(v)
        except (TypeError, ValueError):
            raise DeviceError(f"segment {k} must be a number")
    return clean


def _clean_colors(cols: Any) -> List[Any]:
    if not isinstance(cols, list):
        raise DeviceError("col must be a list of colours")
    out: List[Any] = []
    for col in cols[:3]:
        if isinstance(col, str):
            if col == "r":          # random colour (WLED 16+)
                out.append("r")
                continue
            col = _hex_to_rgb(col)
        if isinstance(col, int):
            out.append(max(0, col))  # 0 = black, >0 = kelvin
            continue
        if isinstance(col, dict):
            out.append({k: int(max(0, min(255, int(v)))) for k, v in col.items() if k in ("r", "g", "b", "w")})
            continue
        if not isinstance(col, list):
            raise DeviceError("each colour must be [r,g,b] or [r,g,b,w]")
        out.append([int(max(0, min(255, int(c)))) for c in col[:4]])
    return out


def _hex_to_rgb(text: str) -> List[int]:
    text = text.strip().lstrip("#")
    if len(text) not in (6, 8):
        raise DeviceError(f"bad colour '{text}'")
    return [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]


class DeviceManager:
    def __init__(self, config_store, *, workers: int = 4, on_change: Optional[Callable[[str], None]] = None):
        self.store = config_store
        self._lock = threading.RLock()
        self._devices: Dict[str, DeviceRecord] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wled-poll")
        # A separate pool for what a person just asked for.  "All off" must not
        # queue behind four in-flight polls of controllers that are switched
        # off, each of which can take the full request timeout.
        # Wide enough that "every light" goes out in one wave rather than in
        # batches of four (a preset holds its worker for a moment while the
        # controller loads it).
        self._cmd_pool = ThreadPoolExecutor(max_workers=max(workers, 16), thread_name_prefix="wled-cmd")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_change = on_change
        self._settings = {}
        self.started = False

    # ---------------------------------------------------------------- setup
    def start(self) -> None:
        self._reload_settings()
        self._apply_config(self.store.section("devices") or [])
        self.store.on_change(self._on_config_change)
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="wled-scheduler", daemon=True)
        self._thread.start()
        atexit.register(self.stop)
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)
        self._cmd_pool.shutdown(wait=False)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            for rec in self._devices.values():
                rec.client.close()
        self.started = False

    def _reload_settings(self) -> None:
        w = self.store.section("wled") or {}
        self._settings = {
            "poll": _clamp(w.get("poll_interval_s"), 1.0, 120.0, 3.0),
            "offline_poll": _clamp(w.get("offline_poll_interval_s"), 3.0, 600.0, 15.0),
            "timeout": _clamp(w.get("request_timeout_s"), 1.0, 30.0, 4.0),
        }
        with self._lock:
            for rec in self._devices.values():
                rec.client.timeout = (min(2.0, self._settings["timeout"]), self._settings["timeout"])

    def _on_config_change(self, before: Dict[str, Any], after: Dict[str, Any]) -> None:
        if before.get("wled") != after.get("wled"):
            self._reload_settings()
        if before.get("devices") != after.get("devices"):
            self._apply_config(after.get("devices", []))

    def _apply_config(self, devices: List[Dict[str, Any]]) -> None:
        with self._lock:
            wanted = {d["id"]: d for d in devices if isinstance(d, dict) and d.get("id")}
            for did in list(self._devices):
                rec = self._devices[did]
                new = wanted.get(did)
                if new is None or new.get("host") != rec.cfg.get("host"):
                    rec.client.close()
                    del self._devices[did]
                else:
                    rec.cfg = dict(new)
            for did, cfg in wanted.items():
                if did not in self._devices:
                    try:
                        client = WLEDClient(str(cfg.get("host") or ""), timeout=self._settings["timeout"])
                    except Exception as exc:  # invalid host or malformed entry: skip, never crash
                        log.error("device %s has an invalid host: %s", did, exc)
                        continue
                    self._devices[did] = DeviceRecord(cfg=dict(cfg), client=client, next_poll=0.0)

    # --------------------------------------------------------------- polling
    def _poll_loop(self) -> None:
        while not self._stop.wait(0.5):
            now = time.monotonic()
            with self._lock:
                due = [rec for rec in self._devices.values()
                       if rec.cfg.get("enabled", True) and not rec.in_flight and rec.next_poll <= now]
                for rec in due:
                    rec.in_flight = True
            for rec in due:
                try:
                    self._pool.submit(self._poll_one, rec)
                except RuntimeError:
                    # Pool shut down: hand the flags back so a manager that is
                    # restarted does not find every device stuck "in flight".
                    for pending in due:
                        pending.in_flight = False
                    return

    def _poll_one(self, rec: DeviceRecord) -> None:
        try:
            self.refresh(rec.cfg["id"])
        except Exception:
            log.exception("poll failed for %s", rec.cfg.get("name"))
        finally:
            rec.in_flight = False

    def _schedule(self, rec: DeviceRecord) -> None:
        base = rec.cfg.get("poll_interval_s") or self._settings["poll"]
        if not rec.online:
            base = min(self._settings["offline_poll"], base * (2 ** min(rec.failures, 3)))
            base = max(base, self._settings["poll"])
        rec.next_poll = time.monotonic() + base

    def refresh(self, device_id: str) -> DeviceRecord:
        """Poll one device now (blocking).  Never raises for network errors."""
        rec = self._get(device_id)
        polled = False
        with self._lock:
            # Other lights' configs, to recognise their controllers.
            others = [r.cfg for r in self._devices.values() if r is not rec]
        with rec.lock:
            t0 = time.monotonic()
            try:
                doc = rec.client.get_state_info()
                rec.latency_ms = round((time.monotonic() - t0) * 1000, 1)
                mine, theirs = normalise_mac(rec.cfg.get("mac")), normalise_mac(_as_doc(doc["info"]).get("mac"))
                owner = next((str(o.get("name") or o.get("host")) for o in others if is_controller(o, theirs)), "")
                # A light from before MACs were remembered is checked against
                # the MAC its id was made from.
                legacy = not mine and bool(_LEGACY_ID.match(str(rec.cfg.get("id") or "")))
                problem = ""
                if theirs and (mine or legacy) and not is_controller(rec.cfg, theirs):
                    # Another controller got this one's old address.  Its
                    # state is not this light's, and commands must not reach it.
                    problem = (f"a different WLED ({_as_doc(doc['info']).get('name') or theirs}) "
                               f"answers at {rec.cfg.get('host')}. If the light moved it will be found "
                               "again; if you replaced its controller, edit the light and save to use the new one")
                elif owner and not mine:
                    # Not learned yet, and what answers is another light's
                    # controller (it moved here): never adopt it.
                    problem = (f"the controller answering at {rec.cfg.get('host')} is your light “{owner}”, "
                               "which has moved there; this light's own controller is elsewhere now – "
                               "edit the light and enter its new address")
                if problem:
                    rec.wrong_device, rec.wrong_mac, rec.wrong_owner = problem, theirs, owner
                    rec.failures = max(rec.failures, 1)
                    self._mark_offline(rec, problem)
                else:
                    rec.wrong_device = rec.wrong_mac = rec.wrong_owner = ""
                    self._absorb(rec, doc["state"], doc["info"])
                    polled = True
            except WLEDUnreachable as exc:
                self._mark_offline(rec, str(exc))
            except WLEDError as exc:
                self._mark_offline(rec, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("unexpected error polling %s", rec.cfg.get("name"))
                self._mark_offline(rec, f"{type(exc).__name__}: {exc}")
            finally:
                rec.last_poll = time.monotonic()
                self._schedule(rec)
        if polled:
            self._remember_mac(rec)
            self._load_catalog(rec)
        return rec

    def _remember_mac(self, rec: DeviceRecord) -> None:
        """Keep each controller's MAC address in its config entry, so it can be
        recognised at a new IP address even right after JimboLED restarts."""
        mac = normalise_mac(_as_doc(rec.info).get("mac"))
        # Learned once.  Never overwritten: a *different* MAC at this address
        # means another controller answers here (see refresh()).
        if not mac or rec.cfg.get("mac"):
            return
        did = rec.cfg["id"]

        host = rec.cfg.get("host")

        def mutate(cfg):
            for d in cfg.get("devices", []):
                # Only the entry this poll was for: its address may have been
                # edited (or a MAC set) since.
                if isinstance(d, dict) and d.get("id") == did and d.get("host") == host and not d.get("mac"):
                    d["mac"] = mac

        try:
            self.store.update(mutate)
            rec.cfg["mac"] = mac
        except Exception:  # pragma: no cover - a full disk must not stop polling
            log.exception("could not remember the MAC address of %s", rec.cfg.get("name"))

    def _load_catalog(self, rec: DeviceRecord, force: bool = False) -> None:
        """Fetch effects, palettes and presets without holding up commands.

        A busy or garbled answer is not "this controller has no effects": the
        catalogue is simply tried again on the next poll, and the controller
        is not counted as offline for it.
        """
        if not rec.catalog_lock.acquire(blocking=force):
            return  # another thread is loading it right now
        try:
            self._ensure_catalog(rec, force=force)
        except WLEDUnreachable as exc:
            with rec.lock:
                self._mark_offline(rec, str(exc))
            if force:
                raise
        except WLEDError as exc:
            log.info("%s: effect list not loaded yet (%s); will retry", rec.cfg.get("name"), exc)
            if force:
                raise
        finally:
            rec.catalog_lock.release()

    def _absorb(self, rec: DeviceRecord, state: Dict[str, Any], info: Optional[Dict[str, Any]]) -> None:
        # Whatever a controller sends lands in the snapshot that /api/state
        # serves every two seconds, so it is shaped here rather than trusted.
        # One device answering with a surprise used to take the whole dashboard
        # down with it, emergency-stop banner included.
        state = _as_doc(state)
        info = _as_doc(info)
        was_online = rec.online
        changed = state != rec.state
        rec.state = state or rec.state
        if info:
            rec.info = info
        rec.online = True
        rec.failures = 0
        rec.last_error = ""
        rec.last_seen = time.time()
        if changed or not was_online:
            rec.changed_at = time.time()
            self._notify(rec.cfg["id"])

    def _mark_offline(self, rec: DeviceRecord, error: str) -> None:
        rec.failures += 1
        was_online = rec.online
        # Tolerate a single dropped packet before declaring the device offline.
        if rec.failures >= 2 or not was_online:
            rec.online = False
        rec.last_error = error
        if was_online and not rec.online:
            log.warning("%s (%s) is offline: %s", rec.cfg.get("name"), rec.cfg.get("host"), error)
            rec.changed_at = time.time()
            self._notify(rec.cfg["id"])

    def _ensure_catalog(self, rec: DeviceRecord, force: bool = False) -> None:
        """Load effect/palette lists once per firmware version."""
        info = rec.info or {}
        key = f"{info.get('ver')}|{info.get('vid')}|{info.get('fxcount')}|{info.get('palcount')}|{info.get('cpalcount')}"
        if force or key != rec.catalog_key or not rec.effects:
            # Everything is fetched first and adopted together, and the key is
            # only recorded once it all arrived: a half-loaded catalogue would
            # otherwise be kept until the next firmware update.
            effects = rec.client.get_effects()
            palettes = rec.client.get_palettes()
            fxdata = rec.client.get_fxdata()
            presets = rec.client.get_presets()
            try:
                palette_data = rec.client.get_palette_data()
            except WLEDUnreachable:
                raise
            except WLEDError:
                palette_data = rec.palette_data  # previews only; keep what we had
            if rec.wrong_device:
                return  # found out meanwhile: these lists are another controller's
            rec.effects, rec.palettes, rec.fxdata = effects, palettes, fxdata
            rec.catalog = build_effect_catalog(effects, fxdata)
            rec.palette_data = palette_data or {}
            rec.presets = presets
            rec.presets_loaded_at = time.monotonic()
            rec.presets_key = _presets_key(info)
            rec.catalog_key = key
            self._notify(rec.cfg["id"])
            return
        # presets.json changed (mtime) or the device rebooted -> reload presets
        pkey = _presets_key(info)
        if _presets_changed(rec.presets_key, pkey) or time.monotonic() - rec.presets_loaded_at > PRESET_REFRESH_S:
            rec.presets = rec.client.get_presets()
            rec.presets_loaded_at = time.monotonic()
            rec.presets_key = pkey

    def _notify(self, device_id: str) -> None:
        if self._on_change:
            try:
                self._on_change(device_id)
            except Exception:
                log.exception("device change callback failed")

    # --------------------------------------------------------------- access
    def wrong_device(self, device_id: str) -> bool:
        """Whether another controller (another MAC) answers at this light's address."""
        return bool(self._get(device_id).wrong_device)

    def wrong_mac(self, device_id: str) -> str:
        """The MAC of the controller answering instead (see :meth:`wrong_device`)."""
        return self._get(device_id).wrong_mac

    @staticmethod
    def _check_identity(rec: DeviceRecord) -> None:
        if rec.wrong_device:
            raise DeviceError(f"{rec.cfg.get('name')}: {rec.wrong_device}")

    def _get(self, device_id: str) -> DeviceRecord:
        with self._lock:
            rec = self._devices.get(device_id)
        if rec is None:
            raise DeviceError(f"unknown device '{device_id}'")
        return rec

    def ids(self) -> List[str]:
        with self._lock:
            return list(self._devices)

    def has(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._devices

    def summary(self, device_id: str) -> Dict[str, Any]:
        rec = self._get(device_id)
        return self._summary(rec)

    def _summary(self, rec: DeviceRecord) -> Dict[str, Any]:
        st, info = _as_doc(rec.state), _as_doc(rec.info)
        segs = [s for s in (st.get("seg") or []) if isinstance(s, dict)] if isinstance(st.get("seg"), list) else []
        main_idx = st.get("mainseg", 0)
        main = next((s for s in segs if s.get("id") == main_idx), segs[0] if segs else {})
        leds = _as_doc(info.get("leds"))
        wifi = _as_doc(info.get("wifi"))
        return {
            "id": rec.cfg["id"],
            "name": rec.cfg.get("name"),
            "host": rec.cfg.get("host"),
            "enabled": rec.cfg.get("enabled", True),
            "icon": rec.cfg.get("icon", "bulb"),
            "color": rec.cfg.get("color", ""),
            # A disabled controller is not polled, so whatever it last said is
            # stale; it must not count as "on" in the All lights tile.
            "online": rec.online and rec.cfg.get("enabled", True),
            "last_error": rec.last_error if rec.cfg.get("enabled", True) else "disabled",
            "wrong_device": bool(rec.wrong_device),
            # Set when what answers instead is another of these lights.
            "wrong_owner": rec.wrong_owner if rec.wrong_device else "",
            "last_seen": rec.last_seen,
            "latency_ms": rec.latency_ms,
            "changed_at": rec.changed_at,
            "state": {
                "on": st.get("on"),
                "bri": st.get("bri"),
                "ps": st.get("ps"),
                "pl": st.get("pl"),
                "transition": st.get("transition"),
                "nl": st.get("nl"),
                "udpn": st.get("udpn"),
                "lor": st.get("lor"),
                "mainseg": main_idx,
                "seg_count": len(segs),
                "main": {
                    "id": main.get("id"),
                    "on": main.get("on"),
                    "bri": main.get("bri"),
                    "col": main.get("col"),
                    "fx": main.get("fx"),
                    "sx": main.get("sx"),
                    "ix": main.get("ix"),
                    "pal": main.get("pal"),
                    "cct": main.get("cct"),
                    "n": main.get("n"),
                },
            } if rec.state else None,
            "info": {
                "ver": info.get("ver"),
                "name": info.get("name"),
                "led_count": leds.get("count"),
                "fps": leds.get("fps"),
                "power_ma": leds.get("pwr"),
                "max_power_ma": leds.get("maxpwr"),
                "signal": wifi.get("signal"),
                "rssi": wifi.get("rssi"),
                "uptime": info.get("uptime"),
                "live": info.get("live"),
                "live_source": info.get("lm"),
                "arch": info.get("arch"),
                "mac": info.get("mac"),
                "ip": info.get("ip"),
            } if rec.info else None,
            "effect_name": _name_at(rec.effects, main.get("fx")),
            "palette_name": _name_at(rec.palettes, main.get("pal")),
            "preset_name": (rec.presets.get(str(st.get("ps"))) or {}).get("n") if st.get("ps", -1) not in (-1, None) else None,
        }

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            recs = list(self._devices.values())
        return [self._summary(r) for r in recs]

    def full(self, device_id: str) -> Dict[str, Any]:
        rec = self._get(device_id)
        base = self._summary(rec)
        base.update({
            "state_full": rec.state,
            "info_full": rec.info,
            "effects": rec.catalog,
            "palettes": _with_previews(palette_catalog(rec.palettes, rec.info or {}), rec.palette_data),
            "presets": _presets_list(rec.presets),
        })
        return base

    # -------------------------------------------------------------- control
    def set_state(self, device_id: str, payload: Dict[str, Any], *, quick: bool = False) -> Dict[str, Any]:
        """Send a state change.  ``quick`` = a short timeout, for trying a
        controller that is marked offline without holding everybody up."""
        rec = self._get(device_id)
        clean = sanitise_state(payload)
        self._check_identity(rec)
        with rec.lock:
            clean = self._prepare(rec, clean)
            try:
                new_state = rec.client.set_state(clean, timeout=(1.0, 2.0) if quick else None)
            except WLEDUnreachable as exc:
                self._mark_offline(rec, str(exc))
                raise DeviceError(f"{rec.cfg.get('name')} is not reachable ({exc})")
            except WLEDError as exc:
                raise DeviceError(str(exc))
            if "ps" in clean or "playlist" in clean or "np" in clean:
                # Preset/playlist application is asynchronous on the device.
                time.sleep(0.35)
                try:
                    new_state = rec.client.get_state()
                except WLEDError:
                    pass
            self._absorb(rec, new_state, None)
            if "psave" in clean or "pdel" in clean:
                time.sleep(0.8)  # presets.json is written on the next main-loop pass
                self._safe_reload_presets(rec)
            rec.next_poll = time.monotonic() + 1.0
        return self._summary(rec)

    def _prepare(self, rec: DeviceRecord, clean: Dict[str, Any]) -> Dict[str, Any]:
        """Make a sanitised payload do what was meant on *this* controller.

        * ``"on": "t"`` becomes an explicit on/off from the cached state, so a
          retry after WLED's "busy" answer can't toggle it straight back.
        * A preset number is sent the way WLED's own app sends it: the preset's
          content plus ``pd`` (mark as current).  A bare ``ps`` equal to the
          preset WLED thinks is still current is silently ignored – which is
          exactly the case after "Movie" → off → "Movie" again.
        * A segment object without an id only reaches *selected* segments, so
          it is addressed to every segment by id.
        """
        out = dict(clean)
        st = _as_doc(rec.state)
        if out.get("on") == "t" and isinstance(st.get("on"), bool):
            out["on"] = not st["on"]
        ps = out.get("ps")
        if isinstance(ps, int) and ps > 0 and not isinstance(ps, bool):
            body = rec.presets.get(str(ps)) if isinstance(rec.presets, dict) else None
            if isinstance(body, dict) and body:
                if "playlist" in body:
                    out.setdefault("on", True)  # what WLED's UI does for playlists
                elif "win" not in body:
                    preset = {k: v for k, v in body.items() if k not in ("n", "ql", "ps", "psave", "pdel", "o")}
                    rest = {k: v for k, v in out.items() if k != "ps"}
                    merged = {**preset, **rest, "pd": ps}
                    if isinstance(preset.get("seg"), list) and isinstance(rest.get("seg"), list):
                        # "Movie, but red": the preset's segments with the
                        # requested changes laid over them, segment by segment.
                        mine = {sg.get("id"): sg for sg in rest["seg"] if isinstance(sg, dict)}
                        segs = [{**sg, **mine.pop(sg.get("id"), {})} if isinstance(sg, dict) else sg for sg in preset["seg"]]
                        merged["seg"] = segs + list(mine.values())
                    out = merged
        seg = out.get("seg")
        if isinstance(seg, dict) and "id" not in seg:
            ids = [s.get("id") for s in (st.get("seg") or []) if isinstance(s, dict) and isinstance(s.get("id"), int)]
            if ids:
                out["seg"] = [{**seg, "id": i} for i in ids]
        return out

    def set_state_all(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply the same state to every enabled controller in parallel.

        Controllers marked offline are tried too (with a short timeout, at most
        every few seconds): "all off" should reach a light that only missed a
        couple of polls, and one that really is gone is reported, not skipped.
        """
        with self._lock:
            targets = [r for r in self._devices.values() if r.cfg.get("enabled", True)]
        results: Dict[str, Any] = {}
        ps = payload.get("ps") if isinstance(payload, dict) else None

        def _one(rec: DeviceRecord):
            did = rec.cfg["id"]
            if not rec.online and time.monotonic() - rec.last_try < OFFLINE_RETRY_S:
                results[did] = f"{rec.cfg.get('name')} is offline"
                return
            if isinstance(ps, int) and rec.presets and str(ps) not in rec.presets:
                results[did] = f"{rec.cfg.get('name')} has no preset #{ps}"
                return
            rec.last_try = time.monotonic()
            try:
                self.set_state(did, payload, quick=not rec.online)
                results[did] = "ok"
            except DeviceError as exc:
                results[did] = str(exc)

        try:
            futures = [self._cmd_pool.submit(_one, r) for r in targets]
        except RuntimeError:  # shutting down
            return results
        for f in futures:
            f.result()
        return results

    # ----------------------------------------------------- many controllers
    def view(self, device_id: str) -> "_intents.DeviceView":
        """A read-only picture of one controller for the intent planner."""
        rec = self._get(device_id)
        return self._view(rec)

    def _view(self, rec: DeviceRecord) -> "_intents.DeviceView":
        return _intents.DeviceView(
            name=str(rec.cfg.get("name") or rec.cfg.get("host")),
            online=rec.online,
            state=_as_doc(rec.state),
            info=_as_doc(rec.info),
            effects=rec.catalog,
            # Previews included so the all-lights panel can draw gradients.
            palettes=_with_previews(palette_catalog(rec.palettes, _as_doc(rec.info)),
                                    rec.palette_data if isinstance(rec.palette_data, dict) else {}),
            presets=rec.presets if isinstance(rec.presets, dict) else {},
        )

    def views(self, device_ids: List[str]) -> List["_intents.DeviceView"]:
        out = []
        for did in device_ids:
            try:
                out.append(self.view(did))
            except DeviceError:
                continue
        return out

    def apply_plans(self, plans: Dict[str, Dict[str, Any]], quick: Optional[set] = None) -> Dict[str, str]:
        """Send a different payload to each controller, all at once.

        ``plans`` maps device id to a WLED state payload.  Returns device id to
        ``"ok"`` or an error message.  Uses the command pool, never the poller's,
        so "everything red" never waits behind a poll of a switched-off strip.
        """
        results: Dict[str, str] = {}

        quick = quick or set()

        def _one(did: str, payload: Dict[str, Any]):
            try:
                self.set_state(did, payload, quick=did in quick)
                results[did] = "ok"
            except DeviceError as exc:
                results[did] = str(exc)
            except Exception as exc:  # pragma: no cover - defensive: never lose the others
                log.exception("applying to %s failed", did)
                results[did] = f"{type(exc).__name__}: {exc}"

        try:
            futures = [self._cmd_pool.submit(_one, did, payload) for did, payload in plans.items()]
        except RuntimeError:  # shutting down
            return {did: "JimboLED is shutting down" for did in plans}
        for f in futures:
            f.result()
        return results

    def apply_intent(self, device_ids: List[str], intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Apply one device-agnostic intent to several controllers.

        Every controller gets the payload that means the same thing for *it*
        (its own effect, palette and preset numbers, every segment, white and
        CCT only where it has them).  The result lists, per controller,
        ``status`` (``ok``, ``partial``, ``skipped``, ``offline`` or ``error``)
        and a plain message.
        """
        plans: Dict[str, Dict[str, Any]] = {}
        rows: Dict[str, Dict[str, Any]] = {}
        quick: set = set()
        for did in device_ids:
            try:
                rec = self._get(did)
            except DeviceError:
                continue
            view = self._view(rec)
            row = {"id": did, "name": view.name, "status": "ok", "notes": []}
            rows[did] = row
            if not rec.cfg.get("enabled", True):
                row.update(status="skipped", message="disabled")
                continue
            if not rec.online:
                # Missed a couple of polls?  Give it a quick try (not on every
                # slider tick, though) rather than leave it out.
                if time.monotonic() - rec.last_try < OFFLINE_RETRY_S:
                    row.update(status="offline", message=rec.last_error or "offline")
                    continue
                rec.last_try = time.monotonic()
                quick.add(did)
            payload, notes = _intents.plan(view, intent)
            row["notes"] = notes
            if payload is None:
                row.update(status="skipped", message="; ".join(notes))
                continue
            plans[did] = payload
        if len(plans) > 1:
            # Controllers with WLED's own sync turned on would otherwise each
            # re-broadcast this change and fight over it (double fades).
            for payload in plans.values():
                payload["udpn"] = {"nn": True}
        for did, outcome in self.apply_plans(plans, quick).items():
            row = rows[did]
            if outcome != "ok":
                row.update(status="offline" if did in quick else "error", message=outcome)
            elif row["notes"]:
                row.update(status="partial", message="; ".join(row["notes"]))
        return [rows[did] for did in device_ids if did in rows]

    def capture(self, device_ids: List[str], *, for_undo: bool = False, fresh: bool = False) -> List[Dict[str, Any]]:
        """Snapshot what each online controller is showing right now.

        ``fresh`` reads each controller first instead of trusting the last
        poll (which can be seconds old), in parallel.
        """
        recs = []
        for did in device_ids:
            try:
                rec = self._get(did)
            except DeviceError:
                continue
            if rec.online:
                recs.append(rec)

        def one(rec: DeviceRecord):
            view = self._view(rec)
            if fresh:
                try:
                    view.state = _as_doc(rec.client.get_state()) or view.state
                except WLEDError:
                    pass
            snap = _intents.snapshot(view, nightlight=for_undo)
            return {"id": rec.cfg["id"], "name": rec.cfg.get("name"), "state": snap} if snap else None

        if fresh and len(recs) > 1:
            try:
                rows = [f.result() for f in [self._cmd_pool.submit(one, r) for r in recs]]
            except RuntimeError:  # shutting down
                rows = []
        else:
            rows = [one(r) for r in recs]
        return [r for r in rows if r]

    def restore(self, snapshots: List[Dict[str, Any]], *, remove_new_segments: bool = False) -> Dict[str, str]:
        """Put controllers back to snapshots taken by :meth:`capture` (undo).

        ``remove_new_segments``: also delete segments that are there now but
        weren't in the snapshot.  Only a preset can add segments, and only a
        snapshot read fresh from the controller (``capture(fresh=True)``) can
        tell "added since" from "the poll had not seen it yet".
        """
        plans, wanted_main = {}, {}
        for snap in snapshots:
            try:
                rec = self._get(snap["id"])
            except DeviceError:
                continue
            plans[snap["id"]] = restore_payload(snap["state"], _as_doc(rec.state) if remove_new_segments else None)
            wanted_main[snap["id"]] = snap["state"].get("mainseg")
        if len(plans) > 1:
            for payload in plans.values():
                payload["udpn"] = {"nn": True}  # no sync echo between the lights being put back
        results = self.apply_plans(plans)
        for did, outcome in results.items():
            if outcome == "ok":
                rec = self._get(did)
                with rec.lock:
                    self._fix_mainseg(rec, {"mainseg": wanted_main.get(did)}, _as_doc(rec.state))
        return results

    def copy_preset(self, source_id: str, slot: int, target_ids: List[str], *, overwrite: bool = True) -> List[Dict[str, Any]]:
        """Store one controller's preset on others, under the same name.

        Lands in the slot that already holds a preset of that name, else the
        same slot number if it is free, else the first free slot.
        """
        portable = _intents.portable_preset(self.view(source_id), int(slot))
        rows: List[Dict[str, Any]] = []
        for did in target_ids:
            if did == source_id:
                continue
            try:
                rec = self._get(did)
            except DeviceError:
                continue
            row: Dict[str, Any] = {"id": did, "name": rec.cfg.get("name"), "status": "ok"}
            rows.append(row)
            if not rec.online:
                row.update(status="offline", message=rec.last_error or "offline")
                continue
            # Choose the slot from the controller's presets as they are *now*:
            # a cached list that has not loaded yet looks empty, and slot 4
            # would be overwritten whatever it holds.
            try:
                rec.presets = rec.client.get_presets()
                rec.presets_loaded_at = time.monotonic()
            except WLEDError as exc:
                row.update(status="error", message=f"could not read its presets ({exc})")
                continue
            view = self._view(rec)
            try:
                state, notes = _intents.preset_state_for(view, portable)
                state = sanitise_state(state)
            except (_intents.IntentError, DeviceError) as exc:
                row.update(status="error", message=str(exc))
                continue
            used = {int(k) for k, v in view.presets.items() if isinstance(v, dict) and v and str(k).isdigit()}
            existing = view.preset_slot(portable["name"])
            if existing is not None:
                if not overwrite:
                    row.update(status="skipped", message=f"already has “{portable['name']}”")
                    continue
                target_slot = existing
            elif int(slot) not in used:
                target_slot = int(slot)
            else:
                target_slot = next((i for i in range(1, 251) if i not in used), None)
                if target_slot is None:
                    row.update(status="error", message="its 250 preset slots are all in use")
                    continue
            try:
                self.save_preset(did, target_slot, portable["name"], quick_label=portable.get("quick_label", ""),
                                 state=state)
            except PutBackError as exc:
                notes = [*notes, str(exc)]  # copied all the same
            except DeviceError as exc:
                row.update(status="error", message=str(exc))
                continue
            row["slot"] = target_slot
            if notes:
                row.update(status="partial", message="; ".join(notes))
        return rows

    def save_preset(self, device_id: str, slot: int, name: str, **kwargs) -> List[Dict[str, Any]]:
        """Save a preset and wait until the controller has really stored it.

        With ``state=`` (a given look rather than the live one) the lights are
        put back afterwards: WLED *applies* such a document to the strip – and
        starts a playlist in it – before it stores it, so renaming or copying a
        preset would otherwise change the lights.  If they can't be put back,
        :class:`PutBackError` says so (the preset itself was saved).
        """
        rec = self._get(device_id)
        content = kwargs.get("state")
        self._check_identity(rec)
        with rec.lock:
            prior = (rec.presets or {}).get(str(int(slot))) if isinstance(rec.presets, dict) else None
            if not content:
                try:
                    rec.client.save_preset(slot, name, **kwargs)
                except WLEDError as exc:
                    raise DeviceError(str(exc))
                self._wait_for_preset(rec, slot, name, prior=prior)
                return _presets_list(rec.presets)
            playlist = "playlist" in content
            if playlist and _as_doc(rec.info).get("live") and not _as_doc(rec.state).get("lor"):
                # WLED writes a playlist from the one it is playing, and it
                # doesn't do that while another app streams to the strip.
                raise DeviceError(f"{rec.cfg.get('name')} is showing another app's stream; "
                                  "stop that (or tap Take over) and try again")
            try:
                before = rec.client.get_state()
            except WLEDError as exc:
                raise DeviceError(f"{rec.cfg.get('name')}: could not read how it looks first ({exc})")
            failure: Optional[WLEDError] = None
            try:
                rec.client.save_preset(slot, name, **kwargs)
            except WLEDError as exc:
                # A timeout or a lost reply can come *after* WLED applied the
                # look (and even stored it), so the lights are put back anyway.
                failure = exc
            stored = self._wait_for_preset(rec, slot, name, playlist=playlist, prior=prior)
            if playlist and not stored:
                # Putting the lights back now would make WLED store an empty
                # playlist; leave it playing rather than lose it.
                raise DeviceError(f"{rec.cfg.get('name')} hasn't stored the playlist yet, so it was left playing; "
                                  "check its presets in a moment")
            problem = self._put_back(rec, before, content, playlist=playlist)
            if failure is not None and not stored:
                raise DeviceError(f"{rec.cfg.get('name')} did not confirm the save ({failure})"
                                  + ("" if problem else "; its lights were put back as they were"))
            if problem:
                raise PutBackError(f"saved, but {rec.cfg.get('name')}'s lights could not be put back as they were "
                                   f"({problem})", presets=_presets_list(rec.presets))
        return _presets_list(rec.presets)

    def _wait_for_preset(self, rec: DeviceRecord, slot: int, name: str, *, playlist: bool = False,
                         prior: Any = None, timeout: float = 4.0) -> bool:
        """Poll presets.json until ``slot`` holds ``name``.  Caller holds the lock.

        ``prior`` is what the slot held before: overwriting a preset under its
        own name only counts once the entry changed (or after the second
        WLED usually needs to write it, if the new one happens to be identical).
        """
        want = fit_name(name)
        start = time.monotonic()
        deadline = start + timeout
        time.sleep(0.25)
        while True:
            try:
                presets = rec.client.get_presets()
                rec.presets, rec.presets_loaded_at = presets, time.monotonic()
                entry = presets.get(str(int(slot))) or {}
                fresh = entry != prior or time.monotonic() - start >= 1.0
                if entry.get("n") == want and (not playlist or entry.get("playlist")) and fresh:
                    return True
            except WLEDError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.4)

    def _put_back(self, rec: DeviceRecord, before: Dict[str, Any], content: Dict[str, Any], *,
                  playlist: bool = False) -> Optional[str]:
        """Restore ``before`` (caller holds ``rec.lock``).  Returns a problem, or None."""
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                now: Optional[Dict[str, Any]] = rec.client.get_state()
            except WLEDError as exc:
                last, now = exc, None
            if now is None:
                # Can't see what the save left behind: assume every segment it
                # named exists, so the ones before didn't have are removed.
                ids = {sg.get("id") for sg in (before.get("seg") or []) if isinstance(sg, dict)}
                segs = content.get("seg") if isinstance(content.get("seg"), list) else []
                ids |= {sg.get("id", i) for i, sg in enumerate(segs) if isinstance(sg, dict)}
                now = {"seg": [{"id": i} for i in ids if isinstance(i, int)]}
            payload = restore_payload(before, now)
            if isinstance(before.get("transition"), int):
                payload["transition"] = before["transition"]
            was_pl, was_ps = before.get("pl"), before.get("ps")
            if isinstance(was_pl, int) and was_pl > 0:
                payload["ps"] = was_pl                 # a playlist was running: start it again
            elif playlist:
                payload["playlist"] = {}               # stop the one the save started
            elif isinstance(was_ps, int) and was_ps > 0:
                payload["pd"] = was_ps                 # it was showing a preset: say so again
            try:
                result = rec.client.set_state(payload)
                self._absorb(rec, result, None)
                self._fix_mainseg(rec, before, result)
                return None
            except WLEDError as exc:
                last = exc
                time.sleep(0.3 * (attempt + 1))
        log.warning("%s: could not put the lights back after saving a preset: %s", rec.cfg.get("name"), last)
        return str(last)

    def _fix_mainseg(self, rec: DeviceRecord, before: Dict[str, Any], result: Any) -> None:
        """WLED resets the main segment when it purges or re-creates segments;
        set it again if it came back different."""
        want = before.get("mainseg")
        got = result.get("mainseg") if isinstance(result, dict) else None
        if isinstance(want, int) and isinstance(got, int) and want != got:
            try:
                self._absorb(rec, rec.client.set_state({"mainseg": want}), None)
            except WLEDError:
                pass

    def delete_preset(self, device_id: str, slot: int) -> List[Dict[str, Any]]:
        rec = self._get(device_id)
        self._check_identity(rec)
        with rec.lock:
            try:
                rec.client.delete_preset(slot)
            except WLEDError as exc:
                raise DeviceError(str(exc))
            time.sleep(0.8)
            self._safe_reload_presets(rec)
        return _presets_list(rec.presets)

    def _safe_reload_presets(self, rec: DeviceRecord) -> None:
        try:
            rec.presets = rec.client.get_presets()
            rec.presets_loaded_at = time.monotonic()
        except WLEDError as exc:
            log.warning("could not reload presets for %s: %s", rec.cfg.get("name"), exc)

    def reload_catalog(self, device_id: str) -> Dict[str, Any]:
        rec = self._get(device_id)
        self._check_identity(rec)  # the other controller's effects aren't this light's
        try:
            self._load_catalog(rec, force=True)
        except WLEDError as exc:
            raise DeviceError(str(exc))
        return self.full(device_id)

    def identify(self, device_id: str, seconds: float = 3.0) -> None:
        """Blink a controller white for a few seconds, then put it back as it was."""
        rec = self._get(device_id)
        self._check_identity(rec)
        view = self._view(rec)
        identify_client(rec.client, seconds, blink_fx=view.effect_id("Blink"), lock=rec.lock)
        rec.next_poll = time.monotonic() + seconds + 0.5

    def rename_preset(self, device_id: str, slot: int, name: str, quick_label: str = "") -> List[Dict[str, Any]]:
        """Rename a preset *keeping its content* (a plain ``psave`` would save
        whatever the lights show right now into it, and destroy a playlist)."""
        rec = self._get(device_id)
        body = rec.presets.get(str(slot)) if isinstance(rec.presets, dict) else None
        if not isinstance(body, dict) or not body:
            raise DeviceError(f"{rec.cfg.get('name')} has no preset #{slot}")
        if "win" in body:
            # Saving it again would also *run* its HTTP-API command on the spot.
            raise DeviceError("this preset runs a WLED HTTP command; rename it on the controller's own page")
        content = {k: v for k, v in body.items() if k not in ("n", "ql", "psave", "pdel", "o", "v")}
        if not content:
            raise DeviceError("that preset is empty")
        return self.save_preset(device_id, slot, name, quick_label=quick_label, state=content)

    def reboot(self, device_id: str) -> None:
        rec = self._get(device_id)
        self._check_identity(rec)
        with rec.lock:
            rec.client.reboot()
            rec.next_poll = time.monotonic() + 5.0

    def presets(self, device_id: str) -> List[Dict[str, Any]]:
        return _presets_list(self._get(device_id).presets)

    def client_for(self, device_id: str) -> WLEDClient:
        return self._get(device_id).client


def restore_payload(before: Dict[str, Any], now: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A state payload that turns ``now`` back into ``before``.

    Segment geometry, colours and effects come from ``before`` (in the shape
    :func:`intents.snapshot` makes), the sleep timer if it was recorded, and
    any segment that exists in ``now`` but not in ``before`` is deleted.
    """
    view = _intents.DeviceView(name="", online=True, state=_as_doc(before), info={}, effects=[], palettes=[], presets={})
    out = _intents.snapshot(view, nightlight=True) or {}
    if "nl" not in out:
        out["nl"] = {"on": False}  # nothing was fading before: stop anything since
    keep = {sg.get("id") for sg in out.get("seg", []) if isinstance(sg, dict)}
    for sg in (_as_doc(now).get("seg") or []):
        if isinstance(sg, dict) and isinstance(sg.get("id"), int) and sg["id"] not in keep:
            out.setdefault("seg", []).append({"id": sg["id"], "stop": 0})
    return out


_identifying: Dict[str, Dict[str, Any]] = {}
_identifying_lock = threading.Lock()
_identify_hosts: Dict[str, threading.Lock] = {}


def _identify_lock(host: str) -> threading.Lock:
    """One identify at a time per controller (and its put-back timer)."""
    with _identifying_lock:
        return _identify_hosts.setdefault(host, threading.Lock())


def identify_client(client: WLEDClient, seconds: float = 3.0, *, blink_fx: Optional[int] = None,
                    lock: Optional[threading.Lock] = None) -> None:
    """Make a controller blink so a person can see which one it is.

    The current state is read first and restored afterwards (on a timer, so
    the request returns straight away).  Raises :class:`DeviceError` if the
    controller does not answer.  Pressing it again while it blinks keeps it
    blinking a little longer and still puts back the look from before.
    """
    guard = lock or threading.Lock()
    key = client.host
    with _identify_lock(key):
        with _identifying_lock:
            pending = _identifying.get(key)
        with guard:
            try:
                if pending:
                    # Put back the look from *before* the first blink – never the blink.
                    before, segs = pending["before"], pending["segs"]
                else:
                    before = client.get_state()
                    segs = [s for s in (before.get("seg") or [])
                            if isinstance(s, dict) and isinstance(s.get("id"), int)]
                blink = {"id": 0, "fx": 1 if blink_fx is None else blink_fx, "sx": 230, "ix": 128,
                         "col": [[255, 255, 255], [0, 0, 0]]}
                client.set_state({"on": True, "bri": 255, "tt": 0,
                                  "seg": [{**blink, "id": s["id"]} for s in segs] or blink}, verbose=False)
            except WLEDError as exc:
                # A blink already running keeps its own timer, which still
                # puts the lights back.
                raise DeviceError(f"could not reach it ({exc})")
        restore: Dict[str, Any] = {k: before[k] for k in ("on", "bri") if k in before}
        restore["tt"] = 0
        restore["seg"] = [{k: s[k] for k in ("id", "fx", "sx", "ix", "col", "pal", "c1", "c2", "c3") if k in s}
                          for s in segs]
        if not restore["seg"]:
            restore.pop("seg")

        def _put_back():
            with _identify_lock(key):
                with _identifying_lock:
                    if _identifying.get(key, {}).get("timer") is not timer:
                        return  # a later identify took over
                    _identifying.pop(key, None)
                with guard:
                    try:
                        client.set_state(restore, verbose=False)
                    except WLEDError:
                        log.info("could not restore %s after identifying it", client.host)

        timer = threading.Timer(max(1.0, min(10.0, seconds)), _put_back)
        timer.daemon = True
        with _identifying_lock:
            if pending:
                pending["timer"].cancel()
            _identifying[key] = {"before": before, "segs": segs, "timer": timer}
        timer.start()


# How often a controller marked offline is tried again by a command.
OFFLINE_RETRY_S = 10.0


def _as_doc(value: Any) -> Dict[str, Any]:
    """A JSON object we can safely ``.get()`` from, whatever the device sent."""
    return value if isinstance(value, dict) else {}


def _presets_key(info: Dict[str, Any]) -> str:
    """``pmt|boot_time`` – changes when presets.json is rewritten or the device reboots."""
    fs = _as_doc(info.get("fs"))
    try:
        boot = int(time.time() - float(info.get("uptime") or 0))
    except (TypeError, ValueError):
        boot = 0
    return f"{fs.get('pmt')}|{boot}"


def _presets_changed(old: str, new: str, tolerance_s: int = 5) -> bool:
    if not old:
        return True
    try:
        old_pmt, old_boot = old.split("|")
        new_pmt, new_boot = new.split("|")
        return old_pmt != new_pmt or abs(int(new_boot) - int(old_boot)) > tolerance_s
    except ValueError:
        return old != new


def _name_at(names: List[str], idx: Any) -> Optional[str]:
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(names):
        return names[idx].split("@", 1)[0]
    return None


def _with_previews(palettes: List[Dict[str, Any]], data: Dict[int, Any]) -> List[Dict[str, Any]]:
    for pal in palettes:
        stops = data.get(pal["id"])
        if stops:
            pal["stops"] = stops
    return palettes


def _presets_list(presets: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for key, val in presets.items():
        try:
            slot = int(key)
        except ValueError:
            continue
        out.append({
            "id": slot,
            "name": val.get("n") or f"Preset {slot}",
            "quick_label": val.get("ql", ""),
            "is_playlist": "playlist" in val,
            "on": val.get("on"),
            "bri": val.get("bri"),
        })
    out.sort(key=lambda p: p["id"])
    return out
