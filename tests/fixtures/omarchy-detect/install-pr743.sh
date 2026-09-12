#!/usr/bin/env bash
# Fixture copy of lgse/strata#743 Omarchy detection. Not a real installer.
omarchy_major_from() {
  if [[ $1 =~ (^|[^0-9.])([34])[.][0-9]+ ]]; then
    printf '%s\n' "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

detect_omarchy_major() {
  local output="" version_file

  if command -v omarchy >/dev/null 2>&1; then
    output=$(omarchy version 2>/dev/null || true)
    if omarchy_major_from "$output"; then
      return 0
    fi
  fi

  for version_file in /usr/share/omarchy/version "$HOME/.local/share/omarchy/version"; do
    if [[ -r $version_file ]] && omarchy_major_from "$(<"$version_file")"; then
      return 0
    fi
  done

  return 0
}

configure_omarchy_bindings() {
  local major=$1 bindings
  if [[ $major == 4 ]]; then
    bindings=$HOME/.config/hypr/bindings.lua
  else
    bindings=$HOME/.config/hypr/bindings.conf
  fi
  install -d "$(dirname "$bindings")"
  if grep -q 'strata-installer: file-manager start' "$bindings" 2>/dev/null; then
    return
  fi
  : "${BIN_PATH:=$HOME/.local/bin/strata}"
  if [[ $major == 4 ]]; then
    cat >>"$bindings" <<EOF

-- strata-installer: file-manager start
o.bind("SUPER + SHIFT + F", "File manager", { launch = "$BIN_PATH" })
-- strata-installer: file-manager end
EOF
  else
    cat >>"$bindings" <<EOF

# strata-installer: file-manager start
bindd = SUPER SHIFT, F, File manager, exec, uwsm-app -- $BIN_PATH
# strata-installer: file-manager end
EOF
  fi
}

if [[ ${STRATA_INSTALLER_TESTING:-0} != 1 ]]; then
  echo "fixture is not a real installer" >&2
  exit 1
fi
