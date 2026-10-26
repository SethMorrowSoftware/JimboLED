#!/usr/bin/env bash
# Update JimboLED to the latest version:   sudo bash update.sh
# Downloads the newest code and re-runs the (idempotent) installer.
# Your settings in /var/lib/jimboled are never touched.
#
# Everything runs inside main(), so bash has read the whole script before
# `git pull` replaces this very file on disk.
set -euo pipefail

main() {
  [ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"
  local SRC OWNER
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  OWNER="${SUDO_USER:-}"
  if [ -z "$OWNER" ] || [ "$OWNER" = root ]; then OWNER="$(stat -c '%U' "$SRC")"; fi
  # git refuses to work in a clone owned by somebody else, so run it as the owner.
  as_owner() { if [ "$OWNER" = root ]; then "$@"; else sudo -u "$OWNER" -H "$@"; fi; }

  if [ -d "$SRC/.git" ]; then
    echo "▸ Downloading the latest JimboLED…"
    if ! as_owner git -C "$SRC" fetch --quiet origin; then
      echo "! Could not reach GitHub, so nothing was downloaded. Check the Pi's internet connection" >&2
      echo "  (for example: ping -c3 github.com) and run this again. JimboLED keeps running as it is." >&2
      exit 1
    fi
    # Follow the repository's main branch.  A copy left on an older branch
    # that has since been merged would otherwise report "Already up to date"
    # for ever and never get another fix.
    as_owner git -C "$SRC" remote set-head origin --auto >/dev/null 2>&1 || true
    local DEFAULT CURRENT
    DEFAULT="$(as_owner git -C "$SRC" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    DEFAULT="${DEFAULT#origin/}"
    CURRENT="$(as_owner git -C "$SRC" rev-parse --abbrev-ref HEAD)"
    if [ -n "$DEFAULT" ] && [ "$CURRENT" != "$DEFAULT" ]; then
      if as_owner git -C "$SRC" merge-base --is-ancestor HEAD "origin/$DEFAULT"; then
        echo "▸ Moving from branch '$CURRENT' to the main branch '$DEFAULT'"
        if as_owner git -C "$SRC" show-ref --verify --quiet "refs/heads/$DEFAULT"; then
          as_owner git -C "$SRC" checkout --quiet "$DEFAULT"
        else
          as_owner git -C "$SRC" checkout --quiet -b "$DEFAULT" --track "origin/$DEFAULT"
        fi || { echo "! Could not switch branches. If you edited files in $SRC, run: git -C \"$SRC\" stash" >&2; exit 1; }
      else
        echo "! This copy is on branch '$CURRENT', which has changes the main branch '$DEFAULT' doesn't." >&2
        echo "  Updating '$CURRENT' only." >&2
      fi
    fi
    as_owner git -C "$SRC" pull --ff-only --quiet \
      || { echo "! git pull failed. If you edited files in $SRC, run: git -C \"$SRC\" stash" >&2; exit 1; }
    echo "  now at $(as_owner git -C "$SRC" log -1 --format='%h – %s' | cut -c1-72)"
  else
    echo "! $SRC is not a git checkout; re-installing the files that are here."
  fi
  exec bash "$SRC/install.sh" --quiet "$@"
}

main "$@"
exit
