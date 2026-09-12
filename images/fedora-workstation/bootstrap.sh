#!/usr/bin/env bash
# Guest recipe downloader (not `mise bootstrap`). Scrapes the F44 Cloud
# images directory for Fedora-Cloud-Base-Generic-*.qcow2, checks the
# CHECKSUM sidecar against the pinned SHA, and fail-closes on mismatch.
set -euo pipefail

CACHE="${STRATA_QEMU_CACHE:?STRATA_QEMU_CACHE is required}"
URL="${SOURCE_URL:?SOURCE_URL is required}"
SHA="${SOURCE_SHA256:?SOURCE_SHA256 is required}"
NAME="${SOURCE_FILENAME:-Fedora-Cloud-Base-Generic.qcow2}"

if [[ ! "$SHA" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "bootstrap.sh: SOURCE_SHA256 must be 64 hex chars" >&2
  exit 1
fi
SHA="$(printf '%s' "$SHA" | tr 'A-F' 'a-f')"

# Directory scrape. A file URL still works if the operator passed a blob.
URL="${URL%/}"
dir_url="$URL"

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

listing="$(curl -L --fail --retry 3 -sS "$dir_url/")"
image="$(
  printf '%s' "$listing" \
    | grep -oE 'Fedora-Cloud-Base-Generic[^"/[:space:]<>]+\.qcow2' \
    || true
)"
image="$(printf '%s' "$image" | sort -u | tail -n1)"
if [[ -z "$image" ]]; then
  echo "bootstrap.sh: no Fedora-Cloud-Base-Generic-*.qcow2 in $dir_url/" >&2
  exit 1
fi

checksum_name="$(
  printf '%s' "$listing" \
    | grep -oE 'Fedora-Cloud-[^"/[:space:]<>]*CHECKSUM' \
    || true
)"
checksum_name="$(printf '%s' "$checksum_name" | sort -u | tail -n1)"
if [[ -z "$checksum_name" ]]; then
  echo "bootstrap.sh: no CHECKSUM sidecar in $dir_url/" >&2
  exit 1
fi

checksum_path="$dest_dir/$checksum_name"
curl -L --fail --retry 3 -o "$checksum_path" "$dir_url/$checksum_name"

# Optional OpenPGP of CHECKSUM when Fedora keys are already on the host.
# Unsigned sidecar (host tests) and missing keyrings skip this extra check.
if grep -q "BEGIN PGP SIGNED MESSAGE" "$checksum_path" \
  && command -v gpgv >/dev/null 2>&1; then
  for keyring in \
    /usr/share/distribution-gpg-keys/fedora/RPM-GPG-KEY-fedora-44-primary \
    /etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary
  do
    if [[ -f "$keyring" ]]; then
      if ! gpgv --keyring "$keyring" "$checksum_path" >/dev/null 2>&1; then
        echo "bootstrap.sh: OpenPGP verification of $checksum_name failed" >&2
        exit 1
      fi
      break
    fi
  done
fi

sidecar_sha="$(
  awk -v f="$image" '
    $1 == "SHA256" && $2 == "(" f ")" { print tolower($4); exit }
  ' "$checksum_path"
)"
if [[ -z "$sidecar_sha" ]]; then
  echo "bootstrap.sh: CHECKSUM has no SHA256 for $image" >&2
  exit 1
fi
if [[ "$sidecar_sha" != "$SHA" ]]; then
  echo "bootstrap.sh: CHECKSUM sha for $image ($sidecar_sha) does not match pin $SHA" >&2
  exit 1
fi

rm -f "$partial"
curl -L --fail --retry 3 -o "$partial" "$dir_url/$image"
actual="$(hash_of "$partial")"
if [[ "$actual" != "$SHA" ]]; then
  rm -f "$partial"
  echo "bootstrap.sh: checksum mismatch for $image (expected $SHA, got $actual)" >&2
  exit 1
fi
mv "$partial" "$dest"
printf '%s\n' "$dest"
