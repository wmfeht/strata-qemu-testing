#!/usr/bin/env bash
# GNOME bus-name oracle for io.github.lgse.Strata plus gnome-screenshot.
# Does not start Strata from a desktop entry (window-after-install is later).
# A missing screenshot binary is a golden bug, not a compositor timeout.
set -euo pipefail

BUS_NAME="io.github.lgse.Strata"
SCREENSHOT_MISSING="screenshot tool missing; rebuild the golden"
REMOTE_PNG="${SMOKE_SCREENSHOT_PATH:-/tmp/strata-window.png}"

if ! command -v gnome-screenshot >/dev/null 2>&1; then
  echo "${SCREENSHOT_MISSING}" >&2
  exit 1
fi

reply="$(gdbus call --session \
  --dest org.freedesktop.DBus \
  --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.NameHasOwner \
  "${BUS_NAME}")"

gnome-screenshot -f "${REMOTE_PNG}"

case "${reply}" in
  *true*) ;;
  *)
    echo "bus name ${BUS_NAME} not owned: ${reply}" >&2
    exit 1
    ;;
esac
