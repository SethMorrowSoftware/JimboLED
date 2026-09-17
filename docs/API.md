# JimboLED REST API

Everything the dashboard does goes through this JSON API, so you can script
JimboLED from Home Assistant, Node-RED, cron jobs or a Stream Deck.

**Rules**

* Base URL: `http://<pi>/api/…`
* Every request that changes something (POST/PUT/PATCH/DELETE) must send the
  header `X-Requested-With: JimboLED` (anti-CSRF). GETs need nothing.
* If a dashboard password is set, log in first: `POST /api/login {"password": "…"}`
  and keep the session cookie.
* Errors are `{"error": "message"}` with a 4xx/5xx status.
* Requests must use an IP address, a plain host name, or a private-style name (`*.local`, `*.lan`, `*.home`, …) in the `Host` header; other names get **421** unless listed in `server.allowed_hosts`. This blocks DNS-rebinding attacks from the internet.
* Hold-to-run switches must receive a heartbeat at least every second (the dashboard sends one every 0.4 s); the relay releases automatically otherwise.

```bash
# turn a controller on at 60 %
curl -s -X POST -H 'X-Requested-With: JimboLED' -H 'Content-Type: application/json' \
  http://jimboled.local/api/devices/wled-a1b2c3/state -d '{"on":true,"bri":153}'
```

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
| POST | `/api/devices/<id>/presets` | `{"name":"Movie","slot":5,"quick_label":"MV","include_brightness":true,"save_segment_bounds":true}` (slot optional = next free) |
| DELETE | `/api/devices/<id>/presets/<slot>` | |
| POST | `/api/devices/<id>/reboot` | |
| POST | `/api/discover` | `{"methods":["mdns","nodes","subnet"],"subnet":"192.168.1.0/24"}` (all optional) |
| GET | `/api/discover` | progress + `found[]` + `passive[]` (controllers heard broadcasting) |

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
| POST | `/api/gpio/test` | `{"pin":22,"active_high":true,"duration_ms":400}` – pulse an unconfigured pin |
| GET | `/api/gpio/events` | recent on/off log with reasons |

A momentary switch **cannot** be turned on with `on`; it only runs while
heartbeats arrive, and it always has a maximum on-time.

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
| PUT | `/api/estop/settings` | `master_name` |

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
| GET / PUT | `/api/dashboard` | `title`, `subtitle`, `theme` (`auto`/midnight/graphite/ocean/oled/daylight/paper), `accent`, `density` (comfortable/compact/roomy), `radius` (sharp/soft/round), `text_scale` (85–150 %), `show_offline`, `show_clock`, `show_estop`, `tiles[]`, `scenes[]` |
| POST | `/api/dashboard/tiles` | add a `heading`, `note`, `clock` or `all` tile |
| PUT / DELETE | `/api/dashboard/tiles/<id>` | `size` (s/m/l/xl), `hidden`, `name`, `icon`, `color`, `opts` |
| POST | `/api/dashboard/order` | `{"ids":[…]}` |
| GET / POST | `/api/scenes` | `{"name":"Goodnight","icon":"moon","actions":[{"type":"all","state":{"on":false}},{"type":"switch","ref":"sw-…","action":"off"},{"type":"delay","ms":500}]}` |
| PUT / DELETE | `/api/scenes/<id>` | |
| POST | `/api/scenes/<id>/run` | |
| GET / PUT | `/api/settings` | `wled.poll_interval_s`, `wled.request_timeout_s`, `gpio.hold_timeout_s`, `gpio.interlock_dead_time_ms`, `gpio.backend`, `server.port`, `server.allowed_hosts` (extra host names the dashboard answers to), `setup_complete` |
| POST | `/api/settings/password` | `{"current":"…","password":"new or empty to remove"}` |
| POST | `/api/login`, `/api/logout` · GET `/api/auth` | |

## System

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/system` | version, Pi model, addresses, uptime, temperature, memory, GPIO backend |
| GET | `/api/system/logs?limit=200` | in-memory log tail |
| GET | `/api/backup` | download `config.json` (secret key stripped) |
| POST | `/api/restore` | upload a backup (multipart `file` or raw JSON body) |
| GET | `/api/backups` · POST `/api/backups/<name>/restore` | automatic snapshots |
| POST | `/api/system/restart`, `/reboot`, `/update/check`, `/update` | need the installed helper |
| POST | `/api/system/shutdown-all` | *All off*: every relay off, every light off. Convenience, not a latch. |
| GET | `/healthz` | `{"ok":true,"version":"…"}` |
