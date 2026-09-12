#!/usr/bin/env bash
# Desktop oracle: GNOME bus-name + gnome-screenshot, or Hyprland hyprctl
# class + grim. Does not start Strata from a desktop entry (launch is host-side).
# A missing screenshot binary is a golden bug, not a compositor timeout.
set -euo pipefail

BUS_NAME="io.github.lgse.Strata"
SCREENSHOT_MISSING="screenshot tool missing; rebuild the golden"
REMOTE_PNG="${SMOKE_SCREENSHOT_PATH:-/tmp/strata-window.png}"
ORACLE="${SMOKE_ORACLE:-gnome}"

if [ "$ORACLE" = "hyprland" ]; then
  if ! command -v grim >/dev/null 2>&1; then
    echo "${SCREENSHOT_MISSING}" >&2
    exit 1
  fi
  clients="$(hyprctl clients -j 2>/dev/null || true)"
  grim "${REMOTE_PNG}"
  if ! printf '%s\n' "${clients}" | jq -e \
      --arg c "${BUS_NAME}" '.[] | select(.class == $c)' >/dev/null; then
    echo "hyprctl class ${BUS_NAME} not present" >&2
    exit 1
  fi
  exit 0
fi

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
