#!/usr/bin/env bash
# Drive Strata Settings to the About page. Ctrl+, opens Settings with
# General focused after the first Tab; four more Tabs reach About; Space
# activates it. Does not launch Strata or take a screenshot.
#
# Inputs: SMOKE_COMPOSITOR (Hyprland|gnome-shell)
# Stdout: INPUT=wtype|hyprctl-open
# Exit 2 if this guest has no in-session key tool (host falls back to QMP).
set -euo pipefail

compositor="${SMOKE_COMPOSITOR:-gnome-shell}"
bus="io.github.lgse.Strata"
# General, Keybindings, Theme & appearance, Updates, About
tabs="${SMOKE_ABOUT_TABS:-5}"

if command -v wtype >/dev/null 2>&1; then
  wtype -M ctrl -k comma -m ctrl
  sleep 0.6
  i=0
  while [[ $i -lt $tabs ]]; do
    wtype -k Tab
    sleep 0.08
    i=$((i + 1))
  done
  wtype -k space
  printf 'INPUT=wtype\n'
  exit 0
fi

if [[ $compositor == Hyprland ]] && command -v hyprctl >/dev/null 2>&1; then
  hyprctl dispatch sendshortcut "CTRL, comma, class:${bus}"
  # hyprctl cannot Tab through the sidebar; host QMP sends Tab × N + space.
  printf 'INPUT=hyprctl-open\n'
  exit 0
fi

echo "INPUT_TOOL=missing" >&2
printf 'INPUT=missing\n'
exit 2
