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
| POST | `/api/gpio/all-off` | release every relay |
| POST | `/api/gpio/test` | `{"pin":22,"active_high":true,"duration_ms":400}` – pulse an unconfigured pin |
| GET | `/api/gpio/events` | recent on/off log with reasons |

A momentary switch **cannot** be turned on with `on`; it only runs while
heartbeats arrive, and it always has a maximum on-time.

## Dashboard, scenes, settings

| Method | Path | Notes |
| --- | --- | --- |
| GET / PUT | `/api/dashboard` | `title`, `subtitle`, `theme` (midnight/graphite/ocean/oled), `accent`, `density`, `show_offline`, `show_clock`, `tiles[]`, `scenes[]` |
| POST | `/api/dashboard/tiles` | add a `heading`, `note`, `clock` or `all` tile |
| PUT / DELETE | `/api/dashboard/tiles/<id>` | `size` (s/m/l/xl), `hidden`, `name`, `icon`, `color`, `opts` |
| POST | `/api/dashboard/order` | `{"ids":[…]}` |
| GET / POST | `/api/scenes` | `{"name":"Goodnight","icon":"moon","actions":[{"type":"all","state":{"on":false}},{"type":"switch","ref":"sw-…","action":"off"},{"type":"delay","ms":500}]}` |
| PUT / DELETE | `/api/scenes/<id>` | |
| POST | `/api/scenes/<id>/run` | |
| GET / PUT | `/api/settings` | `wled.poll_interval_s`, `wled.request_timeout_s`, `gpio.hold_timeout_s`, `gpio.interlock_dead_time_ms`, `gpio.backend`, `server.port`, `setup_complete` |
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
| POST | `/api/system/shutdown-all` | panic: every relay off, every light off |
| GET | `/healthz` | `{"ok":true,"version":"…"}` |
