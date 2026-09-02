"""System, settings, auth, backup and the aggregate state endpoint."""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from flask import Blueprint, Response, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from .. import __version__, get_ctx
from ..gpio.backend import is_raspberry_pi, pi_model
from ..gpio.manager import validate_switches
from ..wled.discovery import local_ipv4_addresses
from . import APIError, body, ok

log = logging.getLogger(__name__)
bp = Blueprint("system", __name__)

HELPER = os.environ.get("JIMBOLED_HELPER", "/opt/jimboled/bin/jimboled-helper")
INSTALL_ENV = Path(os.environ.get("JIMBOLED_INSTALL_ENV", "/etc/jimboled/install.env"))


# ------------------------------------------------------------ aggregate
@bp.get("/state")
def aggregate_state():
    ctx = get_ctx()
    return ok({
        "rev": ctx.rev,
        "cfg_rev": ctx.cfg_rev,
        "ts": time.time(),
        "devices": ctx.devices.snapshot(),
        "gpio": ctx.gpio.snapshot(),
        "discovery_running": ctx.discovery.is_running,
    })


# -------------------------------------------------------------- settings
def _public_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    server = dict(cfg.get("server", {}))
    server.pop("secret_key", None)
    server["password_set"] = bool(server.pop("password_hash", None))
    gpio = dict(cfg.get("gpio", {}))
    gpio.pop("switches", None)
    return {
        "server": server,
        "wled": cfg.get("wled", {}),
        "gpio": gpio,
        "setup_complete": cfg.get("setup_complete", False),
    }


@bp.get("/settings")
def get_settings():
    return ok(_public_settings(get_ctx().store.get()))


@bp.put("/settings")
@bp.patch("/settings")
def update_settings():
    ctx = get_ctx()
    data = body()

    def mutate(cfg):
        if isinstance(data.get("wled"), dict):
            w = data["wled"]
            for key, lo, hi in (("poll_interval_s", 1, 120), ("offline_poll_interval_s", 3, 600), ("request_timeout_s", 1, 30)):
                if key in w:
                    try:
                        cfg["wled"][key] = max(lo, min(hi, float(w[key])))
                    except (TypeError, ValueError):
                        raise APIError(f"{key} must be a number")
        if isinstance(data.get("gpio"), dict):
            g = data["gpio"]
            if "backend" in g:
                backend = str(g["backend"] or "auto")
                if backend not in ("auto", "mock", "lgpio", "rpigpio", "pigpio", "native"):
                    raise APIError("unknown GPIO backend")
                cfg["gpio"]["backend"] = backend
            if "interlock_dead_time_ms" in g:
                cfg["gpio"]["interlock_dead_time_ms"] = int(max(0, min(5000, int(g["interlock_dead_time_ms"]))))
            if "hold_timeout_s" in g:
                cfg["gpio"]["hold_timeout_s"] = max(0.5, min(10.0, float(g["hold_timeout_s"])))
        if isinstance(data.get("server"), dict):
            s = data["server"]
            if "port" in s:
                port = int(s["port"])
                if not 1 <= port <= 65535:
                    raise APIError("port must be between 1 and 65535")
                cfg["server"]["port"] = port
        if "setup_complete" in data:
            cfg["setup_complete"] = bool(data["setup_complete"])

    ctx.store.update(mutate, backup_reason="settings")
    return ok(_public_settings(ctx.store.get()))


# ------------------------------------------------------------------ auth
@bp.post("/settings/password")
def set_password():
    ctx = get_ctx()
    data = body()
    current_hash = (ctx.store.section("server") or {}).get("password_hash")
    if current_hash and not check_password_hash(current_hash, str(data.get("current") or "")):
        raise APIError("Current password is wrong", 403)
    new = str(data.get("password") or "")
    if new and len(new) < 4:
        raise APIError("Password must be at least 4 characters")
    new_hash = generate_password_hash(new) if new else None
    ctx.store.update(lambda cfg: cfg["server"].__setitem__("password_hash", new_hash), backup_reason="password")
    if new_hash:
        session.permanent = True
        session["auth"] = "ok"
    else:
        session.clear()
    return ok({"password_set": bool(new_hash)})


@bp.post("/login")
def api_login():
    ctx = get_ctx()
    data = body()
    current_hash = (ctx.store.section("server") or {}).get("password_hash")
    if not current_hash:
        return ok({"authed": True})
    if check_password_hash(current_hash, str(data.get("password") or "")):
        session.permanent = True
        session["auth"] = "ok"
        return ok({"authed": True})
    raise APIError("Wrong password", 401)


@bp.get("/auth")
def auth_status():
    ctx = get_ctx()
    needs = bool((ctx.store.section("server") or {}).get("password_hash"))
    return ok({"password_set": needs, "authed": (not needs) or session.get("auth") == "ok"})


@bp.post("/logout")
def api_logout():
    session.clear()
    return ok()


# ---------------------------------------------------------------- backup
@bp.get("/backup")
def download_backup():
    ctx = get_ctx()
    cfg = ctx.store.get()
    cfg["server"].pop("secret_key", None)
    payload = json.dumps(cfg, indent=2).encode("utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return send_file(io.BytesIO(payload), mimetype="application/json", as_attachment=True,
                     download_name=f"jimboled-backup-{stamp}.json")


@bp.post("/restore")
def restore_backup():
    ctx = get_ctx()
    raw = None
    if "file" in request.files:
        raw = request.files["file"].read()
    elif request.data:
        raw = request.data
    if not raw:
        raise APIError("Upload a backup file")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise APIError("That file is not a valid JimboLED backup")
    if not isinstance(data, dict) or "devices" not in data or "dashboard" not in data:
        raise APIError("That file is not a valid JimboLED backup")
    try:
        validate_switches(data.get("gpio", {}).get("switches", []))
    except Exception as exc:
        raise APIError(f"Backup contains invalid GPIO settings: {exc}")
    data.setdefault("server", {})
    current = ctx.store.section("server") or {}
    data["server"]["secret_key"] = current.get("secret_key")
    data["server"].setdefault("password_hash", current.get("password_hash"))
    ctx.store.replace(data, backup_reason="pre-restore")
    return ok({"message": "Backup restored"})


@bp.get("/backups")
def list_backups():
    return ok({"backups": get_ctx().store.list_backups()})


@bp.post("/backups/<name>/restore")
def restore_named_backup(name):
    ctx = get_ctx()
    if "/" in name or not name.startswith("config-") or not name.endswith(".json"):
        raise APIError("unknown backup", 404)
    path = ctx.store.backup_dir / name
    if not path.exists():
        raise APIError("unknown backup", 404)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    current = ctx.store.section("server") or {}
    data.setdefault("server", {})["secret_key"] = current.get("secret_key")
    ctx.store.replace(data, backup_reason="pre-restore")
    return ok({"message": f"Restored {name}"})


# ---------------------------------------------------------------- system
def _install_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        for line in INSTALL_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return env


def helper_available() -> bool:
    return os.path.exists(HELPER) and shutil.which("sudo") is not None


def run_helper(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    if not helper_available():
        raise APIError("This action needs the JimboLED service installed with install.sh", 501)
    cmd = ["sudo", "-n", HELPER, *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise APIError("The helper took too long to respond", 504)
    except OSError as exc:
        raise APIError(f"Could not run helper: {exc}", 500)


def apply_boot_pins(ctx) -> None:
    """Record boot-time pin states in config.txt via the helper (Pi only)."""
    if not is_raspberry_pi() or not helper_available():
        return
    switches = (ctx.store.section("gpio") or {}).get("switches", [])
    spec = ",".join(f"{s['pin']}={'dl' if s.get('active_high', True) else 'dh'}" for s in switches if s.get("pin") is not None)
    proc = run_helper("bootpins", spec or "none", timeout=20)
    if proc.returncode != 0:
        log.warning("boot pin update failed: %s", (proc.stderr or proc.stdout).strip()[:200])


@bp.get("/system")
def system_info():
    ctx = get_ctx()
    env = _install_env()
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        load = (0, 0, 0)
    mem_total = mem_avail = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except OSError:
        pass
    temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            temp = int(fh.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    disk = shutil.disk_usage(str(ctx.store.data_dir))
    return ok({
        "version": __version__,
        "hostname": socket.gethostname(),
        "addresses": [a.split("/")[0] for a in local_ipv4_addresses()],
        "port": (ctx.store.section("server") or {}).get("port"),
        "pi": is_raspberry_pi(),
        "pi_model": pi_model(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "gpio_backend": ctx.gpio.factory_name,
        "gpio_simulated": ctx.gpio.simulated,
        "uptime_s": round(time.time() - ctx.started_at),
        "load": load,
        "cpu_temp_c": temp,
        "memory": {"total": mem_total, "available": mem_avail},
        "disk": {"total": disk.total, "free": disk.free},
        "data_dir": str(ctx.store.data_dir),
        "helper": helper_available(),
        "install": {k: v for k, v in env.items() if k in ("JIMBOLED_SRC", "JIMBOLED_APP", "JIMBOLED_USER", "JIMBOLED_PORT", "JIMBOLED_INSTALLED_AT", "JIMBOLED_REPO_URL")},
        "git": _git_info(env),
    })


def _git_info(env: Dict[str, str]) -> Dict[str, Any]:
    """Prefer what install.sh recorded (the service cannot read the owner's clone)."""
    if env.get("JIMBOLED_REV"):
        return {"rev": env.get("JIMBOLED_REV"), "branch": env.get("JIMBOLED_BRANCH", "")}
    src = str(Path(__file__).resolve().parents[2])
    if not (Path(src) / ".git").exists() or not shutil.which("git"):
        return {}
    try:
        rev = subprocess.run(["git", "-C", src, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        branch = subprocess.run(["git", "-C", src, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        return {"rev": rev, "branch": branch}
    except (OSError, subprocess.SubprocessError):
        return {}


@bp.get("/system/logs")
def system_logs():
    ctx = get_ctx()
    limit = min(1000, max(10, int(request.args.get("limit", 200))))
    lines = ctx.log_buffer.tail(limit) if ctx.log_buffer else []
    return ok({"logs": lines})


@bp.post("/system/restart")
def restart_service():
    proc = run_helper("restart", timeout=15)
    if proc.returncode != 0:
        raise APIError(f"Restart failed: {(proc.stderr or proc.stdout).strip()[:300]}", 500)
    return ok({"message": "Restarting JimboLED… the page will reconnect in a moment."})


@bp.post("/system/reboot")
def reboot_pi():
    proc = run_helper("reboot", timeout=15)
    if proc.returncode != 0:
        raise APIError(f"Reboot failed: {(proc.stderr or proc.stdout).strip()[:300]}", 500)
    return ok({"message": "Rebooting the Raspberry Pi. Give it a minute."})


@bp.post("/system/update/check")
def check_update():
    proc = run_helper("check-update", timeout=60)
    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise APIError(f"Update check failed: {(proc.stderr or text).strip()[:300]}", 500)
    try:
        data = json.loads(text)
    except ValueError:
        data = {"raw": text}
    return ok(data)


@bp.post("/system/update")
def run_update():
    proc = run_helper("update", timeout=60)
    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise APIError(f"Update could not start:\n{((proc.stderr or '') + text).strip()[-1500:]}", 500)
    try:
        data = json.loads(text)
    except ValueError:
        data = {"started": True, "message": text or "Update started."}
    data["rev"] = _install_env().get("JIMBOLED_REV", "")
    return ok(data)


@bp.post("/system/shutdown-all")
def emergency_all_off():
    """Panic button: every relay off and every light off."""
    ctx = get_ctx()
    gpio = ctx.gpio.all_off("panic")
    lights = ctx.devices.set_state_all({"on": False})
    return ok({"gpio": gpio, "lights": lights})
