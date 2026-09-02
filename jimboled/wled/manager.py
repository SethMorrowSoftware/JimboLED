"""Device registry and background poller for WLED controllers.

One :class:`DeviceManager` owns every controller.  A poller thread refreshes
each device's state on a schedule (fast when online, slow when offline) using
a small worker pool so one dead controller never delays the others.  User
commands go through :meth:`set_state`, which updates the cache immediately
from WLED's verbose response so the UI never shows stale values.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .client import WLEDClient, WLEDError, WLEDUnreachable, normalise_host
from .fxdata import build_effect_catalog, palette_catalog

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


@dataclass
class DeviceRecord:
    cfg: Dict[str, Any]
    client: WLEDClient
    lock: threading.Lock = field(default_factory=threading.Lock)
    online: bool = False
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
            out[key] = value if value == "t" else bool(value)
        elif key in ("nl", "udpn", "playlist"):
            if not isinstance(value, dict):
                raise DeviceError(f"{key} must be an object")
            out[key] = value
        else:
            out[key] = value
    if not out:
        raise DeviceError("nothing to change")
    return out


_VALUE_STRING = re.compile(r"^[~w]?-?\d{0,4}~?\d{0,4}~?r?$")


def _clean_segment(seg: Any) -> Dict[str, Any]:
    if not isinstance(seg, dict):
        raise DeviceError("segment must be an object")
    clean = {k: v for k, v in seg.items() if k in ALLOWED_SEG_KEYS}
    if "col" in clean:
        clean["col"] = _clean_colors(clean["col"])
    if "n" in clean:
        clean["n"] = str(clean["n"])[:32]
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
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)
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
                        client = WLEDClient(cfg["host"], timeout=self._settings["timeout"])
                    except WLEDError as exc:
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
                    return  # pool shut down

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
        with rec.lock:
            t0 = time.monotonic()
            try:
                doc = rec.client.get_state_info()
                rec.latency_ms = round((time.monotonic() - t0) * 1000, 1)
                self._absorb(rec, doc["state"], doc["info"])
                self._ensure_catalog(rec)
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
        return rec

    def _absorb(self, rec: DeviceRecord, state: Dict[str, Any], info: Optional[Dict[str, Any]]) -> None:
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
            rec.effects = rec.client.get_effects()
            rec.palettes = rec.client.get_palettes()
            rec.fxdata = rec.client.get_fxdata()
            rec.catalog = build_effect_catalog(rec.effects, rec.fxdata)
            rec.catalog_key = key
            try:
                rec.palette_data = rec.client.get_palette_data()
            except WLEDError:
                rec.palette_data = {}
            rec.presets = rec.client.get_presets()
            rec.presets_loaded_at = time.monotonic()
            rec.presets_key = _presets_key(info)
            return
        # presets.json changed (mtime) or the device rebooted -> reload presets
        pkey = _presets_key(info)
        if pkey != rec.presets_key or time.monotonic() - rec.presets_loaded_at > PRESET_REFRESH_S:
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
        st, info = rec.state or {}, rec.info or {}
        segs = st.get("seg") or []
        main_idx = st.get("mainseg", 0)
        main = next((s for s in segs if s.get("id") == main_idx), segs[0] if segs else {})
        leds = info.get("leds") or {}
        wifi = info.get("wifi") or {}
        return {
            "id": rec.cfg["id"],
            "name": rec.cfg.get("name"),
            "host": rec.cfg.get("host"),
            "enabled": rec.cfg.get("enabled", True),
            "icon": rec.cfg.get("icon", "bulb"),
            "color": rec.cfg.get("color", ""),
            "online": rec.online,
            "last_error": rec.last_error,
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
    def set_state(self, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        rec = self._get(device_id)
        clean = sanitise_state(payload)
        with rec.lock:
            try:
                new_state = rec.client.set_state(clean)
            except WLEDUnreachable as exc:
                self._mark_offline(rec, str(exc))
                raise DeviceError(f"{rec.cfg.get('name')} is not reachable ({exc})")
            except WLEDError as exc:
                raise DeviceError(str(exc))
            if "ps" in clean or "playlist" in clean or "pd" in clean or "np" in clean:
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

    def set_state_all(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply the same state to every online, enabled device in parallel."""
        with self._lock:
            targets = [r for r in self._devices.values() if r.online and r.cfg.get("enabled", True)]
        results: Dict[str, Any] = {}

        def _one(rec: DeviceRecord):
            try:
                self.set_state(rec.cfg["id"], payload)
                results[rec.cfg["id"]] = "ok"
            except DeviceError as exc:
                results[rec.cfg["id"]] = str(exc)

        futures = [self._pool.submit(_one, r) for r in targets]
        for f in futures:
            f.result()
        return results

    def save_preset(self, device_id: str, slot: int, name: str, **kwargs) -> Dict[str, Any]:
        rec = self._get(device_id)
        with rec.lock:
            try:
                rec.client.save_preset(slot, name, **kwargs)
            except WLEDError as exc:
                raise DeviceError(str(exc))
            time.sleep(0.8)  # WLED writes presets.json asynchronously
            self._safe_reload_presets(rec)
        return _presets_list(rec.presets)

    def delete_preset(self, device_id: str, slot: int) -> List[Dict[str, Any]]:
        rec = self._get(device_id)
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
        with rec.lock:
            try:
                self._ensure_catalog(rec, force=True)
            except WLEDError as exc:
                raise DeviceError(str(exc))
        return self.full(device_id)

    def reboot(self, device_id: str) -> None:
        rec = self._get(device_id)
        with rec.lock:
            rec.client.reboot()
            rec.next_poll = time.monotonic() + 5.0

    def presets(self, device_id: str) -> List[Dict[str, Any]]:
        return _presets_list(self._get(device_id).presets)

    def client_for(self, device_id: str) -> WLEDClient:
        return self._get(device_id).client


def _presets_key(info: Dict[str, Any]) -> str:
    """Changes when presets.json is rewritten or the device reboots (pmt resets to 0)."""
    fs = info.get("fs") or {}
    boot = int(time.time() - float(info.get("uptime") or 0)) // 5  # 5 s tolerance
    return f"{fs.get('pmt')}|{boot}"


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
