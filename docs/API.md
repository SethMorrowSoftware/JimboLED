# JimboLED REST API

Everything the dashboard does goes through this JSON API, so you can script
JimboLED from Home Assistant, Node-RED, cron jobs or a Stream Deck.

**Rules**

* Base URL: `http://<pi>/api/…`
* Every request that changes something (POST/PUT/PATCH/DELETE) must send the
  header `X-Requested-With: JimboLED` (anti-CSRF). GETs need nothing.
* If a dashboard password is set, log in first: `POST /api/login {"password": "…"}`
  and keep the session cookie.
* **Or use an API token** (Settings → Integrations): send
  `Authorization: Bearer jl_…`. A token replaces both the login and the
  `X-Requested-With` header, and is for *operating* things: it may read
  everything except `/api/tokens`, `/api/settings` and backups, and may POST to
  `/api/lights/apply|capture|undo/…`, `/api/devices/<id>/state|refresh|copy|identify`,
  `/api/devices/<id>/presets/<slot>/copy`, `/api/scenes/<id>/run`,
  `/api/schedules/<id>/run`, `/api/timers`, `/api/gpio/all-off`,
  `/api/system/shutdown-all`, `/api/estop/engage`, and
  `/api/gpio/switches/<id>/action` with `on`/`off`/`toggle`/`pulse`. Everything
  else – changing settings, switches, schedules or stops, resetting an
  emergency stop, holding a hold-to-run switch – gets **403**. A wrong token
  gets **401**.
* Errors are `{"error": "message"}` with a 4xx/5xx status.
* Requests must use an IP address, a plain host name, or a private-style name (`*.local`, `*.lan`, `*.home`, …) in the `Host` header; other names get **421** unless listed in `server.allowed_hosts`. This blocks DNS-rebinding attacks from the internet.
* Hold-to-run switches must receive a heartbeat at least every second (the dashboard sends one every 0.4 s); the relay releases automatically otherwise. They can't be pulsed, toggled or turned on by a scene, a schedule or a script – only held by a person (the 2 s *Test this pin* blip while setting up is the one exception).

```bash
# turn a controller on at 60 %
curl -s -X POST -H 'X-Requested-With: JimboLED' -H 'Content-Type: application/json' \
  http://jimboled.local/api/devices/wled-a1b2c3/state -d '{"on":true,"bri":153}'
```

## Every light at once

These endpoints take a **target** and a device-agnostic **intent**, and work out
what each controller needs: effects, palettes and presets are matched *by name*
(each controller numbers them differently), colours go to every segment, and
white/CCT only to strips that have them.

**Targets:** `"all"` (default, every enabled controller) · `"group:<id>"` ·
`"device:<id>"` · `{"devices": ["wled-…", …]}`.

**Intent fields** (all optional, at least one): `on` (true/false/`"toggle"`),
`bri` 1–255 or `brightness` 1–100 %, `color` `"#rrggbb"`, `colors` (up to 3,
`null` leaves a slot alone), `kelvin` 1000–12000 (white light the best way each
strip can: white LEDs/CCT where present, a colour mix where not), `white`
0–255, `cct` 0–255, `effect` (name, `"Solid"`), `palette` (name), `preset`
(name), `preset_slot` 1–250, `speed`, `intensity` 0–255, `custom`
`{c1,c2,c3,o1,o2,o3}`, `transition` seconds 0–60, `nightlight`
`{"on":true,"minutes":30,"mode":"fade|instant|color|sunrise","target":0}`,
`sync` (restart effects together), `segments` (`"all"`/`"main"`),
`best_effort` (don't report features a light lacks). A colour, effect or preset
also switches the light on unless `on` says otherwise.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/lights/apply` | `{"target":"all","intent":{"preset":"Movie"}}` or flat: `{"target":"group:grp-…","color":"#ff0000","brightness":60}`. Returns `ok`, `changed`, `total`, a plain-English `message` and per-light `results[]` (`status` ok/partial/skipped/offline/error, `notes`) |
| GET | `/api/lights/catalog?target=…` (or `?devices=a,b`) | effects, palettes and presets by name with `count` (how many of the online lights have each), `caps` {white, cct}, `led_count`, `power_ma` |
| POST | `/api/lights/capture` | `{"target":…}` → the exact current look as scene `actions[]` |
| POST | `/api/lights/undo/<undo>` | every `/lights/apply` that changed something returns an `undo` key; this puts those lights back as they were (kept 10 minutes, last 20 changes) |
| POST | `/api/devices/<id>/copy` | `{"target":…, "include_brightness":true}` – make other lights look like this one |
| POST | `/api/devices/<id>/presets/<slot>/copy` | `{"target":…, "overwrite":true}` – store this preset on other controllers under the same name (same-name slot, else the same number if free, else the first free slot). Playlists can't be copied |
| GET / POST | `/api/groups` | `{"name":"Bedroom","icon":"bed","color":"#…","devices":["wled-…"]}`; names are unique; each group gets a dashboard tile |
| PUT / DELETE | `/api/groups/<id>` | |

```bash
# every light warm white at 40 %, fading over 2 s
curl -s -X POST -H 'Authorization: Bearer jl_…' -H 'Content-Type: application/json' \
  http://jimboled.local/api/lights/apply -d '{"target":"all","kelvin":2700,"brightness":40,"transition":2}'
```

## Schedules and timers

A schedule is `{"name", "enabled", "when", "days", "random", "action", "delete_after"}`:

* `when`: `{"type":"time","time":"07:00"}` · `{"type":"sun","event":"sunset","offset":15}` (minutes, −180…180) · `{"type":"once","at":<unix seconds>}`
* `days`: weekday numbers, Monday = 0 (ignored for `once`)
* `random`: 0–60 minutes of variation either side, fixed per day
* `action`: `{"type":"lights","target":…,"intent":{…}}` · `{"type":"scene","ref":"scene-…"}` · `{"type":"switch","ref":"sw-…","action":"off|on|toggle|pulse"}` (hold-to-run switches: `off` only) · `{"type":"all_off"}`

Runs that were due while the Pi was off are skipped after 3 minutes and shown as *missed*.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/schedules` | schedules with `next_run`, `last_run`, `last_ok`, `last_message`, `missed_at`, `problem`; plus `location`, `timezone`, `now`, today's `sun` |
| POST | `/api/schedules` | create (max 100) |
| PUT / PATCH / DELETE | `/api/schedules/<id>` | any edit re-arms from now |
| POST | `/api/schedules/<id>/run` | run it now (a one-off timer run by hand is used up) |
| PUT | `/api/schedules/paused` | `{"paused":true}` holds every repeating schedule – guests staying, a holiday; one-off timers still run. Resuming re-arms from now |
| POST | `/api/timers` | `{"minutes":30,"action":{…},"name":"Sleep timer"}` – a one-off that removes itself (default action: everything off) |
| GET / PUT | `/api/location` | `{"lat":51.5,"lon":-0.12,"label":"Home","timezone":"Europe/London"}`; `?zones=1` lists timezones. Setting the timezone needs the installed helper |

## Notifications

| Method | Path | Notes |
| --- | --- | --- |
| GET / PUT | `/api/notify` | `{"url":"https://ntfy.sh/<topic>","events":["estop","relay","schedule","offline"]}`. An ntfy URL gets a plain-text message with a title; any other URL gets JSON `{"title","message","event","ts"}`. The same message is not repeated within 10 minutes |
| POST | `/api/notify/test` | send a test message now; **502** with the reason if it did not arrive |

## API tokens

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/tokens` | `[{id, name, prefix, created, last_used}]` |
| POST | `/api/tokens` | `{"name":"Home Assistant"}` → `{"token":"jl_…"}`, shown **once**; only a SHA-256 is stored |
| DELETE | `/api/tokens/<id>` | |

## State

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/state` | Everything the dashboard polls: `devices[]`, `gpio`, `rev`, `cfg_rev` |

## WLED controllers

| Method | Path | Body / notes |
| --- | --- | --- |
| GET | `/api/devices` | summaries (online, state, info, effect/preset names) |
| POST | `/api/devices` | `{"host":"192.168.1.50","name":"optional","skip_probe":false}` |
| POST | `/api/devices/probe` | `{"host":…}` → name/version/LED count without adding |
| GET | `/api/devices/<id>` | full state, info, effect catalogue (with slider metadata), palettes, presets |
| PUT | `/api/devices/<id>` | `name`, `host`, `enabled`, `icon`, `color`, `notes`, `poll_interval_s` |
| DELETE | `/api/devices/<id>` | |
| POST | `/api/devices/<id>/state` | any [WLED JSON state](https://kno.wled.ge/interfaces/json-api/) subset, e.g. `{"on":"t"}`, `{"seg":{"id":0,"fx":9,"fxdef":true}}`, `{"ps":3}`, `{"nl":{"on":true,"dur":15}}`. Colours accept `[r,g,b]`, `"#ff0000"` or `"ff0000"`. |
| POST | `/api/devices/all/state` | same body, applied to every online controller |
| POST | `/api/devices/<id>/refresh` | poll now |
| POST | `/api/devices/<id>/catalog` | reload effects/palettes |
| GET | `/api/devices/<id>/presets` | |
| POST | `/api/devices/<id>/presets` | `{"name":"Movie","slot":5,"quick_label":"MV","include_brightness":true,"save_segment_bounds":true}` (slot optional = next free) saves the current look. Add `"state":{…}` to save that instead, or `"rename":true` to rename the preset in `slot` while keeping what it does (playlists included) |
| DELETE | `/api/devices/<id>/presets/<slot>` | |
| POST | `/api/devices/<id>/reboot` | |
| POST | `/api/devices/<id>/identify` | blink it white for 3 s, then put it back – to see which light is which |
| POST | `/api/discover/identify` | `{"host":"192.168.1.40"}` – the same for a controller found but not added yet |
| POST | `/api/discover` | `{"methods":["mdns","nodes","subnet"],"subnet":"192.168.1.0/24"}` (all optional) |
| GET | `/api/discover` | progress + `found[]` + `passive[]` (controllers heard broadcasting). A controller that has been offline for a minute and turns up at a new address with the same MAC (after a router restart, say) is followed automatically |

## GPIO switches

| Method | Path | Body / notes |
| --- | --- | --- |
| GET | `/api/gpio` | backend, simulated flag, live switch states |
| GET | `/api/gpio/pins` | header map with notes, recommended/reserved flags, boot pull |
| GET | `/api/gpio/switches` | configured switches |
| POST | `/api/gpio/switches` | `{"name":"Bed up","pin":17,"mode":"momentary","active_high":false,"max_on_seconds":60,"interlock_group":"bed","pulse_ms":500,"icon":"up","color":"#34d399","confirm":false}` |
| PUT | `/api/gpio/switches/<id>` | same fields |
| DELETE | `/api/gpio/switches/<id>` | |
| POST | `/api/gpio/switches/<id>/action` | `{"action":"on"|"off"|"toggle"|"pulse"}`; hold-to-run: `{"action":"press"}` → returns `switch.token`, then `{"action":"heartbeat","token":…}` at least every second, then `{"action":"release","token":…}` |
| POST | `/api/gpio/all-off` | release every relay (does **not** latch – see the emergency stop below) |
| POST | `/api/gpio/test` | `{"pin":22,"active_high":true,"duration_ms":400}` – pulse a pin to find its relay. A pin that belongs to a switch is pulsed *as* that switch (interlocks and all); a bare pin is claimed for the pulse and released by any master emergency stop. |
| GET | `/api/gpio/events` | recent on/off log with reasons |

A momentary switch **cannot** be turned on with `on`; it only runs while
heartbeats arrive, and it always has a maximum on-time.

## Buttons

Push buttons wired to GPIO inputs. A button is
`{"name", "pin", "pull": "up"|"down", "enabled", "press": action|null, "hold": action|null}`
where an action has the same shape as a schedule's (`lights`, `scene`,
`switch`, `all_off`). A press shorter than a second is a tap; holding it for a
second runs `hold` (or, if there is none, `press` on release). Hold-to-run
switches can't be run by a button.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/gpio/buttons` | buttons with live `pressed`, `available`, `error`, `presses`, `last_event` |
| POST | `/api/gpio/buttons` | add one; the pin may not be used by a relay, an emergency-stop input or another button |
| PUT / PATCH / DELETE | `/api/gpio/buttons/<id>` | |
| POST | `/api/gpio/buttons/<id>/try` | `{"kind":"press"\|"hold"}` – do what the button would do |

## Emergency stop

`all-off` and `shutdown-all` release everything, and the next request can turn
it straight back on. An **emergency stop** latches: every relay it covers is
held off and every attempt to energise one is refused with **409** until the
stop is reset.

| Method | Path | Body / notes |
| --- | --- | --- |
| GET | `/api/estop` | `engaged`, `engaged_zones[]`, `zones[]` (each with what it covers and whether a button is holding it), `inputs[]` |
| POST | `/api/estop/engage` | `{"zone":"all","reason":"optional note"}` – `zone` defaults to `all`. Idempotent. |
| POST | `/api/estop/reset` | `{"zone":"all"}` – **400** while a physical button is still held |
| GET / POST | `/api/estop/zones` | `{"name":"Bed","scope":"group"\|"switch","refs":["bed-head"],"icon":"bed","color":"#f87171"}` |
| PUT / DELETE | `/api/estop/zones/<id>` | the built-in `all` zone cannot be edited or removed |
| POST | `/api/estop/inputs` | `{"name":"Bedside button","pin":26,"zone":"all","normally_closed":true,"pull":"up"}` |
| PUT / DELETE | `/api/estop/inputs/<id>` | |
| PUT | `/api/estop/settings` | `master_name`, `confirm_engage` (ask before engaging; off by default) |

```bash
# stop the bed, then read back what is locked out
curl -s -X POST -H 'X-Requested-With: JimboLED' -H 'Content-Type: application/json' \
  http://jimboled.local/api/estop/engage -d '{"zone":"stop-1a2b3c4d","reason":"motor stuck"}'
```

A request refused by a latch answers with the zone, so a client can offer the
right reset without another round trip:

```json
{ "error": "Emergency stop \u201cBed\u201d is engaged \u2026", "estop": true,
  "zone": "stop-1a2b3c4d", "zone_name": "Bed" }
```

Each switch in `/api/state` and `/api/gpio` also carries `locked_out`,
`locked_by` and `locked_zone`.

The latch lives in `estop.json` next to `config.json`, so it survives a
restart, a crash and a power cut. It is runtime state, not configuration, so
it is deliberately **not** part of a backup.

## Dashboard, scenes, settings

| Method | Path | Notes |
| --- | --- | --- |
| GET / PUT | `/api/dashboard` | `title`, `subtitle`, `theme` (`auto`/midnight/graphite/ocean/oled/daylight/paper), `accent`, `density` (comfortable/compact/roomy), `radius` (sharp/soft/round), `text_scale` (85–150 %), `show_offline`, `show_clock`, `show_estop`, `show_lights_button`, `favorite_colors[]` (up to 24 hex colours), `tiles[]`, `scenes[]`. GET also returns `groups[]` |
| POST | `/api/dashboard/tiles` | add a `heading`, `note`, `clock` or `all` tile |
| PUT / DELETE | `/api/dashboard/tiles/<id>` | `size` (s/m/l/xl), `hidden`, `name`, `icon`, `color`, `opts` |
| POST | `/api/dashboard/order` | `{"ids":[…]}` |
| GET / POST | `/api/scenes` | `{"name":"Goodnight","icon":"moon","actions":[{"type":"lights","target":"all","intent":{"on":false}},{"type":"switch","ref":"sw-…","action":"off"},{"type":"delay","ms":500}]}`. Action types: `lights` (target + intent, as above), `device` (`ref` + exact WLED `state`), `all` (exact `state` for every controller), `switch`, `all_off`, `delay` (≤ 10 s) |
| PUT / DELETE | `/api/scenes/<id>` | |
| POST | `/api/scenes/test` | `{"actions":[…]}` – run actions without saving them (the editor's *Try it*) |
| POST | `/api/scenes/<id>/run` | a scene runs on the request thread, so its `delay` actions share a 30 s budget; waits past that are skipped and reported, the actions after them still run |
| GET / PUT | `/api/settings` | `wled.poll_interval_s`, `wled.request_timeout_s`, `gpio.hold_timeout_s`, `gpio.interlock_dead_time_ms`, `gpio.backend`, `server.port`, `server.allowed_hosts` (extra host names the dashboard answers to), `setup_complete` |
| POST | `/api/settings/password` | `{"current":"…","password":"new or empty to remove"}` |
| POST | `/api/login`, `/api/logout` · GET `/api/auth` | |

## System

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/system` | version, Pi model, addresses, uptime, temperature, memory, GPIO backend |
| GET | `/api/system/logs?limit=200` | in-memory log tail |
| GET | `/api/backup` | download `config.json` (secret key, password hash and API tokens stripped) |
| POST | `/api/restore` | upload a backup (multipart `file` or raw JSON body). Switches and the whole emergency-stop section must validate or the restore is refused with **400** – a backup must not be able to quietly remove a stop. Unusable tiles, scenes, groups and schedules are dropped instead; restored schedules start fresh (nothing "missed" fires); the current password and API tokens are kept. |
| GET | `/api/backups` · POST `/api/backups/<name>/restore` | automatic snapshots |
| POST | `/api/system/restart`, `/reboot`, `/update/check`, `/update` | need the installed helper |
| POST | `/api/system/shutdown-all` | *All off*: every relay off, every light off (lights marked offline are tried too). Returns `ok`, `failed` and a `message` naming any light that was not reached. Convenience, not a latch. |
| GET | `/healthz` | `{"ok":true,"version":"…"}` |
