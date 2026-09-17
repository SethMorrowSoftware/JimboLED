"""Configuration store.

All user data lives in a single JSON file (``config.json``) inside the data
directory.  Writes are atomic (write to a temporary file, fsync, rename) so a
power cut mid-write never leaves a half-written config behind, and a rolling
set of timestamped backups is kept next to it.

The store is thread-safe: every read returns a deep copy, and every mutation
happens under a lock through :meth:`ConfigStore.update`.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from .atomicio import write_json_atomic

log = logging.getLogger(__name__)

CONFIG_VERSION = 1
MAX_BACKUPS = 20

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "server": {
        "host": "0.0.0.0",
        "port": 80,
        # Optional dashboard password (werkzeug hash).  ``None`` = open on LAN.
        "password_hash": None,
        "secret_key": None,
        "session_version": 0,
        # Extra host names the dashboard may be opened with (e.g. "jimboled.lan").
        # IP addresses, bare names and *.local/.lan/.home... always work.
        "allowed_hosts": [],
    },
    "devices": [],
    "gpio": {
        # "auto" picks the best available gpiozero pin factory, "mock" forces
        # simulation (useful on a laptop), anything else is passed straight to
        # gpiozero (lgpio, rpigpio, pigpio, native).
        "backend": "auto",
        "switches": [],
        # Milliseconds to wait between turning one interlocked relay off and
        # the opposing one on (protects motor drivers from shoot-through).
        "interlock_dead_time_ms": 250,
        # Seconds without a heartbeat before a held (momentary) switch releases.
        "hold_timeout_s": 1.5,
        # Emergency stop.  ``zones`` are extra, scoped stops on top of the
        # built-in master stop; ``inputs`` are physical buttons wired to GPIO
        # inputs.  The latch itself is runtime state and lives in estop.json.
        "estop": {
            "master_name": "All relays",
            "confirm_engage": False,
            "zones": [],
            "inputs": [],
        },
    },
    "wled": {
        "poll_interval_s": 3.0,
        "offline_poll_interval_s": 15.0,
        "request_timeout_s": 4.0,
    },
    "dashboard": {
        "title": "JimboLED",
        "subtitle": "",
        "theme": "midnight",     # see dashboard.THEMES; "auto" follows the device
        "accent": "#7c5cff",
        "density": "comfortable",  # comfortable | compact | roomy
        "text_scale": 100,         # 85-150 %, for readability at arm's length
        "radius": "soft",          # sharp | soft | round
        "show_offline": True,
        "show_clock": True,
        "show_estop": True,        # the red emergency-stop button in the header
        "tiles": [],              # ordered list of tile descriptors, see dashboard.py
        "scenes": [],             # [{"id":..., "name":..., "actions":[...]}], see dashboard.py
    },
    "setup_complete": False,
}


class ConfigError(Exception):
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any], _path: str = "") -> Dict[str, Any]:
    """Return ``base`` updated with ``override`` recursively (dicts only).

    A value that would replace one of the default *sections* with something
    that is not an object is dropped and logged. Every consumer treats
    ``config["gpio"]`` and friends as objects, so a scalar there – a stray
    ``"gpio": "mock"`` from a hand-edited file – would crash the service on
    start-up, over and over. Losing one bad key beats never coming up.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        where = f"{_path}{key}"
        if isinstance(out.get(key), dict):
            if isinstance(value, dict):
                out[key] = _deep_merge(out[key], value, f"{where}.")
            else:
                log.error("config.json: '%s' should be an object, not %s; using the default",
                          where, type(value).__name__)
            continue
        out[key] = copy.deepcopy(value)
    return out


def default_data_dir() -> Path:
    env = os.environ.get("JIMBOLED_DATA_DIR")
    if env:
        return Path(env).expanduser()
    # Development fallback: keep data inside the checkout.
    return Path(__file__).resolve().parent.parent / "data"


class ConfigStore:
    """Thread-safe, atomic JSON config with defaults and rolling backups."""

    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.path = self.data_dir / "config.json"
        self.backup_dir = self.data_dir / "backups"
        self._lock = threading.RLock()
        # Listeners are notified outside the data lock but strictly in commit
        # order (this lock is taken before the data lock is released).
        self._notify_lock = threading.RLock()  # re-entrant: a listener may write back to the store
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._listeners: List[Callable[[Dict[str, Any], Dict[str, Any]], None]] = []
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> Dict[str, Any]:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    if not isinstance(raw, dict):
                        raise ConfigError("config.json must contain a JSON object")
                except (OSError, ValueError, ConfigError) as exc:
                    # Never destroy a broken file: move it aside so the user can recover it.
                    broken = self.path.with_suffix(f".broken-{int(time.time())}.json")
                    log.error("config.json is unreadable (%s); moving it to %s", exc, broken)
                    try:
                        shutil.move(str(self.path), str(broken))
                    except OSError:
                        pass
                    raw = {}
            else:
                raw = {}
            migrated = self._migrate(raw)
            self._data = _deep_merge(DEFAULT_CONFIG, migrated)
            if not self.path.exists() or migrated != raw:
                self._write_locked()
            return copy.deepcopy(self._data)

    def _migrate(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Upgrade older config layouts in place.  Currently a no-op hook."""
        version = raw.get("version", CONFIG_VERSION)
        if version > CONFIG_VERSION:
            log.warning("config.json is from a newer JimboLED (v%s); loading best-effort", version)
        raw = copy.deepcopy(raw)
        raw["version"] = CONFIG_VERSION
        return raw

    def _write_locked(self, data: Dict[str, Any] | None = None) -> None:
        write_json_atomic(self.path, self._data if data is None else data,
                          mode=0o600, indent=2, sort_keys=False)

    def backup(self, reason: str = "manual") -> Path:
        """Copy the current config into the backups directory and prune old ones."""
        with self._lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason)[:32] or "backup"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = self.backup_dir / f"config-{stamp}-{safe_reason}.json"
            # Two saves inside the same second would otherwise overwrite each
            # other, quietly costing one of the snapshots the user expects.
            if dest.exists():
                for n in range(2, 100):
                    candidate = self.backup_dir / f"config-{stamp}-{safe_reason}-{n}.json"
                    if not candidate.exists():
                        dest = candidate
                        break
            write_json_atomic(dest, self._data, mode=0o600, indent=2)
            backups = sorted(self.backup_dir.glob("config-*.json"))
            for old in backups[:-MAX_BACKUPS]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return dest

    def list_backups(self) -> List[Dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        out = []
        for p in sorted(self.backup_dir.glob("config-*.json"), reverse=True):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    # --------------------------------------------------------------- access
    def get(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(name))

    def update(self, mutator: Callable[[Dict[str, Any]], Any], *, backup_reason: str | None = None) -> Dict[str, Any]:
        """Apply ``mutator(config_dict)`` under the lock and persist.

        The mutator receives a deep copy; if it raises nothing is written.
        Returns the new config.  Listeners are notified *outside* the lock.
        """
        self._lock.acquire()
        notify = False
        try:
            before = copy.deepcopy(self._data)
            working = copy.deepcopy(self._data)
            mutator(working)
            working["version"] = CONFIG_VERSION
            if working == before:
                return copy.deepcopy(working)
            if backup_reason:
                self.backup(backup_reason)
            # Persist first, adopt second.  A write that fails (a full disk, a
            # value that will not serialise) must leave memory and the file
            # agreeing on the old configuration rather than quietly drifting
            # apart until the next save commits the bad value.
            self._write_locked(working)
            self._data = working
            after = copy.deepcopy(working)
            self._notify_lock.acquire()
            notify = True
        finally:
            self._lock.release()
        try:
            for listener in list(self._listeners):
                try:
                    listener(before, after)
                except Exception:  # pragma: no cover - listeners must not break saves
                    log.exception("config listener failed")
        finally:
            if notify:
                self._notify_lock.release()
        return after

    def replace(self, new_config: Dict[str, Any], *, backup_reason: str = "restore") -> Dict[str, Any]:
        """Replace the whole config (used by restore-from-backup)."""
        if not isinstance(new_config, dict):
            raise ConfigError("config must be a JSON object")
        merged = _deep_merge(DEFAULT_CONFIG, self._migrate(new_config))
        return self.update(lambda cfg: (cfg.clear(), cfg.update(merged)), backup_reason=backup_reason)

    def on_change(self, listener: Callable[[Dict[str, Any], Dict[str, Any]], None]) -> None:
        self._listeners.append(listener)


def new_id(prefix: str) -> str:
    """Short, URL-safe, human-friendly identifier."""
    import secrets

    return f"{prefix}-{secrets.token_hex(4)}"
