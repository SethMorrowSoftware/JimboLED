# JimboLED architecture

JimboLED is a single Flask process that runs on a Raspberry Pi (Zero, Zero 2, or
any other model) and serves a dark-themed dashboard for:

* **WLED controllers** – any number of them, discovered on the LAN or added by
  IP, controlled through the WLED JSON API.
* **GPIO switches** – relays wired to the Pi header (for example a motorized
  bed), driven through `gpiozero` with hard safety rules.

## Process layout

```
waitress (threads) ──► Flask app
                        ├── /            dashboard SPA (templates/index.html + static/)
                        ├── /api/state   aggregate snapshot polled by the browser
                        ├── /api/devices WLED device CRUD + control
                        ├── /api/gpio    switch CRUD + actions
                        ├── /api/dashboard, /api/settings, /api/system
                        │
                        ├── DeviceManager   background poller, per-device cache
                        │     └── WLEDClient (requests.Session, short timeouts)
                        ├── GPIOManager     gpiozero OutputDevices + watchdog thread
                        └── ConfigStore     atomic JSON at $JIMBOLED_DATA_DIR/config.json
```

The browser never talks to WLED directly; everything goes through the Pi so
the dashboard stays consistent for every phone/tablet on the network and the
GPIO safety logic lives in exactly one place.

## Directories on the Pi

| Path                          | Purpose                                              |
| ----------------------------- | ---------------------------------------------------- |
| `/opt/jimboled/app`           | application code (rsynced from the git clone)        |
| `/opt/jimboled/venv`          | Python virtualenv (`--system-site-packages`)         |
| `/var/lib/jimboled`           | **user data**: `config.json`, backups, logs          |
| `/etc/jimboled/install.env`   | where the git clone lives, chosen port, service user |
| `/etc/systemd/system/jimboled.service` | service unit                                |

`install.sh` never touches `/var/lib/jimboled` except to create it and copy
`config.example.json` when no config exists. Re-running it (or `update.sh`)
refreshes code and dependencies only.

## Safety model for GPIO

* Every switch has `mode`: `toggle`, `momentary` (hold-to-run) or `pulse`.
* `momentary` switches require a heartbeat from the browser at least every
  second while held. If the tab closes, the phone locks, or Wi-Fi drops, the
  watchdog releases the relay.
* `max_on_seconds` caps how long any switch can stay energised.
* `interlock_group` guarantees that two switches in the same group (e.g. bed
  UP and bed DOWN) are never on at the same time; switching one on first turns
  the other off and waits a short dead time.
* All relays are driven off at start-up, on shutdown (SIGTERM/atexit) and when
  the service crashes and restarts.
* When no GPIO hardware/library is present the app runs on gpiozero's mock pin
  factory and the UI shows a clear "simulation" badge.

## Config file

`config.json` is the single source of truth (devices, switches, dashboard
layout, appearance, server settings). Writes are atomic (temp file + rename)
and a rolling set of backups is kept in `/var/lib/jimboled/backups`.
