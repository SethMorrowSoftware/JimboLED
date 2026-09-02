# JimboLED architecture

JimboLED is a single Python process that runs on a Raspberry Pi (Zero, Zero 2
or any other model) and serves a dark-themed dashboard for:

* **WLED controllers** – any number of them, discovered on the LAN or added by
  address, controlled through the WLED JSON API.
* **GPIO switches** – relays wired to the Pi header (for example an adjustable
  bed), driven through `gpiozero`/`lgpio` with hard safety rules.

## Process layout

```
waitress (8 threads) ──► Flask app (jimboled/)
                          ├── /                dashboard SPA (templates/index.html + static/)
                          ├── /api/state       aggregate snapshot polled by browsers every 2 s
                          ├── /api/devices     WLED CRUD, control, presets, discovery
                          ├── /api/gpio        switch CRUD, press/heartbeat/release, all-off
                          ├── /api/dashboard   tiles, appearance, scenes
                          ├── /api/settings    polling, safety, password
                          └── /api/system      info, logs, backup/restore, restart/update/reboot
                          │
                          ├── DeviceManager   (wled/manager.py)  poller thread + 4 workers, per-device cache
                          │     └── WLEDClient (wled/client.py)   requests.Session, short timeouts, busy retry
                          ├── DiscoveryService (wled/discovery.py) avahi / built-in mDNS / UDP 65506 / sweep
                          ├── GPIOManager     (gpio/manager.py)  gpiozero OutputDevices + 100 ms watchdog
                          └── ConfigStore     (config.py)        atomic JSON + rolling backups
```

Browsers never talk to WLED directly: everything goes through the Pi so the
dashboard is consistent for every phone on the network and the GPIO safety
logic lives in exactly one place.

The aggregate `/api/state` carries two counters: `rev` changes whenever any
observable state changes; `cfg_rev` only when the configuration changes. The
front end re-fetches the (larger) dashboard layout only when `cfg_rev` moves.

## Directories on the Pi

| Path | Purpose |
| --- | --- |
| `/opt/jimboled/app` | application code, rsynced from the git clone by `install.sh` |
| `/opt/jimboled/venv` | Python virtualenv (`--system-site-packages` so apt's gpiozero/lgpio are used) |
| `/opt/jimboled/app/bin/jimboled-helper` | root-only helper reachable via a one-line sudoers rule |
| `/var/lib/jimboled` | **user data**: `config.json`, `backups/` – never touched by installs |
| `/etc/jimboled/install.env` | install metadata (clone path, owner, port, git revision) |
| `/etc/jimboled/jimboled.env` | optional service overrides loaded by systemd |
| `/etc/systemd/system/jimboled.service` | service unit |
| `/run/jimboled` | runtime dir (lgpio notification pipe, `LG_WD`) |

`install.sh` is idempotent: package installs, user creation, venv, unit and
sudoers are all guarded, and `git pull && sudo bash install.sh` (or
`update.sh`) refreshes code only.

## The privileged helper

The service runs as the unprivileged `jimboled` user with `ProtectHome=yes`.
The dashboard's *Restart*, *Reboot*, *Check for updates* and *Update* buttons
call `sudo -n /opt/jimboled/app/bin/jimboled-helper <cmd>`. The helper is
root-owned and only implements those narrow commands. Anything that must
touch the owner's git clone or survive the service restart is spawned through
`systemd-run`, i.e. outside the service's sandbox and cgroup.

Two more helper commands keep relays safe outside the Python process:

* `pins-safe` – run by systemd `ExecStopPost=` (even after SIGKILL); drives
  every configured pin to its OFF level with `pinctrl`.
* `bootpins` – writes `gpio=<pin>=op,<dl|dh>` lines into `config.txt` so the
  firmware holds relays off from power-on until the service starts.

## Safety model for GPIO

* Every switch has a `mode`: `toggle`, `momentary` (hold-to-run) or `pulse`.
* `momentary` switches need a heartbeat from the browser every 0.4 s while
  held (the server tolerates 1.5 s). If the tab closes, the phone locks or
  Wi-Fi drops, the watchdog releases the relay.
* `max_on_seconds` caps how long any switch stays energised; hold-to-run
  switches always have a cap.
* `interlock_group` guarantees two switches in the same group (bed UP / bed
  DOWN) are never on together; switching one on first turns the other off
  and waits a dead time.
* All relays are driven off at start-up, on shutdown (SIGTERM/atexit), on
  reconfiguration, when the service crashes and restarts, and by
  `ExecStopPost` afterwards. The lgpio pin factory is wrapped so an
  active-low output is claimed *with* its safe level (no LOW blip).
* Without GPIO hardware/libraries the app uses gpiozero's mock factory and the
  UI shows a clear "simulated" badge.

## WLED integration notes

* Polling uses `/json/si` (state + info in one round trip) with `/json` as a
  fallback for old firmware; effect names, effect metadata (`/json/fxdata`),
  palette names and palette previews (`/json/palx`) are fetched once per
  firmware build; presets are re-read when `info.fs.pmt` changes or after a
  reboot, and right after we save/delete one.
* Writes go to `/json/state` with `v:true`; the returned state updates the
  cache immediately. Requests to one device are serialised (WLED has a single
  JSON buffer) and HTTP 503 "busy" answers are retried briefly.
* The effect metadata parser mirrors WLED's own UI rules so each effect shows
  exactly the sliders, checkboxes, colour slots and palette it uses.

## Config file

`config.json` is the single source of truth (devices, switches, dashboard
layout, scenes, appearance, server settings). Writes are atomic (temp file +
rename + fsync) and a rolling set of 20 backups is kept in `backups/`. Unknown
keys are preserved and missing keys are filled from defaults, so upgrades
never need a migration for additive changes.
