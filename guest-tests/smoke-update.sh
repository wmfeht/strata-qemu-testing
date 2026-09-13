#!/usr/bin/env bash
# Seed a previous Strata release, then run current install.sh (latest).
# Never pipe curl into bash.
#
# Inputs:
#   SMOKE_UPDATE_PHASE     previous | latest | all (default all)
#   UPDATE_FROM_VERSION    previous release, e.g. 0.15.0 (required for previous)
#   UPDATE_FROM_ARCHIVE    optional host-uploaded previous tarball
#   INSTALL_SH_URL / INSTALL_SH_DEST
#   SMOKE_FORBID_OMARCHY   1 on the arch guest
#
# Stdout KEY=value: FROM_VERSION=, INSTALL_SH_SHA256=
set -euo pipefail

phase="${SMOKE_UPDATE_PHASE:-all}"
from_version="${UPDATE_FROM_VERSION:-}"
url="${INSTALL_SH_URL:-https://raw.githubusercontent.com/lgse/strata/main/install.sh}"
dest="${INSTALL_SH_DEST:-/tmp/strata-install.sh}"
bin="${HOME}/.local/bin/strata"
app_id="io.github.lgse.Strata"
repo="lgse/strata"

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

detect_target() {
  case $(uname -m) in
    x86_64 | amd64) printf '%s\n' x86_64-unknown-linux-gnu ;;
    aarch64 | arm64) printf '%s\n' aarch64-unknown-linux-gnu ;;
    *)
      echo "Strata has no prebuilt release for $(uname -m)." >&2
      exit 1
      ;;
  esac
}

normalize_from_version() {
  local text=$1
  if [[ $text == v* || $text == V* ]]; then
    text=${text:1}
  fi
  printf '%s\n' "$text"
}

install_previous() (
  set -euo pipefail
  if [[ -z $from_version ]]; then
    echo "UPDATE_FROM_VERSION is required for the previous phase" >&2
    exit 1
  fi
  from_version=$(normalize_from_version "$from_version")
  local target archive_name work base extracted desktop_dir
  target=$(detect_target)
  archive_name="strata-${from_version}-${target}.tar.gz"
  work=$(mktemp -d)
  trap 'rm -rf -- "$work"' EXIT

  if [[ -n ${UPDATE_FROM_ARCHIVE:-} ]]; then
    if [[ ! -f $UPDATE_FROM_ARCHIVE ]]; then
      echo "previous archive missing: ${UPDATE_FROM_ARCHIVE}" >&2
      exit 1
    fi
    cp "$UPDATE_FROM_ARCHIVE" "$work/$archive_name"
  else
    base="https://github.com/${repo}/releases/download/v${from_version}"
    curl -fsSL -o "$work/$archive_name" "$base/$archive_name"
    curl -fsSL -o "$work/$archive_name.sha256" "$base/$archive_name.sha256"
    (cd "$work" && sha256sum --check "$archive_name.sha256")
  fi

  tar -xzf "$work/$archive_name" -C "$work"
  extracted="$work/${archive_name%.tar.gz}"
  if [[ ! -x $extracted/strata ]]; then
    extracted=$work
  fi
  if [[ ! -x $extracted/strata ]]; then
    echo "previous archive does not contain the Strata binary" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$bin")"
  install -Dm755 "$extracted/strata" "$bin"

  if [[ -r $extracted/$app_id.desktop ]]; then
    desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$desktop_dir"
    sed "s|^Exec=strata |Exec=$bin |" "$extracted/$app_id.desktop" \
      >"$desktop_dir/$app_id.desktop"
  fi

  printf 'FROM_VERSION=%s\n' "$from_version"
  test -x "$bin"
)

update_latest() {
  # install.sh --non-interactive dies if BIN_PATH already exists. Moving the
  # previous binary aside is the documented recovery path ("remove it or run
  # the interactive installer to replace it") and leaves desktop metadata in
  # place so this is still an update of an existing install.
  if [[ -e $bin ]]; then
    mv "$bin" "${bin}.previous"
  fi
  curl -fsSL "$url" -o "$dest"
  local digest
  digest="$(sha256sum "$dest" | awk '{print $1}')"
  printf 'INSTALL_SH_SHA256=%s\n' "$digest"
  bash "$dest" --non-interactive --with-desktop-entry --without-file-chooser
  test -x "$bin"
}

case "$phase" in
  previous) install_previous ;;
  latest) update_latest ;;
  all)
    install_previous
    update_latest
    ;;
  *)
    echo "unknown SMOKE_UPDATE_PHASE=${phase}" >&2
    exit 1
    ;;
esac
