"""Phone notifications: tell somebody when something needs their attention.

Nobody watches a dashboard, so a few events are pushed to a URL instead:

* ``estop``     – an emergency stop was engaged (or reset)
* ``relay``     – a relay was released by its time limit or a lost hold
* ``schedule``  – a schedule or timer failed
* ``offline``   – a light has been offline for 10 minutes (and when it is back)

The URL is either an `ntfy <https://ntfy.sh>`_ topic (``https://ntfy.sh/<topic>``
or a self-hosted server – it gets a plain-text message with a title, which
the ntfy phone app shows as a notification) or any other webhook, which gets
JSON ``{"title", "message", "event", "ts"}`` (Home Assistant, Node-RED …).

Sending happens on a background thread; callers only enqueue, so a slow or
missing internet connection can never hold up a relay or a light.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

EVENTS = ("estop", "relay", "schedule", "offline")
OFFLINE_AFTER_S = 600.0
REPEAT_AFTER_S = 600.0     # the same thing is not announced twice within this
QUEUE_MAX = 50


def clean_settings(raw: Any) -> Dict[str, Any]:
    """Validate ``{"url", "events": [...]}``; raises ValueError with a plain message."""
    raw = raw if isinstance(raw, dict) else {}
    url = str(raw.get("url") or "").strip()
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("the address must start with https:// (for example https://ntfy.sh/my-jimboled-topic)")
        if len(url) > 300:
            raise ValueError("that address is too long")
    events = raw.get("events")
    if not isinstance(events, list):
        events = list(EVENTS)
    return {"url": url, "events": [e for e in EVENTS if e in events]}


class Notifier:
    def __init__(self, store, devices=None):
        self.store = store
        self.devices = devices
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._last: Dict[str, float] = {}
        self._offline_since: Dict[str, float] = {}
        self._announced_offline: set = set()
        self._stop = threading.Event()
        self._session = requests.Session()
        self.last_result: Dict[str, Any] = {}

    # -------------------------------------------------------------- inputs
    def settings(self) -> Dict[str, Any]:
        try:
            return clean_settings(self.store.section("notify"))
        except ValueError:
            return {"url": "", "events": []}

    def send(self, event: str, title: str, message: str, *, key: Optional[str] = None, force: bool = False) -> bool:
        """Queue a notification (never blocks).  Returns whether it was queued."""
        cfg = self.settings()
        if not cfg["url"] or (event not in cfg["events"] and not force):
            return False
        key = key or f"{event}|{title}"
        now = time.monotonic()
        if not force and now - self._last.get(key, -1e9) < REPEAT_AFTER_S:
            return False
        self._last[key] = now
        try:
            self._q.put_nowait({"event": event, "title": title, "message": message, "url": cfg["url"], "ts": time.time()})
            return True
        except queue.Full:
            log.warning("notification queue full; dropping '%s'", title)
            return False

    def gpio_event(self, evt: Dict[str, Any]) -> None:
        """Hooked into the GPIO manager's event callback (may run under its lock)."""
        if not isinstance(evt, dict):
            return
        action, name, reason = evt.get("action"), evt.get("name") or "", evt.get("reason") or ""
        if action == "estop-engaged":
            self.send("estop", f"Emergency stop: {name}",
                      f"{name} was stopped ({reason or 'manual'}). Its relays stay off until somebody resets it.",
                      force=False, key=f"estop|{name}|{time.time()}")
        elif action == "estop-reset":
            self.send("estop", f"Emergency stop reset: {name}", f"{name} was unlocked.", key=f"estop-reset|{name}|{time.time()}")
        elif action == "off" and reason in ("max-on-time", "heartbeat-timeout"):
            why = "its time limit was reached" if reason == "max-on-time" else "the phone holding it lost contact"
            self.send("relay", f"{name} switched off", f"{name} was switched off because {why}.")

    def schedule_result(self, sched: Dict[str, Any], result: Dict[str, Any]) -> None:
        if result.get("error"):
            name = sched.get("name") or "A schedule"
            self.send("schedule", f"{name} didn't work", str(result["error"])[:300], key=f"schedule|{sched.get('id')}")

    # ------------------------------------------------------------- runtime
    def start(self) -> None:
        threading.Thread(target=self._sender, name="notify", daemon=True).start()
        threading.Thread(target=self._watch_offline, name="notify-offline", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _sender(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            self.last_result = self.deliver(item)

    def deliver(self, item: Dict[str, Any]) -> Dict[str, Any]:
        url = item["url"]
        try:
            host = urlsplit(url).netloc.lower()
            if "ntfy" in host:
                # ntfy wants a plain body; headers must be latin-1, so the
                # title goes in as RFC 2047-free ASCII where it can.
                title = item["title"].encode("ascii", "replace").decode("ascii")
                resp = self._session.post(url, data=item["message"].encode("utf-8"), timeout=8,
                                          headers={"Title": title, "Tags": _tag(item["event"])})
            else:
                resp = self._session.post(url, json={k: item[k] for k in ("title", "message", "event", "ts")}, timeout=8)
            ok = resp.status_code < 400
            if not ok:
                log.warning("notification to %s failed: HTTP %s", host, resp.status_code)
            return {"ok": ok, "status": resp.status_code, "at": time.time()}
        except requests.RequestException as exc:
            log.warning("notification failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200], "at": time.time()}

    def _watch_offline(self) -> None:
        while not self._stop.wait(30.0):
            try:
                self.check_offline()
            except Exception:  # pragma: no cover - must never stop
                log.exception("offline check failed")

    def check_offline(self, now: Optional[float] = None) -> None:
        """Announce lights that have been unreachable for a while, and their return."""
        if self.devices is None:
            return
        now = now or time.time()
        for d in self.devices.snapshot():
            did = d.get("id")
            if d.get("enabled") is False:
                self._offline_since.pop(did, None)
                continue
            if d.get("online"):
                self._offline_since.pop(did, None)
                if did in self._announced_offline:
                    self._announced_offline.discard(did)
                    self.send("offline", f"{d.get('name')} is back", f"{d.get('name')} is reachable again.", key=f"back|{did}|{now}")
                continue
            if not d.get("last_seen"):
                continue  # never seen since start-up: nothing to report yet
            since = self._offline_since.setdefault(did, now)
            if now - since >= OFFLINE_AFTER_S and did not in self._announced_offline:
                self._announced_offline.add(did)
                self.send("offline", f"{d.get('name')} is offline",
                          f"{d.get('name')} ({d.get('host')}) has not answered for {int(OFFLINE_AFTER_S // 60)} minutes: "
                          f"{d.get('last_error') or 'no reply'}. Is it powered and on Wi-Fi?", key=f"offline|{did}")


def _tag(event: str) -> str:
    return {"estop": "rotating_light", "relay": "warning", "schedule": "alarm_clock", "offline": "electric_plug"}.get(event, "bell")
