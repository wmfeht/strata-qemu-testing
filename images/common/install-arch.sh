#!/usr/bin/env bash
# Arch installer helper. Flags already exist on install.sh.
# Save the script, then exec it (do not pipe curl into a shell).
# Record the script SHA-256.
set -euo pipefail

URL="https://raw.githubusercontent.com/lgse/strata/main/install.sh"
dest="${INSTALL_SH_DEST:-/tmp/strata-install.sh}"

omarchy_share="${OMARCHY_SHARE:-/usr/share/omarchy}"
omarchy_local="${OMARCHY_LOCAL:-${HOME}/.local/share/omarchy}"
if command -v omarchy >/dev/null 2>&1 \
  || [[ -e "${omarchy_share}" ]] \
  || [[ -e "${omarchy_local}" ]] \
  || [[ -e "${omarchy_share}/version" ]] \
  || [[ -e "${omarchy_local}/version" ]]; then
  echo "omarchy unexpectedly present on arch guest" >&2
  exit 1
fi

curl -fsSL "$URL" -o "$dest"
digest="$(sha256sum "$dest" | awk '{print $1}')"
printf 'INSTALL_SH_SHA256=%s\n' "$digest"
bash "$dest" --non-interactive --with-desktop-entry --without-file-chooser
test -x "${HOME}/.local/bin/strata"
