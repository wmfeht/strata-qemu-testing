#!/usr/bin/env bash
# Guest setup for fedora-workstation. Run as tester with NOPASSWD sudo.
# Strata installer is out of scope for this recipe.
set -eu

keep_virtio_dhcp() {
  # Workstation GNOME can cut over NetworkManager / rename the NIC.
  # Keep DHCP so SSH does not drop mid-script.
  command -v nmcli >/dev/null 2>&1 || return 0
  local dev dtype conn
  while IFS=: read -r dev dtype _; do
    [ -n "$dev" ] || continue
    [ "$dtype" = "ethernet" ] || continue
    sudo -n nmcli device set "$dev" managed yes || true
    sudo -n nmcli device set "$dev" autoconnect yes || true
    conn="$(
      sudo -n nmcli -t -f UUID,DEVICE connection show --active \
        | awk -F: -v d="$dev" '$2==d {print $1; exit}'
    )"
    if [ -n "$conn" ]; then
      sudo -n nmcli connection modify "$conn" \
        ipv4.method auto connection.autoconnect yes \
        || true
    else
      sudo -n nmcli connection add type ethernet ifname "$dev" \
        con-name "strata-${dev}" ipv4.method auto \
        connection.autoconnect yes \
        || true
    fi
  done < <(sudo -n nmcli -t -f DEVICE,TYPE device status)
}

keep_virtio_dhcp

sudo -n dnf -y upgrade
sudo -n dnf -y install @workstation-product-environment \
  fedora-release-workstation \
  gnome-screenshot \
  qemu-guest-agent \
  bubblewrap \
  ffmpeg-free \
  ffmpegthumbnailer \
  fontconfig \
  gstreamer1-plugin-libav \
  gstreamer1-plugins-good \
  gtk4 \
  gtksourceview5 \
  gvfs \
  poppler-glib \
  xdg-utils \
  desktop-file-utils \
  at-spi2-core \
  xdg-desktop-portal \
  xdg-desktop-portal-gnome \
  NetworkManager \
  firewalld

keep_virtio_dhcp

# Fedora GDM (not Ubuntu gdm3).
sudo -n mkdir -p /etc/gdm
sudo -n tee /etc/gdm/custom.conf >/dev/null <<'EOF'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=tester
WaylandEnable=true
EOF

sudo -n systemctl set-default graphical.target
sudo -n systemctl enable gdm.service || true
sudo -n systemctl mask \
  gnome-initial-setup.service \
  gnome-initial-setup-first-login.service \
  gnome-tour.service \
  || true
sudo -n rm -f /etc/xdg/autostart/gnome-software-service.desktop \
  /etc/xdg/autostart/gnome-software*.desktop \
  /etc/xdg/autostart/org.gnome.Tour.desktop \
  /usr/share/applications/org.gnome.Tour.desktop \
  || true
sudo -n dnf -y remove gnome-tour || true

sudo -n -u tester mkdir -p /home/tester/.config
sudo -n -u tester touch /home/tester/.config/gnome-initial-setup-done
sudo -n restorecon -R /home/tester || true

sudo -n loginctl enable-linger tester
sudo -n systemctl enable qemu-guest-agent || true
sudo -n systemctl start qemu-guest-agent || true

sudo -n systemctl enable --now firewalld || true
sudo -n firewall-cmd --permanent --add-service=ssh
sudo -n firewall-cmd --reload || sudo -n firewall-cmd --add-service=ssh

keep_virtio_dhcp

# Best-effort: toolkit accessibility for later AT-SPI oracles.
# Skip the GNOME 40+ welcome dialog (built into gnome-shell; tour.service
# mask is not enough on F44).
sudo -n -u tester dbus-run-session \
  gsettings set org.gnome.desktop.interface toolkit-accessibility true \
  || true
sudo -n -u tester dbus-run-session \
  gsettings set org.gnome.shell welcome-dialog-last-shown-version '99.0' \
  || true

sudo -n rpm -qa >/var/tmp/strata-inventory.txt
sudo -n getconf GNU_LIBC_VERSION >/var/tmp/strata-glibc.txt
sudo -n rpm -q --qf '%{VERSION}\n' gtk4 >/var/tmp/strata-gtk.txt 2>/dev/null \
  || sudo -n tee /var/tmp/strata-gtk.txt >/dev/null <<<""
sudo -n chmod 644 /var/tmp/strata-inventory.txt /var/tmp/strata-glibc.txt /var/tmp/strata-gtk.txt
