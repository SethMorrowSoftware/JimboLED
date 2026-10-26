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
                          ├── /api/lights      every light / a group at once, copy look & presets
                          ├── /api/groups      rooms of controllers
                          ├── /api/schedules   schedules, timers, location, timezone
                          ├── /api/tokens      bearer tokens for integrations
                          ├── /api/gpio        switch CRUD, press/heartbeat/release, all-off
                          ├── /api/estop       emergency-stop zones, inputs, engage/reset
                          ├── /api/dashboard   tiles, appearance, scenes
                          ├── /api/settings    polling, safety, password
                          └── /api/system      info, logs, backup/restore, restart/update/reboot
                          │
                          ├── DeviceManager   (wled/manager.py)  poller thread + 4 workers, per-device cache
                          │     └── WLEDClient (wled/client.py)   requests.Session, short timeouts, busy retry
                          │     └── intents   (wled/intents.py)  device-agnostic "what to do" → per-device payload
                          ├── Scheduler       (scheduler.py)     5 s tick, sun times (sun.py), schedules-state.json
                          ├── DiscoveryService (wled/discovery.py) avahi / built-in mDNS / UDP 65506 / sweep
                          ├── GPIOManager     (gpio/manager.py)  gpiozero OutputDevices + 100 ms watchdog
                          │     └── EStopController (gpio/estop.py) latch state + DigitalInputDevices
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
| `/var/lib/jimboled` | **user data**: `config.json`, `estop.json`, `schedules-state.json`, `backups/` – never touched by installs |
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

And one for schedules: `timezone <Area/City>` sets the system timezone with
`timedatectl` (falling back to `/etc/localtime`), accepting only names that
exist under `/usr/share/zoneinfo`.

## Emergency stop

`all_off()` is a convenience: it releases every relay, and the next request can
energise one again. An emergency stop *latches*, which is a different thing and
lives in `gpio/estop.py`.

* A **zone** says what a stop covers: the built-in `all` zone covers every
  switch; user zones cover an `interlock_group` or a list of switch ids. So a
  caravan can stop the awning without locking out the bed.
* `GPIOManager._check_estop()` guards every energising path – `turn_on`,
  `toggle`, `pulse`, `press`, scene actions and `test_pin` – and is re-checked
  inside `_energise`'s retry loop, so a stop that latches during an interlock
  dead time still blocks. `turn_off`, `release` and `all_off` are never
  blocked: safety only ever runs one way.
* The watchdog polls hardware inputs first on every 100 ms tick, then drives
  every covered relay off again, so a wedged output cannot outlive a stop.
* A pin energised by *Test this pin* belongs to no switch, so it is registered
  in `GPIOManager._test_pins` before it is driven high. A master stop releases
  it like any relay, and the watchdog reclaims it if the request that asked
  for it never comes back.
* **Hardware inputs** are `gpiozero.DigitalInputDevice`s. gpiozero reports
  `value == 1` for "active" – a LOW pin under a pull-up, a HIGH pin under a
  pull-down – so one rule covers both wirings: a closed normally-closed loop
  reads 1, and anything else (a press, a cut wire, an unreadable pin) latches
  after two consecutive ticks of debounce.
* In simulation the mock pin is parked at the level a healthy button would
  hold, because a mock pin floats at its pull level and would otherwise boot a
  laptop straight into a latch nobody can clear.
* Latch state lives in `estop.json`, not in `config.json`: it must survive a
  crash and a restart, it must not churn the configuration backups, and
  restoring last week's settings must not restore last week's emergency.
  `LatchStore` has its own lock and takes no other, so it is a leaf: latches
  are written with the GPIO lock held and the ordering stays acyclic
  (config-store → GPIO → latch). The write is durable as well as atomic —
  see `atomicio.py`: a lock-out a power cut can undo is not a lock-out.

`gpio/common.py` holds the pin tables and error types both `manager.py` and
`estop.py` need; `manager.py` re-exports every name, so
`from jimboled.gpio.manager import GPIOError` still works.

## Safety model for GPIO

* Every switch has a `mode`: `toggle`, `momentary` (hold-to-run) or `pulse`.
* `momentary` switches need a heartbeat from the browser every 0.4 s while
  held (the server tolerates 1.5 s). If the tab closes, the phone locks or
  Wi-Fi drops, the watchdog releases the relay.
* `max_on_seconds` caps how long any switch stays energised; hold-to-run
  switches always have a cap.
* `interlock_group` guarantees two switches in the same group (bed UP / bed
  DOWN) are never on together; switching one on first turns the other off
  and waits a dead time. That wait happens **outside** the manager's lock:
  one caller sleeping while holding it would stall every other switch, the
  heartbeats, `all_off` and the watchdog. `_GuardedLock` counts nesting depth
  so `_energise()` can check that rather than trust its callers.
* All relays are driven off at start-up, on shutdown (SIGTERM/atexit), on
  reconfiguration, when the service crashes and restarts, and by
  `ExecStopPost` afterwards. The lgpio pin factory is wrapped so an
  active-low output is claimed *with* its safe level (no LOW blip).
* Without GPIO hardware/libraries the app uses gpiozero's mock factory and the
  UI shows a clear "simulated" badge.
* Hold-to-run switches only ever run while a person holds the button: they
  refuse `on`, `toggle` and `pulse` from anywhere (scenes, schedules, API
  tokens), so nothing can run a bed motor unattended.
* A latched emergency stop outranks all of the above; see the section above.

## WLED integration notes

* Polling uses `/json/si` (state + info in one round trip) with `/json` as a
  fallback for old firmware; effect names, effect metadata (`/json/fxdata`),
  palette names and palette previews (`/json/palx`) are fetched once per
  firmware build; presets are re-read when `info.fs.pmt` changes or after a
  reboot, and right after we save/delete one.
* Writes go to `/json/state` with `v:true`; the returned state updates the
  cache immediately. A preset is sent the way WLED's own app sends it – its
  content plus `pd` – because a bare `ps` equal to the preset WLED still
  considers current is ignored. `"on":"t"` becomes an explicit on/off, and a
  `seg` object without an id is addressed to every segment. Commands that are
  not idempotent are never retried on a 503, since WLED can apply a command and
  still answer "busy". Names are cut to 32 UTF-8 bytes, the size of WLED's
  buffers, and replies are decoded leniently so one bad character can't hide a
  controller's presets. The catalogue (effects, palettes, presets, previews) is
  downloaded outside the command lock and only remembered once it is complete. Requests to one device are serialised (WLED has a single
  JSON buffer) and HTTP 503 "busy" answers are retried briefly.
* The effect metadata parser mirrors WLED's own UI rules so each effect shows
  exactly the sliders, checkboxes, colour slots and palette it uses.

## Controlling many lights at once

WLED numbers its effects, palettes and presets per firmware build and per
device, so "effect 66" or "preset 3" means different things on different
controllers, and a `"seg"` object without an id only reaches the *selected*
segments. `wled/intents.py` therefore works with **intents** – "red", "the
preset called Movie", "Rainbow at half speed", "2700 K white" – and plans one
payload per controller from what the poller already knows about it: its
segments (every segment is addressed by id), its light capabilities (`lc`:
white channel, CCT), and its effect, palette and preset names. Anything a
controller can't do is left out of *its* payload and reported as a note, so
the rest still applies and the person is told plainly what didn't.
`DeviceManager.apply_intent()` sends the payloads in parallel on the command
pool. Copying a look reads one controller's state back into an intent; copying
a preset rewrites its effect and palette numbers for each target by name and
saves it with `psave`.

## Schedules

`scheduler.py` wakes every 5 s and fires each enabled schedule whose most
recent due time has passed and has not fired yet. "Has fired" is recorded per
schedule in `schedules-state.json` (atomic writes) *before* the action runs, so
a crash mid-action can't make it run twice. A due time more than 3 minutes in
the past – the Pi was off – is recorded as *missed* and skipped. Editing a
schedule sets `updated_at`, which re-arms it from that moment. Sunrise and
sunset come from the NOAA approximation in `sun.py` (no network needed);
random variation is derived from a hash of the schedule id and the date, so it
is stable across restarts. Actions go through the same code as scenes, so the
GPIO safety rules and emergency stops apply unchanged, and a hold-to-run switch
can only be scheduled *off*.

## Front-end layers

No build step: the browser loads the files in `static/js/` in order and each
attaches one global (`UI`, `api`, `GPIO`, `EStop`, `Device`, `Lights`,
`Automation`, `Settings`, `Wizard`, `App`). `lights.js` holds the Lights panel,
group tiles and the light-settings editor shared by scenes and schedules;
`automation.js` holds the Groups, Schedules and Integrations settings pages.

`static/css/app.css` is layered deliberately:

1. **Foundation** – type, spacing, radius, elevation and motion scales. Type is
   a rem scale driven by `--text-scale`, so Settings → Appearance → Text size
   scales the whole interface; spacing stays in px so that never reflows the
   grid.
2. **Theme** – every theme defines the same complete token set, including
   `--on-accent`, `--sheen`, `--shadow-color` and per-status foregrounds, so a
   component rule never needs to know whether it is light or dark. Theme
   selectors are scoped to `:root` (or an explicit `.theme-swatch`, which is
   how the Settings chips preview their own palette).
3. **Components** – built only from layers 1 and 2.

## Config file

`config.json` is the single source of truth (devices, groups, switches,
schedules, location, dashboard layout, scenes, appearance, server settings and
hashed API tokens). Writes go through
`atomicio.write_json_atomic()`: serialise, temp file, fsync, rename, then
fsync the *directory* – the rename lives there, so without it a power cut can
still bring the old file back. The payload is built before anything on disk is
touched and the new value is adopted in memory only once the write succeeded,
so a full disk leaves memory and file agreeing rather than drifting apart. A
rolling set of 20 backups is kept in `backups/`. Unknown keys are preserved and
missing keys are filled from defaults, so upgrades never need a migration for
additive changes.

A restore is validated before it is applied (`api/system.py`): switches and the
whole emergency-stop section are *refused* if they will not validate – silently
dropping a zone or a physical button would restore a dashboard that promises a
stop which no longer exists – while tiles and scenes, being cosmetic, are
dropped individually rather than failing the restore.
