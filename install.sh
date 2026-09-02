#!/usr/bin/env bash
# =============================================================================
#  JimboLED installer – safe to run as many times as you like.
#
#      git clone https://github.com/SethMorrowSoftware/JimboLED.git
#      cd JimboLED
#      sudo bash install.sh
#
#  Options:  --hostname NAME   make this Pi reachable at http://NAME.local
#            --port N          listen on port N (default 80, or 8080 if 80 is taken)
#            --quiet           no questions, less output (used by update.sh / the web UI)
#
#  What it does (idempotently):
#    • installs the few system packages needed (python3, venv, gpiozero, lgpio, avahi)
#    • copies the app to /opt/jimboled/app and builds a venv in /opt/jimboled/venv
#    • creates a service user "jimboled" in the gpio group
#    • keeps ALL user data in /var/lib/jimboled – never touched on re-install
#    • installs and (re)starts the systemd service
#    • installs a tiny privileged helper so the dashboard can restart/update/reboot
# =============================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"

APP_NAME="jimboled"
SERVICE_USER="jimboled"
INSTALL_ROOT="/opt/jimboled"
APP_DIR="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/venv"
DATA_DIR="/var/lib/jimboled"
ETC_DIR="/etc/jimboled"
ENV_FILE="$ETC_DIR/install.env"
UNIT="/etc/systemd/system/${APP_NAME}.service"
SUDOERS="/etc/sudoers.d/${APP_NAME}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUIET=0; WANT_PORT=""; NEW_HOSTNAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --quiet|-y|--yes) QUIET=1; shift ;;
    --port) WANT_PORT="$2"; shift 2 ;;
    --port=*) WANT_PORT="${1#*=}"; shift ;;
    --hostname) NEW_HOSTNAME="$2"; shift 2 ;;
    --hostname=*) NEW_HOSTNAME="${1#*=}"; shift ;;
    -h|--help) sed -n '3,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- output
if [ -t 1 ]; then BOLD=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; RESET=$'\e[0m'; else BOLD=; DIM=; GREEN=; YELLOW=; RED=; RESET=; fi
say()  { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
step() { say ""; say "${BOLD}▸ $*${RESET}"; }
ok()   { say "  ${GREEN}✓${RESET} $*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }
trap 'die "Install failed on line $LINENO. Nothing was lost – it is safe to run this script again."' ERR

# ---------------------------------------------------------------- sanity
[ -f "$SRC/jimboled/__main__.py" ] || die "Run this from the JimboLED folder you cloned (jimboled/ package not found next to install.sh)."
command -v systemctl >/dev/null 2>&1 || die "This installer needs systemd (Raspberry Pi OS, Debian, Ubuntu)."
mkdir -p /run/lock
exec 9>"/run/lock/${APP_NAME}-install.lock"
flock -n 9 || die "Another JimboLED install/update is already running."

# The clone's owner runs git (git refuses repos owned by someone else).
OWNER="$(stat -c '%U' "$SRC")"
as_owner() { if [ "$OWNER" = root ]; then "$@"; else sudo -u "$OWNER" -H "$@"; fi; }

IS_PI=0
grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null && IS_PI=1
PI_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"

say "${BOLD}JimboLED installer${RESET}"
say "${DIM}source: $SRC (owner: $OWNER)${RESET}"
if [ "$IS_PI" = 1 ]; then say "${DIM}board:  $PI_MODEL${RESET}"; else warn "Not a Raspberry Pi – GPIO will run in simulation mode."; fi

# ---------------------------------------------------------- apt packages
step "System packages"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  PKGS=(python3 python3-venv python3-pip git rsync curl avahi-daemon avahi-utils iw)
  [ "$IS_PI" = 1 ] && PKGS+=(python3-gpiozero python3-lgpio)   # compiled for this Pi; never from pip
  # pinctrl (raspi-utils) lets the helper drive relay pins safe after the service stops; optional.
  if [ "$IS_PI" = 1 ] && apt-cache show raspi-utils >/dev/null 2>&1; then PKGS+=(raspi-utils); fi
  MISSING=()
  for p in "${PKGS[@]}"; do dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || MISSING+=("$p"); done
  if [ "${#MISSING[@]}" -gt 0 ]; then
    say "  installing: ${MISSING[*]} ${DIM}(a few minutes on a Pi Zero)${RESET}"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${MISSING[@]}"
    ok "installed ${#MISSING[@]} package(s)"
  else
    ok "all present"
  fi
else
  warn "apt-get not found – make sure python3 (3.9+), python3-venv and rsync are installed."
fi
command -v rsync >/dev/null 2>&1 || die "rsync is required (apt install rsync)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null || die "Python 3.9 or newer is required."

# ---------------------------------------------------------- service user
step "Service user"
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin --comment "JimboLED service" "$SERVICE_USER"
  ok "created user $SERVICE_USER"
else
  ok "user $SERVICE_USER exists"
fi
SUPP_GROUPS=""
for g in gpio i2c spi; do
  if getent group "$g" >/dev/null; then
    id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$g" || usermod -aG "$g" "$SERVICE_USER"
    SUPP_GROUPS="$SUPP_GROUPS $g"
  fi
done
[ -n "$SUPP_GROUPS" ] && ok "groups:${SUPP_GROUPS}" || ok "no hardware groups on this system"

# ------------------------------------------------------------ app files
step "Application files → $APP_DIR"
install -d -m 0755 -o root -g root "$INSTALL_ROOT" "$ETC_DIR"
rsync -a --delete \
  --exclude '.git' --exclude 'data/' --exclude 'venv/' --exclude '.venv/' --exclude '__pycache__' \
  --exclude '*.pyc' --exclude '.pytest_cache' --exclude 'node_modules' --exclude 'tests/' \
  --chown=root:root --chmod=D755,F644 "$SRC/" "$APP_DIR/"
install -m 0755 -o root -g root "$SRC/bin/jimboled-helper" "$APP_DIR/bin/jimboled-helper"
ok "synced"

# ------------------------------------------------------------------ venv
step "Python environment → $VENV_DIR"
PY_SYS="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if [ -x "$VENV_DIR/bin/python" ]; then
  PY_VENV="$("$VENV_DIR/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo broken)"
  if [ "$PY_VENV" != "$PY_SYS" ]; then warn "Python changed ($PY_VENV → $PY_SYS); rebuilding the environment"; rm -rf "$VENV_DIR"; fi
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv --system-site-packages "$VENV_DIR"
  ok "created"
else
  ok "exists"
fi
say "  installing Python packages ${DIM}(quick if nothing changed)${RESET}"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PIP_NO_INPUT=1
"$VENV_DIR/bin/python" -m pip install -q --upgrade --prefer-binary -r "$APP_DIR/requirements.txt" \
  || die "pip could not install the Python packages. Check the internet connection and run again."
"$VENV_DIR/bin/python" -c "import flask, waitress, requests, gpiozero" || die "Python packages did not install correctly."
if [ "$IS_PI" = 1 ]; then
  # lgpio creates a notification pipe in its working directory on import: keep that out of the clone.
  if (cd / && LG_WD=/tmp "$VENV_DIR/bin/python" -c "import lgpio" 2>/dev/null); then ok "gpiozero + lgpio ready"; else warn "lgpio is not importable – GPIO will be simulated. Try: sudo apt install python3-lgpio"; fi
  rm -f "$SRC"/.lgd-nfy* 2>/dev/null || true
fi
ok "packages ready"

# ------------------------------------------------------------------ port
step "Network port"
PORT="$WANT_PORT"
if [ -z "$PORT" ] && [ -f "$DATA_DIR/config.json" ]; then
  PORT="$("$VENV_DIR/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("server",{}).get("port",""))' "$DATA_DIR/config.json" 2>/dev/null || true)"
fi
if [ -z "$PORT" ]; then
  PORT=80
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '[:.]80$' && ! systemctl is-active --quiet "$APP_NAME"; then
    warn "port 80 is already used by another program; using 8080 instead"
    PORT=8080
  fi
fi
ok "port $PORT"

# ------------------------------------------------------------- user data
step "User data → $DATA_DIR ${DIM}(preserved across installs)${RESET}"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR" "$DATA_DIR/backups"
if [ ! -e "$DATA_DIR/config.json" ]; then
  "$VENV_DIR/bin/python" - "$SRC/config/config.example.json" "$DATA_DIR/config.json" "$PORT" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg.setdefault("server", {})["port"] = int(sys.argv[3])
json.dump(cfg, open(sys.argv[2], "w"), indent=2)
PY
  chmod 0640 "$DATA_DIR/config.json"
  ok "created a fresh config.json"
else
  # Honour an explicit --port even for an existing install.
  if [ -n "$WANT_PORT" ]; then
    "$VENV_DIR/bin/python" - "$DATA_DIR/config.json" "$WANT_PORT" <<'PY'
import json, sys
p = sys.argv[1]; cfg = json.load(open(p)); cfg.setdefault("server", {})["port"] = int(sys.argv[2]); json.dump(cfg, open(p, "w"), indent=2)
PY
  fi
  ok "existing settings kept"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
if [ ! -e "$ETC_DIR/jimboled.env" ]; then
  cat > "$ETC_DIR/jimboled.env" <<'ENV'
# Optional overrides for the JimboLED service. After editing: sudo systemctl restart jimboled
#JIMBOLED_THREADS=8
#GPIOZERO_PIN_FACTORY=mock
ENV
fi

# ------------------------------------------------------- install.env / sudo
GIT_REV="$(as_owner git -C "$SRC" rev-parse --short HEAD 2>/dev/null || true)"
GIT_BRANCH="$(as_owner git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
REPO_URL="$(as_owner git -C "$SRC" remote get-url origin 2>/dev/null || true)"
cat > "$ENV_FILE" <<ENV
# Written by install.sh – read by update.sh and the dashboard's System page. Not loaded by systemd.
JIMBOLED_SRC="$SRC"
JIMBOLED_APP="$APP_DIR"
JIMBOLED_VENV="$VENV_DIR"
JIMBOLED_DATA="$DATA_DIR"
JIMBOLED_USER="$SERVICE_USER"
JIMBOLED_OWNER="$OWNER"
JIMBOLED_PORT="$PORT"
JIMBOLED_REV="$GIT_REV"
JIMBOLED_BRANCH="$GIT_BRANCH"
JIMBOLED_REPO_URL="$REPO_URL"
JIMBOLED_INSTALLED_AT="$(date -Is)"
ENV
chmod 0644 "$ENV_FILE"
printf '%s ALL=(root) NOPASSWD: %s/bin/jimboled-helper\n' "$SERVICE_USER" "$APP_DIR" > "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null || die "generated sudoers entry is invalid"
install -m 0440 -o root -g root "$SUDOERS.tmp" "$SUDOERS"; rm -f "$SUDOERS.tmp"

# --------------------------------------------------------------- systemd
step "Service"
PIN_FACTORY="lgpio"; [ "$IS_PI" = 1 ] || PIN_FACTORY="mock"
SUPP_LINE=""; [ -n "$SUPP_GROUPS" ] && SUPP_LINE="SupplementaryGroups=${SUPP_GROUPS# }"
sed -e "s|__USER__|$SERVICE_USER|g" -e "s|__APP__|$APP_DIR|g" -e "s|__VENV__|$VENV_DIR|g" -e "s|__DATA__|$DATA_DIR|g" \
    -e "s|__PIN_FACTORY__|$PIN_FACTORY|g" -e "s|__SUPP_GROUPS__|$SUPP_LINE|g" \
    "$SRC/system/jimboled.service" > "$UNIT.tmp"
if ! cmp -s "$UNIT.tmp" "$UNIT" 2>/dev/null; then install -m 0644 "$UNIT.tmp" "$UNIT"; fi
rm -f "$UNIT.tmp"
systemctl daemon-reload
systemctl enable "$APP_NAME.service" >/dev/null 2>&1
systemctl restart "$APP_NAME.service"
HEALTHY=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 1
  if curl -fsS -m 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then HEALTHY=1; break; fi
done
if [ "$HEALTHY" = 1 ] && systemctl is-active --quiet "$APP_NAME.service"; then
  ok "jimboled.service is running and answering on port $PORT"
else
  journalctl -u "$APP_NAME" -n 40 --no-pager || true
  die "the service did not come up – the log above usually says why"
fi
# Boot-time relay safety: record configured pins in config.txt (no-op when none are configured yet).
if [ "$IS_PI" = 1 ]; then
  SPEC="$(JIMBOLED_INSTALL_ENV="$ENV_FILE" "$APP_DIR/bin/jimboled-helper" pins-safe >/dev/null 2>&1; "$VENV_DIR/bin/python" - "$DATA_DIR/config.json" <<'PY' 2>/dev/null || echo none
import json, sys
cfg = json.load(open(sys.argv[1]))
print(",".join(f"{s['pin']}={'dl' if s.get('active_high', True) else 'dh'}" for s in cfg.get("gpio", {}).get("switches", []) if isinstance(s.get("pin"), int)) or "none")
PY
)"
  JIMBOLED_INSTALL_ENV="$ENV_FILE" "$APP_DIR/bin/jimboled-helper" bootpins "${SPEC:-none}" >/dev/null 2>&1 || true
fi

# ------------------------------------------------------- hostname / mDNS
if [ -z "$NEW_HOSTNAME" ] && [ "$QUIET" = 0 ] && [ -t 0 ] && [ "$(hostname)" = "raspberrypi" ]; then
  say ""
  printf '  Give this Pi a friendlier name so the dashboard opens at %shttp://jimboled.local%s ? [Y/n] ' "$BOLD" "$RESET"
  read -r answer || answer=""
  case "${answer:-Y}" in y|Y|yes|YES) NEW_HOSTNAME="jimboled" ;; esac
fi
if [ -n "$NEW_HOSTNAME" ]; then
  [[ "$NEW_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "Hostname may only contain lowercase letters, digits and hyphens."
  if [ "$(hostname)" != "$NEW_HOSTNAME" ]; then
    if command -v raspi-config >/dev/null 2>&1; then raspi-config nonint do_hostname "$NEW_HOSTNAME"
    else hostnamectl set-hostname "$NEW_HOSTNAME" && sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts; fi
    ok "hostname changed to $NEW_HOSTNAME (fully effective after a reboot)"
  fi
fi
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
if [ -d /etc/avahi/services ]; then
  cat > "/etc/avahi/services/${APP_NAME}.service.tmp" <<AVAHI
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">JimboLED on %h</name>
  <service><type>_http._tcp</type><port>$PORT</port><txt-record>path=/</txt-record></service>
</service-group>
AVAHI
  cmp -s "/etc/avahi/services/${APP_NAME}.service.tmp" "/etc/avahi/services/${APP_NAME}.service" 2>/dev/null \
    || install -m 0644 "/etc/avahi/services/${APP_NAME}.service.tmp" "/etc/avahi/services/${APP_NAME}.service"
  rm -f "/etc/avahi/services/${APP_NAME}.service.tmp"
fi
[ -n "$NEW_HOSTNAME" ] && systemctl restart avahi-daemon >/dev/null 2>&1 || true

# ------------------------------------------------------- Wi-Fi power save
# Wi-Fi power save makes the Pi sleep through the multicast/broadcast frames
# that mDNS (jimboled.local, WLED _wled._tcp) and WLED node discovery (UDP
# 65506) rely on. Disable it persistently via NetworkManager and right now.
WIFI_IFACES=()
for w in /sys/class/net/*/wireless; do [ -d "$w" ] && WIFI_IFACES+=("$(basename "$(dirname "$w")")"); done
if [ "${#WIFI_IFACES[@]}" -gt 0 ]; then
  step "Wi-Fi power save ${DIM}(keeps .local names and WLED discovery reliable)${RESET}"
  if [ -d /etc/NetworkManager ]; then
    NM_CONF=/etc/NetworkManager/conf.d/99-jimboled-wifi-powersave.conf
    cat > "$NM_CONF.tmp" <<'NM'
# Installed by JimboLED. Wi-Fi power save makes the Pi sleep through the
# multicast/broadcast frames that mDNS and WLED discovery rely on.
# Value MUST be an integer: 2 = disable, 3 = enable, 1 = driver default.
[connection]
wifi.powersave = 2
NM
    if ! cmp -s "$NM_CONF.tmp" "$NM_CONF" 2>/dev/null; then
      install -d -m 0755 /etc/NetworkManager/conf.d
      install -m 0644 -o root -g root "$NM_CONF.tmp" "$NM_CONF"
      nmcli general reload conf >/dev/null 2>&1 || systemctl reload NetworkManager >/dev/null 2>&1 || true
    fi
    rm -f "$NM_CONF.tmp"
    HOOK=/etc/NetworkManager/dispatcher.d/99-jimboled-wifi-powersave
    cat > "$HOOK.tmp" <<'SH'
#!/bin/sh
# Installed by JimboLED: re-assert "power save off" whenever a Wi-Fi interface
# comes up (covers driver re-probes after a firmware crash).
[ "$2" = up ] && [ -d "/sys/class/net/$1/wireless" ] || exit 0
IW=$(command -v iw || echo /usr/sbin/iw)
[ -x "$IW" ] && "$IW" dev "$1" set power_save off
exit 0
SH
    if ! cmp -s "$HOOK.tmp" "$HOOK" 2>/dev/null; then
      install -d -m 0755 /etc/NetworkManager/dispatcher.d
      install -m 0755 -o root -g root "$HOOK.tmp" "$HOOK"
    fi
    rm -f "$HOOK.tmp"
    ok "NetworkManager default set (wifi.powersave = 2)"
  else
    warn "NetworkManager not found – power save switched off now but not persisted"
  fi
  for i in "${WIFI_IFACES[@]}"; do
    if command -v iw >/dev/null 2>&1; then
      iw dev "$i" set power_save off 2>/dev/null || true
      if iw dev "$i" get power_save 2>/dev/null | grep -q 'Power save: off'; then ok "$i: power save off"; fi
    fi
  done
fi

# ------------------------------------------------------------------ done
HOST_SHORT="${NEW_HOSTNAME:-$(hostname -s 2>/dev/null || hostname)}"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SUFFIX=""; [ "$PORT" != 80 ] && SUFFIX=":$PORT"
say ""
say "${GREEN}${BOLD}JimboLED is installed and running.${RESET}"
say ""
say "  Open it from any phone, tablet or computer on your network:"
say "    ${BOLD}http://${HOST_SHORT}.local${SUFFIX}${RESET}   or   ${BOLD}http://${IP:-<pi-ip>}${SUFFIX}${RESET}"
say ""
say "  Useful commands:"
say "    sudo systemctl status jimboled       # is it running?"
say "    sudo journalctl -u jimboled -f       # live log"
say "    cd \"$SRC\" && sudo bash update.sh    # update to the latest version"
say ""
