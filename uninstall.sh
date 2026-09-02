#!/usr/bin/env bash
# Remove JimboLED:   sudo bash uninstall.sh [--purge]
# Your settings in /var/lib/jimboled are KEPT unless you pass --purge.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
if [ "$PURGE" = 1 ] && [ -t 0 ]; then
  printf 'Really delete all JimboLED settings too? [y/N] '; read -r a; case "$a" in y|Y) ;; *) echo "Cancelled."; exit 0 ;; esac
fi
[ -x /opt/jimboled/app/bin/jimboled-helper ] && /opt/jimboled/app/bin/jimboled-helper pins-safe >/dev/null 2>&1 || true
systemctl disable --now jimboled.service >/dev/null 2>&1 || true
systemctl stop jimboled-update.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/jimboled.service /etc/sudoers.d/jimboled /etc/avahi/services/jimboled.service
rm -f /etc/NetworkManager/conf.d/99-jimboled-wifi-powersave.conf /etc/NetworkManager/dispatcher.d/99-jimboled-wifi-powersave
nmcli general reload conf >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl reset-failed jimboled.service >/dev/null 2>&1 || true
rm -rf /opt/jimboled
for c in /boot/firmware/config.txt /boot/config.txt; do
  if [ -f "$c" ] && grep -q '^# BEGIN JimboLED' "$c"; then
    tmp="$(mktemp)"; awk '/^# BEGIN JimboLED/{skip=1} !skip{print} /^# END JimboLED/{skip=0}' "$c" > "$tmp"; cat "$tmp" > "$c"; rm -f "$tmp"
  fi
done
if [ "$PURGE" = 1 ]; then
  rm -rf /var/lib/jimboled /etc/jimboled
  userdel jimboled >/dev/null 2>&1 || true
  echo "JimboLED and all its settings were removed."
else
  echo "JimboLED removed. Your settings are still in /var/lib/jimboled (run 'sudo bash uninstall.sh --purge' to delete them too)."
fi
