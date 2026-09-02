#!/usr/bin/env bash
# Update JimboLED to the latest version:   sudo bash update.sh
# Downloads the newest code and re-runs the (idempotent) installer.
# Your settings in /var/lib/jimboled are never touched.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNER="${SUDO_USER:-}"
if [ -z "$OWNER" ] || [ "$OWNER" = root ]; then OWNER="$(stat -c '%U' "$SRC")"; fi
if [ -d "$SRC/.git" ]; then
  echo "▸ Downloading the latest JimboLED…"
  if [ "$OWNER" = root ]; then git -C "$SRC" pull --ff-only; else sudo -u "$OWNER" -H git -C "$SRC" pull --ff-only; fi \
    || { echo "! git pull failed. If you edited files in $SRC, run: git -C \"$SRC\" stash" >&2; exit 1; }
else
  echo "! $SRC is not a git checkout; re-installing the files that are here."
fi
exec bash "$SRC/install.sh" --quiet "$@"
