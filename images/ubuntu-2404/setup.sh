#!/usr/bin/env bash
# Guest setup for ubuntu-2404. Run as tester with NOPASSWD sudo.
# Strata installer is out of scope for this recipe.
set -eu
export DEBIAN_FRONTEND=noninteractive

SNAPSHOT_URL="${SNAPSHOT_URL:-http://snapshot.ubuntu.com/ubuntu/20260901T000000Z}"

sudo -n mkdir -p /etc/apt/sources.list.d
if [[ -f /etc/apt/sources.list.d/ubuntu.sources ]]; then
  sudo -n mv /etc/apt/sources.list.d/ubuntu.sources \
    /etc/apt/sources.list.d/ubuntu.sources.disabled
fi
if [[ -f /etc/apt/sources.list ]]; then
  sudo -n mv /etc/apt/sources.list /etc/apt/sources.list.disabled
fi
sudo -n tee /etc/apt/sources.list.d/snapshot.list >/dev/null <<EOF
deb [check-valid-until=no] ${SNAPSHOT_URL} noble main universe
deb [check-valid-until=no] ${SNAPSHOT_URL} noble-updates main universe
deb [check-valid-until=no] ${SNAPSHOT_URL} noble-security main universe
EOF

sudo -n apt-get update
sudo -n apt-get install -y \
  ubuntu-desktop-minimal \
  gnome-screenshot \
  qemu-guest-agent \
  bubblewrap \
  ffmpeg \
  ffmpegthumbnailer \
  fontconfig \
  gstreamer1.0-libav \
  gstreamer1.0-plugins-good \
  libgtk-4-1 \
  libgtksourceview-5-0 \
  gvfs-daemons \
  libpoppler-glib8 \
  xdg-utils \
  desktop-file-utils \
  at-spi2-core \
  xdg-desktop-portal \
  xdg-desktop-portal-gnome

# Noble GDM is gdm3; /etc/gdm/custom.conf would not autologin on this guest.
sudo -n mkdir -p /etc/gdm3
sudo -n tee /etc/gdm3/custom.conf >/dev/null <<'EOF'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=tester
WaylandEnable=true
EOF

sudo -n systemctl set-default graphical.target
sudo -n systemctl mask \
  gnome-initial-setup.service \
  gnome-initial-setup-first-login.service \
  gnome-tour.service \
  || true
sudo -n rm -f /etc/xdg/autostart/gnome-software-service.desktop || true
sudo -n systemctl disable --now unattended-upgrades || true

sudo -n -u tester mkdir -p /home/tester/.config
sudo -n -u tester touch /home/tester/.config/gnome-initial-setup-done

sudo -n loginctl enable-linger tester
sudo -n systemctl enable qemu-guest-agent || true
sudo -n systemctl start qemu-guest-agent || true

# Best-effort: toolkit accessibility for later AT-SPI oracles (PR 5).
sudo -n -u tester dbus-run-session \
  gsettings set org.gnome.desktop.interface toolkit-accessibility true \
  || true

sudo -n dpkg-query -W -f='${Package}\t${Version}\n' \
  >/var/tmp/strata-inventory.txt
sudo -n getconf GNU_LIBC_VERSION >/var/tmp/strata-glibc.txt
sudo -n dpkg-query -W -f='${Version}\n' libgtk-4-1 \
  >/var/tmp/strata-gtk.txt || sudo -n tee /var/tmp/strata-gtk.txt >/dev/null <<<""
sudo -n chmod 644 /var/tmp/strata-inventory.txt /var/tmp/strata-glibc.txt /var/tmp/strata-gtk.txt
