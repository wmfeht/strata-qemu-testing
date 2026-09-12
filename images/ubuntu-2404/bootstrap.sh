#!/usr/bin/env bash
# Guest recipe downloader (not `mise bootstrap`). Fetches the pinned cloudimg
# into $STRATA_QEMU_CACHE/downloads/ and fail-closes on SHA-256 mismatch.
set -euo pipefail

CACHE="${STRATA_QEMU_CACHE:?STRATA_QEMU_CACHE is required}"
URL="${SOURCE_URL:?SOURCE_URL is required}"
SHA="${SOURCE_SHA256:?SOURCE_SHA256 is required}"
NAME="${SOURCE_FILENAME:-noble-server-cloudimg-amd64.img}"

if [[ ! "$SHA" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "bootstrap.sh: SOURCE_SHA256 must be 64 hex chars" >&2
  exit 1
fi
SHA="$(printf '%s' "$SHA" | tr 'A-F' 'a-f')"

dest_dir="$CACHE/downloads"
mkdir -p "$dest_dir"
dest="$dest_dir/$NAME"
partial="$dest.partial"

hash_of() {
  sha256sum "$1" | awk '{print $1}'
}

if [[ -f "$dest" ]]; then
  actual="$(hash_of "$dest")"
  if [[ "$actual" == "$SHA" ]]; then
    printf '%s\n' "$dest"
    exit 0
  fi
  echo "bootstrap.sh: removing stale $dest (sha256 $actual, expected $SHA)" >&2
  rm -f "$dest"
fi

rm -f "$partial"
curl -L --fail --retry 3 -o "$partial" "$URL"
actual="$(hash_of "$partial")"
if [[ "$actual" != "$SHA" ]]; then
  rm -f "$partial"
  echo "bootstrap.sh: checksum mismatch for $NAME (expected $SHA, got $actual)" >&2
  exit 1
fi
mv "$partial" "$dest"
printf '%s\n' "$dest"
