"""Thin HTTP client for the WLED JSON API.

Only the parts we need, but written to tolerate every WLED release since
0.13: ``/json/si`` (state+info in one round trip) is tried first and the
older ``/json`` document is used as a fallback.  Every call has a short
timeout so a powered-off controller never stalls the dashboard.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

HOST_RE = re.compile(r"^[A-Za-z0-9.\-_:\[\]]+$")


class WLEDError(Exception):
    """Any failure talking to a controller."""


class WLEDUnreachable(WLEDError):
    """Timeout / connection refused / DNS failure."""


class WLEDBusy(WLEDError):
    """Device answered 503: its single JSON buffer is in use."""


def normalise_host(host: str) -> str:
    """Accept ``192.168.1.5``, ``wled-abc.local``, ``http://x``, ``x:8080``."""
    host = (host or "").strip()
    if not host:
        raise WLEDError("host is required")
    if "://" in host:
        parts = urlsplit(host)
        host = parts.netloc or parts.path
    host = host.strip("/")
    if "/" in host:
        host = host.split("/", 1)[0]
    if not HOST_RE.match(host):
        raise WLEDError(f"'{host}' is not a valid address")
    return host


class WLEDClient:
    def __init__(self, host: str, timeout: float = 4.0, connect_timeout: float = 2.0):
        self.host = normalise_host(host)
        self.base = f"http://{self.host}"
        self.timeout = (connect_timeout, timeout)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "JimboLED"
        # WLED is single-threaded; never hammer it with parallel requests.
        adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=0)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + path
        data = json.dumps(payload, separators=(",", ":")) if method != "GET" else None
        resp = None
        for attempt in range(3):
            try:
                if method == "GET":
                    resp = self.session.get(url, timeout=self.timeout)
                else:
                    resp = self.session.post(url, data=data, timeout=self.timeout,
                                             headers={"Content-Type": "application/json"})
            except (requests.ConnectionError, requests.Timeout, socket.gaierror) as exc:
                raise WLEDUnreachable(_short_error(exc)) from exc
            except requests.RequestException as exc:
                raise WLEDError(_short_error(exc)) from exc
            # WLED has a single JSON buffer; it answers 503 {"error":3} when busy.
            if resp.status_code == 503 and attempt < 2:
                time.sleep(0.25 * (attempt + 1))
                continue
            break
        if resp.status_code == 404:
            raise WLEDError(f"{path} not found on this device (old firmware?)")
        if resp.status_code == 503:
            raise WLEDBusy("device busy, try again")
        if resp.status_code >= 400:
            raise WLEDError(f"HTTP {resp.status_code} from {path}: {_error_text(resp)}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise WLEDError(f"invalid JSON from {path}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: Dict[str, Any]) -> Any:
        return self._request("POST", path, payload)

    # --------------------------------------------------------------- reads
    def get_state_info(self) -> Dict[str, Any]:
        """Return ``{"state": ..., "info": ...}`` using the cheapest endpoint."""
        try:
            doc = self.get("/json/si")
            if isinstance(doc, dict) and "state" in doc and "info" in doc:
                return {"state": doc["state"], "info": doc["info"]}
        except WLEDError as exc:
            if isinstance(exc, (WLEDUnreachable, WLEDBusy)):
                raise
            log.debug("%s: /json/si unavailable (%s), using /json", self.host, exc)
        doc = self.get("/json")
        if not isinstance(doc, dict) or "state" not in doc:
            raise WLEDError("unexpected response from /json")
        return {"state": doc["state"], "info": doc.get("info", {})}

    def get_all(self) -> Dict[str, Any]:
        """Full ``/json`` document: state, info, effects, palettes."""
        doc = self.get("/json")
        if not isinstance(doc, dict) or "state" not in doc:
            raise WLEDError("unexpected response from /json")
        return doc

    def get_state(self) -> Dict[str, Any]:
        return self.get("/json/state")

    def get_info(self) -> Dict[str, Any]:
        return self.get("/json/info")

    def get_effects(self) -> List[str]:
        eff = self.get("/json/eff")
        return [str(e) for e in eff] if isinstance(eff, list) else []

    def get_palettes(self) -> List[str]:
        pal = self.get("/json/pal")
        return [str(p) for p in pal] if isinstance(pal, list) else []

    def get_fxdata(self) -> List[str]:
        try:
            data = self.get("/json/fxdata")
        except WLEDError as exc:
            if isinstance(exc, WLEDUnreachable):
                raise
            return []
        return [str(d) for d in data] if isinstance(data, list) else []

    def get_presets(self) -> Dict[str, Dict[str, Any]]:
        """Presets file as ``{"1": {...}, ...}`` (slot 0 is always empty)."""
        try:
            doc = self.get("/presets.json")
        except WLEDError as exc:
            if isinstance(exc, WLEDUnreachable):
                raise
            return {}
        if not isinstance(doc, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for key, val in doc.items():
            if key == "0" or not isinstance(val, dict) or not val:
                continue
            out[str(key)] = val
        return out

    def get_nodes(self) -> List[Dict[str, Any]]:
        try:
            doc = self.get("/json/nodes")
        except WLEDError:
            return []
        nodes = doc.get("nodes") if isinstance(doc, dict) else None
        return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []

    def get_config(self) -> Dict[str, Any]:
        doc = self.get("/json/cfg")
        return doc if isinstance(doc, dict) else {}

    # -------------------------------------------------------------- writes
    def set_state(self, payload: Dict[str, Any], *, verbose: bool = True) -> Dict[str, Any]:
        """POST a (partial) state.  Returns the new full state when ``verbose``."""
        body = dict(payload)
        if verbose:
            body["v"] = True
        result = self.post("/json/state", body)
        if verbose and isinstance(result, dict) and "on" in result:
            return result
        if isinstance(result, dict) and result.get("success") is False:
            raise WLEDError("device rejected the command")
        if verbose:
            return self.get_state()
        return result if isinstance(result, dict) else {}

    def save_preset(self, slot: int, name: str, *, include_brightness: bool = True,
                    save_segment_bounds: bool = True, quick_label: str = "",
                    state: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"psave": int(slot), "n": name[:32],
                                   "ib": bool(include_brightness), "sb": bool(save_segment_bounds)}
        if quick_label:
            payload["ql"] = quick_label[:2]
        if state:
            payload.update(state)
            payload["o"] = True  # overwrite: save the supplied state, not the current one
        self.post("/json/state", payload)

    def delete_preset(self, slot: int) -> None:
        self.post("/json/state", {"pdel": int(slot)})

    def apply_preset(self, slot: int) -> Dict[str, Any]:
        return self.set_state({"ps": int(slot)})

    def reboot(self) -> None:
        try:
            self.post("/json/state", {"rb": True})
        except WLEDUnreachable:
            pass  # the device drops the connection while rebooting

    def ping(self) -> float:
        """Round-trip time in ms for a tiny request (raises when offline)."""
        t0 = time.monotonic()
        self.get("/json/info")
        return (time.monotonic() - t0) * 1000

    def close(self) -> None:
        self.session.close()


WLED_ERRORS = {
    1: "access denied (settings PIN)", 2: "device busy", 3: "device busy", 4: "not supported by this firmware",
    7: "not enough memory for LEDs", 8: "effect memory exhausted", 9: "request too large or invalid",
    10: "filesystem error", 11: "filesystem full", 12: "preset does not exist", 19: "filesystem error",
    33: "low memory", 34: "low segment memory",
}


def _error_text(resp) -> str:
    try:
        doc = resp.json()
        code = int(doc.get("error"))
        return WLED_ERRORS.get(code, f"error {code}")
    except Exception:
        return resp.text[:120]


def _short_error(exc: Exception) -> str:
    text = str(exc)
    if "Max retries" in text or "NewConnectionError" in text:
        return "connection failed"
    if "timed out" in text.lower():
        return "timed out"
    if "Name or service not known" in text or "nodename nor servname" in text:
        return "hostname not found"
    return text[:120] or type(exc).__name__
