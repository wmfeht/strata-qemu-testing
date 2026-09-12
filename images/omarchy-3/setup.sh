#!/usr/bin/env bash
# Guest setup for omarchy-3. Probe SDDM vs omarchy-seamless-login.service;
# do not assume greetd. If SDDM: same autologin drop-in as omarchy-4. If
# seamless-login is enabled and owns VT1, leave it and do not also enable
# SDDM. Never enable greetd. Never enable both. Unencrypted ISO installs
# do not autologin. Strata is out of scope. Do not run omarchy update or
# pacman -Syu after install.
#
# Image-build runs this via `sudo -S` (password sudo until we write
# NOPASSWD). Re-runs as tester with NOPASSWD exec sudo -n here.
set -eu

if [[ "$(id -u)" -ne 0 ]]; then
  exec sudo -n bash "$0" "$@"
fi

if systemctl is-enabled greetd.service >/dev/null 2>&1; then
  echo "setup.sh: greetd is enabled; omarchy-3 never uses greetd" >&2
  exit 1
fi
if ! command -v Hyprland >/dev/null 2>&1; then
  echo "setup.sh: Hyprland not found" >&2
  exit 1
fi

seamless_unit="omarchy-seamless-login.service"
seamless_enabled=false
if systemctl is-enabled "$seamless_unit" >/dev/null 2>&1; then
  seamless_enabled=true
fi

seamless_owns_vt1=false
if $seamless_enabled; then
  tty_path="$(systemctl show "$seamless_unit" -p TTYPath --value 2>/dev/null || true)"
  conflicts="$(systemctl show "$seamless_unit" -p Conflicts --value 2>/dev/null || true)"
  if [[ "$tty_path" == *tty1* ]] \
    || [[ "$conflicts" == *getty@tty1* ]]; then
    seamless_owns_vt1=true
  fi
fi

has_sddm=false
if command -v sddm >/dev/null 2>&1 \
  || systemctl list-unit-files sddm.service >/dev/null 2>&1; then
  has_sddm=true
fi

sddm_enabled=false
if systemctl is-enabled sddm.service >/dev/null 2>&1; then
  sddm_enabled=true
fi

use_seamless=false
if $seamless_enabled && $seamless_owns_vt1; then
  use_seamless=true
fi

if $use_seamless && $sddm_enabled; then
  echo "setup.sh: both SDDM and omarchy-seamless-login.service enabled" >&2
  exit 1
fi

if $use_seamless; then
  : # leave omarchy-seamless-login.service; do not enable SDDM
elif $has_sddm; then
  if $seamless_enabled; then
    echo "setup.sh: both SDDM and omarchy-seamless-login.service enabled" >&2
    exit 1
  fi
  session=""
  for desktop in omarchy.desktop hyprland-uwsm.desktop; do
    if [[ -f "/usr/share/wayland-sessions/${desktop}" ]]; then
      session="${desktop%.desktop}"
      break
    fi
  done
  if [[ -z "$session" ]]; then
    echo "setup.sh: no wayland session in /usr/share/wayland-sessions/" >&2
    ls -la /usr/share/wayland-sessions/ || true
    exit 1
  fi

  mkdir -p /etc/sddm.conf.d
  tee /etc/sddm.conf.d/99-autologin.conf >/dev/null <<EOF
[Autologin]
User=tester
Session=${session}
Relogin=true
EOF
else
  echo "setup.sh: neither SDDM nor omarchy-seamless-login.service (VT1)" >&2
  exit 1
fi

mkdir -p /etc/sudoers.d
tee /etc/sudoers.d/tester >/dev/null <<'EOF'
tester ALL=(ALL) NOPASSWD: ALL
Defaults:tester !authenticate
EOF
chmod 440 /etc/sudoers.d/tester

# The ISO install uses an offline repo and never fetches core/extra/multilib
# sync DBs. install.sh then dies with "target not found" for gst-libav,
# gst-plugins-good, and gtksourceview5. Sync DBs and install those three.
# extra's gst-libav pulls a newer gstreamer than the ISO-pinned
# gst-plugins-base (exact-version deps). Upgrade installed gstreamer/gst-*
# in the same transaction. Do not -Syu or omarchy update.
if [[ ! -f /var/lib/pacman/sync/core.db ]]; then
  pacman -Sy --noconfirm
fi
mapfile -t gst_installed < <(pacman -Qq | grep -E '^(gstreamer|gst-)' || true)
pacman -S --noconfirm --needed \
  gst-libav gst-plugins-good gtksourceview5 "${gst_installed[@]}"

if ! command -v grim >/dev/null 2>&1; then
  pacman -S --noconfirm --needed grim
fi

loginctl enable-linger tester
systemctl set-default graphical.target
if $use_seamless; then
  :
else
  systemctl enable sddm.service
fi
systemctl enable qemu-guest-agent || true
systemctl start qemu-guest-agent || true
systemctl enable --now sshd || systemctl enable --now ssh || true
if command -v ufw >/dev/null 2>&1; then
  ufw allow ssh || true
  ufw --force enable || true
fi
if $use_seamless; then
  :
else
  systemctl restart sddm.service || systemctl start sddm.service || true
fi

if command -v omarchy >/dev/null 2>&1; then
  omarchy version >/var/tmp/strata-omarchy-version.txt 2>&1 || true
elif [[ -x /home/tester/.local/share/omarchy/bin/omarchy ]]; then
  OMARCHY_PATH=/home/tester/.local/share/omarchy \
    PATH="/home/tester/.local/share/omarchy/bin:$PATH" \
    omarchy version >/var/tmp/strata-omarchy-version.txt 2>&1 || true
else
  tee /var/tmp/strata-omarchy-version.txt >/dev/null <<<"omarchy not found"
fi
pacman -Q >/var/tmp/strata-inventory.txt
getconf GNU_LIBC_VERSION >/var/tmp/strata-glibc.txt
pacman -Q gtk4 >/var/tmp/strata-gtk.txt 2>/dev/null \
  || tee /var/tmp/strata-gtk.txt >/dev/null <<<""
chmod 644 /var/tmp/strata-inventory.txt /var/tmp/strata-glibc.txt \
  /var/tmp/strata-gtk.txt /var/tmp/strata-omarchy-version.txt 2>/dev/null || true
