#!/usr/bin/env bash
# Guest install smoke (design contract).
# Save install.sh, then exec it. Never pipe curl into bash.
# Flags: --non-interactive --with-desktop-entry --without-file-chooser
# Optional: --archive PATH (or INSTALL_ARCHIVE). Extra matching flags from
# the host command are accepted so they appear in recorded guest argv.
set -euo pipefail

URL="${INSTALL_SH_URL:-https://raw.githubusercontent.com/lgse/strata/main/install.sh}"
dest="${INSTALL_SH_DEST:-/tmp/strata-install.sh}"

if [[ "${SMOKE_FORBID_OMARCHY:-}" == "1" ]]; then
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
fi

curl -fsSL "$URL" -o "$dest"
digest="$(sha256sum "$dest" | awk '{print $1}')"
printf 'INSTALL_SH_SHA256=%s\n' "$digest"

flags=(--non-interactive --with-desktop-entry --without-file-chooser)
archive="${INSTALL_ARCHIVE:-}"
passthrough=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)
      if [[ $# -lt 2 ]]; then
        echo "--archive requires a path" >&2
        exit 1
      fi
      archive="$2"
      shift 2
      ;;
    --non-interactive|--with-desktop-entry|--without-file-chooser)
      shift
      ;;
    *)
      passthrough+=("$1")
      shift
      ;;
  esac
done

if [[ -n "$archive" ]]; then
  flags+=(--archive "$archive")
fi
if [[ ${#passthrough[@]} -gt 0 ]]; then
  flags+=("${passthrough[@]}")
fi

bash "$dest" "${flags[@]}"
test -x "${HOME}/.local/bin/strata"
