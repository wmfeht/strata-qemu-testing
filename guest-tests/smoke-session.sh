#!/usr/bin/env bash
# Guest session smoke.
# Pick Type=wayland Class=user State=active Seat=seat0.
# Never select SSH $XDG_SESSION_ID. Type=x11 is a hard fail.
# WAYLAND_DISPLAY must be a socket, not a leftover lock.
# Compositor process must be present (gnome-shell on ubuntu-2404).
set -euo pipefail

uid="${SMOKE_UID:-$(id -u)}"
export XDG_RUNTIME_DIR="${SMOKE_RUNTIME_DIR:-/run/user/${uid}}"
compositor="${SMOKE_COMPOSITOR:-gnome-shell}"

# SSH logind session is Type=tty (or unspecified). Do not target it.
ssh_sid="${XDG_SESSION_ID:-}"

graphical=""
while read -r sid; do
  [ -n "$sid" ] || continue
  t="$(loginctl show-session "$sid" -p Type --value)"
  c="$(loginctl show-session "$sid" -p Class --value)"
  s="$(loginctl show-session "$sid" -p State --value)"
  seat="$(loginctl show-session "$sid" -p Seat --value)"
  if [ "$t" = "x11" ]; then
    echo "Type=x11 is a hard fail (session $sid)" >&2
    exit 1
  fi
  if [ -n "$ssh_sid" ] && [ "$sid" = "$ssh_sid" ]; then
    continue
  fi
  if [ "$t" = "wayland" ] && [ "$c" = "user" ] && [ "$s" = "active" ] && [ "$seat" = "seat0" ]; then
    graphical="$sid"
    break
  fi
done < <(loginctl --no-legend list-sessions | awk -v u="$uid" '$2==u {print $1}')

if [ -z "$graphical" ]; then
  echo "no active wayland seat0 session" >&2
  exit 1
fi

export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
busctl --user status >/dev/null

shopt -s nullglob
WAYLAND_DISPLAY=""
for candidate in "${XDG_RUNTIME_DIR}"/wayland-*; do
  [[ -S "$candidate" ]] || continue
  WAYLAND_DISPLAY="$(basename "$candidate")"
  break
done
export WAYLAND_DISPLAY
if [ -z "$WAYLAND_DISPLAY" ]; then
  echo "no wayland socket" >&2
  exit 1
fi

if [ -d "${XDG_RUNTIME_DIR}/hypr" ]; then
  export HYPRLAND_INSTANCE_SIGNATURE="$(
    find "${XDG_RUNTIME_DIR}/hypr" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' \
      | sort -n | tail -n1 | cut -d' ' -f2-
  )"
fi

if ! pgrep -x "$compositor" >/dev/null 2>&1; then
  echo "compositor process missing: ${compositor}" >&2
  exit 1
fi

printf 'SESSION_ID=%s\n' "$graphical"
printf 'XDG_RUNTIME_DIR=%s\n' "$XDG_RUNTIME_DIR"
printf 'DBUS_SESSION_BUS_ADDRESS=%s\n' "$DBUS_SESSION_BUS_ADDRESS"
printf 'WAYLAND_DISPLAY=%s\n' "$WAYLAND_DISPLAY"
if [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  printf 'HYPRLAND_INSTANCE_SIGNATURE=%s\n' "$HYPRLAND_INSTANCE_SIGNATURE"
fi
