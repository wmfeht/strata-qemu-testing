#!/usr/bin/env bash
# Probe detect_omarchy_major / omarchy_major_from from a sourced install.sh.
# Isolated cases use a throwaway HOME and a fake `omarchy` on PATH. They do
# not rewrite Hyprland bindings or run the real installer.
# Inputs: INSTALL_SH, SMOKE_OMARCHY_CASE (live|token|command),
#         SMOKE_OMARCHY_OUTPUT (token/command), SMOKE_USER_VERSION (optional).
set -euo pipefail

install_sh="${INSTALL_SH:-/tmp/strata-install.sh}"
case_name="${SMOKE_OMARCHY_CASE:-live}"

if [[ ! -f $install_sh ]]; then
  echo "missing install.sh: ${install_sh}" >&2
  exit 1
fi

print_result() {
  local major=$1
  printf 'SMOKE_OMARCHY_CASE=%s\n' "$case_name"
  printf 'DETECTED_MAJOR=%s\n' "$major"
}

if [[ $case_name == live ]]; then
  major="$(
    STRATA_INSTALLER_TESTING=1 bash -c 'source "$1"; detect_omarchy_major' \
      bash "$install_sh"
  )"
  print_result "$major"
  exit 0
fi

if [[ $case_name == token ]]; then
  output="${SMOKE_OMARCHY_OUTPUT:?}"
  set +e
  major="$(
    STRATA_INSTALLER_TESTING=1 bash -c \
      'source "$1"; omarchy_major_from "$SMOKE_OMARCHY_OUTPUT"' \
      bash "$install_sh"
  )"
  rc=$?
  set -e
  if ((rc == 127)); then
    echo "omarchy_major_from missing in ${install_sh}" >&2
    exit 1
  fi
  if ((rc != 0 && rc != 1)); then
    echo "omarchy_major_from failed (${rc})" >&2
    exit 1
  fi
  print_result "$major"
  exit 0
fi

if [[ $case_name != command ]]; then
  echo "unknown SMOKE_OMARCHY_CASE: ${case_name}" >&2
  exit 1
fi

output="${SMOKE_OMARCHY_OUTPUT:?}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
home="$tmp/home"
mkdir -p "$home"
bindir="$tmp/bin"
mkdir -p "$bindir"
printf '%s\n' "$output" >"$tmp/omarchy-version-out"
cat >"$bindir/omarchy" <<EOF
#!/usr/bin/env bash
cat '$tmp/omarchy-version-out'
EOF
chmod +x "$bindir/omarchy"

if [[ -n ${SMOKE_USER_VERSION:-} ]]; then
  version_dir="$home/.local/share/omarchy"
  mkdir -p "$version_dir"
  printf '%s\n' "$SMOKE_USER_VERSION" >"$version_dir/version"
fi

major="$(
  HOME="$home" PATH="$bindir:$PATH" \
    STRATA_INSTALLER_TESTING=1 bash -c 'source "$1"; detect_omarchy_major' \
    bash "$install_sh"
)"
print_result "$major"
