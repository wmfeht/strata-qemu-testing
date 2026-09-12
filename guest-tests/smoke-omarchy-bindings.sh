#!/usr/bin/env bash
# After install.sh (or a sourced configure_omarchy_bindings), assert Hyprland
# bindings match the Omarchy major.
# 4 → ~/.config/hypr/bindings.lua with the installer marker; 3 → bindings.conf.
# The unused sibling file must not carry the marker (lgse/strata#743 / #652).
# SMOKE_WRITE_BINDINGS=1 sources INSTALL_SH and writes the bindings first.
set -euo pipefail

major="${SMOKE_OMARCHY_MAJOR:?}"
marker="strata-installer: file-manager start"
lua="${HOME}/.config/hypr/bindings.lua"
conf="${HOME}/.config/hypr/bindings.conf"

if [[ ${SMOKE_WRITE_BINDINGS:-} == 1 ]]; then
  install_sh="${INSTALL_SH:-/tmp/strata-install.sh}"
  if [[ ! -f $install_sh ]]; then
    echo "missing install.sh: ${install_sh}" >&2
    exit 1
  fi
  BIN_PATH="${BIN_PATH:-$HOME/.local/bin/strata}"
  mkdir -p "$(dirname "$BIN_PATH")"
  if [[ ! -x $BIN_PATH ]]; then
    printf '%s\n' '#!/bin/sh' >"$BIN_PATH"
    chmod +x "$BIN_PATH"
  fi
  export BIN_PATH
  STRATA_INSTALLER_TESTING=1 bash -c \
    'source "$1"; configure_omarchy_bindings "$2"' \
    bash "$install_sh" "$major"
fi

has_marker() {
  local path=$1
  [[ -r $path ]] && grep -q "$marker" "$path"
}

if [[ $major == 4 ]]; then
  if ! has_marker "$lua"; then
    echo "omarchy-4 bindings.lua missing installer marker" >&2
    exit 1
  fi
  if has_marker "$conf"; then
    echo "omarchy-4 wrote installer bindings to bindings.conf (Omarchy 4 never loads it)" >&2
    exit 1
  fi
  printf 'BINDINGS_KIND=lua\n'
  printf 'BINDINGS_PATH=%s\n' "$lua"
  exit 0
fi

if [[ $major == 3 ]]; then
  if ! has_marker "$conf"; then
    echo "omarchy-3 bindings.conf missing installer marker" >&2
    exit 1
  fi
  if ! grep -q 'bindd = SUPER SHIFT, F' "$conf"; then
    echo "omarchy-3 bindings.conf missing SUPER SHIFT F bindd" >&2
    exit 1
  fi
  if has_marker "$lua"; then
    echo "omarchy-3 wrote installer bindings to bindings.lua (Omarchy 3 never loads it)" >&2
    exit 1
  fi
  printf 'BINDINGS_KIND=conf\n'
  printf 'BINDINGS_PATH=%s\n' "$conf"
  exit 0
fi

echo "SMOKE_OMARCHY_MAJOR must be 3 or 4 (got ${major})" >&2
exit 1
