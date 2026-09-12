#!/usr/bin/env bash
# Pre-#743 detect_omarchy_major: unanchored 3/4 match, skip version files when
# `omarchy version` printed anything. `dev (b280f130)` becomes 3.
detect_omarchy_major() {
  local output="" version_file

  if command -v omarchy >/dev/null 2>&1; then
    output=$(omarchy version 2>/dev/null || true)
  fi

  if [[ -z $output ]]; then
    for version_file in /usr/share/omarchy/version "$HOME/.local/share/omarchy/version"; do
      if [[ -r $version_file ]]; then
        output=$(<"$version_file")
        break
      fi
    done
  fi

  if [[ $output =~ ([34])([.][0-9]+)* ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
  return 0
}

if [[ ${STRATA_INSTALLER_TESTING:-0} != 1 ]]; then
  echo "fixture is not a real installer" >&2
  exit 1
fi
