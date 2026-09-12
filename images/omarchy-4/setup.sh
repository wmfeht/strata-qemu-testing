#!/usr/bin/env bash
# Guest setup for omarchy-4. Unencrypted ISO installs do not autologin;
# this writes SDDM autologin, NOPASSWD, linger, and grim. Strata is out
# of scope. Do not refresh the distro after install.
#
# Image-build runs this via `sudo -S` (password sudo until we write
# NOPASSWD). Re-runs as tester with NOPASSWD exec sudo -n here.
set -eu

if [[ "$(id -u)" -ne 0 ]]; then
  exec sudo -n bash "$0" "$@"
fi

if ! command -v sddm >/dev/null 2>&1 \
  && ! systemctl list-unit-files sddm.service >/dev/null 2>&1; then
  echo "setup.sh: sddm not found (omarchy-4 requires SDDM)" >&2
  exit 1
fi
if ! command -v Hyprland >/dev/null 2>&1; then
  echo "setup.sh: Hyprland not found" >&2
  exit 1
fi
if systemctl is-enabled greetd.service >/dev/null 2>&1; then
  echo "setup.sh: greetd is enabled; omarchy-4 uses sddm, never greetd" >&2
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

mkdir -p /etc/sudoers.d
tee /etc/sudoers.d/tester >/dev/null <<'EOF'
tester ALL=(ALL) NOPASSWD: ALL
Defaults:tester !authenticate
EOF
chmod 440 /etc/sudoers.d/tester

if ! command -v grim >/dev/null 2>&1; then
  pacman -S --noconfirm --needed grim
fi

loginctl enable-linger tester
systemctl set-default graphical.target
systemctl enable sddm.service
systemctl enable qemu-guest-agent || true
systemctl start qemu-guest-agent || true
systemctl enable --now sshd || systemctl enable --now ssh || true
if command -v ufw >/dev/null 2>&1; then
  ufw allow ssh || true
  ufw --force enable || true
fi
systemctl restart sddm.service || systemctl start sddm.service || true

omarchy version >/var/tmp/strata-omarchy-version.txt 2>&1 || true
pacman -Q >/var/tmp/strata-inventory.txt
getconf GNU_LIBC_VERSION >/var/tmp/strata-glibc.txt
pacman -Q gtk4 >/var/tmp/strata-gtk.txt 2>/dev/null \
  || tee /var/tmp/strata-gtk.txt >/dev/null <<<""
chmod 644 /var/tmp/strata-inventory.txt /var/tmp/strata-glibc.txt \
  /var/tmp/strata-gtk.txt /var/tmp/strata-omarchy-version.txt 2>/dev/null || true
