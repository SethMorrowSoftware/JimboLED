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
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

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
    },
    "wled": {
        "poll_interval_s": 3.0,
        "offline_poll_interval_s": 15.0,
        "request_timeout_s": 4.0,
    },
    "dashboard": {
        "title": "JimboLED",
        "subtitle": "",
        "theme": "midnight",     # midnight | graphite | oled
        "accent": "#7c5cff",
        "density": "comfortable",  # comfortable | compact
        "show_offline": True,
        "show_clock": True,
        "tiles": [],              # ordered list of tile descriptors, see dashboard.py
        "quick_presets": [],      # [{"id":..., "name":..., "actions":[...]}]
    },
    "setup_complete": False,
}


class ConfigError(Exception):
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``base`` updated with ``override`` recursively (dicts only)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
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

    def _write_locked(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=2, sort_keys=False)
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(self.data_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def backup(self, reason: str = "manual") -> Path:
        """Copy the current config into the backups directory and prune old ones."""
        with self._lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason)[:32] or "backup"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = self.backup_dir / f"config-{stamp}-{safe_reason}.json"
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
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
        with self._lock:
            before = copy.deepcopy(self._data)
            working = copy.deepcopy(self._data)
            mutator(working)
            working["version"] = CONFIG_VERSION
            if working == before:
                return copy.deepcopy(working)
            if backup_reason:
                self.backup(backup_reason)
            self._data = working
            self._write_locked()
            after = copy.deepcopy(working)
        for listener in list(self._listeners):
            try:
                listener(before, after)
            except Exception:  # pragma: no cover - listeners must not break saves
                log.exception("config listener failed")
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
