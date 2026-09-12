#!/usr/bin/env bash
# Guest setup for arch. Run as tester with NOPASSWD sudo (image-build
# may wrap this in sudo -n bash). Strata installer is out of scope.
set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SNAPSHOT='https://archive.archlinux.org/repos/2026/09/01/$repo/os/$arch'
SNAPSHOT_URL="${SNAPSHOT_URL:-$DEFAULT_SNAPSHOT}"

if [[ ! -f "$HERE/greetd-config.toml" ]]; then
  echo "setup.sh: missing $HERE/greetd-config.toml (upload the recipe file)" >&2
  exit 1
fi
if [[ ! -f "$HERE/hyprland.lua" ]]; then
  echo "setup.sh: missing $HERE/hyprland.lua (upload the recipe file)" >&2
  exit 1
fi

probe="${SNAPSHOT_URL//\$repo/core}"
probe="${probe//\$arch/x86_64}"
http_code="$(
  curl -sS -o /dev/null -w '%{http_code}' --retry 2 --head "${probe}/core.db" || true
)"
if [[ "$http_code" != "200" ]]; then
  echo "Arch package archive is not usable (HTTP ${http_code:-curl-fail}) at ${probe}." >&2
  echo "Retarget images/arch/image.toml source_url, source_sha256, and packages.snapshot_url to a complete https://archive.archlinux.org/repos/YYYY/MM/DD day that matches the cloudimg pin." >&2
  echo "Refusing to use a rolling mirror." >&2
  exit 1
fi

sudo -n tee /etc/pacman.d/mirrorlist >/dev/null <<EOF
Server = ${SNAPSHOT_URL}
EOF

sudo -n pacman -Syu --noconfirm
sudo -n pacman -S --noconfirm --needed \
  hyprland \
  greetd \
  xdg-desktop-portal-hyprland \
  xdg-desktop-portal \
  jq \
  grim \
  ttf-liberation \
  qemu-guest-agent \
  polkit

if command -v omarchy >/dev/null 2>&1 || [[ -e /usr/share/omarchy ]]; then
  echo "omarchy must not be installed on the arch golden" >&2
  exit 1
fi
if command -v strata >/dev/null 2>&1 || [[ -e /home/tester/.local/bin/strata ]]; then
  echo "strata must not be installed on the arch golden" >&2
  exit 1
fi

sudo -n mkdir -p /etc/greetd /etc/sudoers.d
sudo -n cp "$HERE/greetd-config.toml" /etc/greetd/config.toml
sudo -n chmod 644 /etc/greetd/config.toml

sudo -n usermod -aG video,input,seat tester || sudo -n usermod -aG video,input tester

sudo -n install -d -o tester -g tester /home/tester/.config/hypr
sudo -n install -m 644 -o tester -g tester "$HERE/hyprland.lua" \
  /home/tester/.config/hypr/hyprland.lua

sudo -n tee /etc/sudoers.d/tester >/dev/null <<'EOF'
tester ALL=(ALL) NOPASSWD: ALL
EOF
sudo -n chmod 440 /etc/sudoers.d/tester

sudo -n mkdir -p /etc/systemd/system/greetd.service.d
# greetd owns VT1; a getty there races the compositor.
sudo -n systemctl disable --now getty@tty1.service || true
sudo -n systemctl mask getty@tty1.service || true
sudo -n systemctl set-default graphical.target
sudo -n systemctl enable greetd.service
sudo -n systemctl enable qemu-guest-agent || true
sudo -n systemctl start qemu-guest-agent || true
sudo -n loginctl enable-linger tester
sudo -n systemctl restart greetd.service || sudo -n systemctl start greetd.service

uid="$(id -u tester)"
runtime="/run/user/${uid}"
deadline=$((SECONDS + 90))
wayland_ok=0
while (( SECONDS < deadline )); do
  while read -r sid; do
    [ -n "$sid" ] || continue
    t="$(loginctl show-session "$sid" -p Type --value 2>/dev/null || true)"
    c="$(loginctl show-session "$sid" -p Class --value 2>/dev/null || true)"
    s="$(loginctl show-session "$sid" -p State --value 2>/dev/null || true)"
    seat="$(loginctl show-session "$sid" -p Seat --value 2>/dev/null || true)"
    if [ "$t" = "wayland" ] && [ "$c" = "user" ] && [ "$s" = "active" ] && [ "$seat" = "seat0" ]; then
      wayland_ok=1
      break
    fi
  done < <(loginctl --no-legend list-sessions 2>/dev/null | awk -v u="$uid" '$2==u {print $1}')
  if [ "$wayland_ok" = 1 ]; then
    break
  fi
  sleep 2
done
if [ "$wayland_ok" != 1 ]; then
  echo "setup.sh: greetd never reached Type=wayland; check hyprland.lua" >&2
  loginctl list-sessions || true
  systemctl status greetd --no-pager -l || true
  journalctl -u greetd -b --no-pager | tail -n 80 || true
  ls -la /home/tester/.config/hypr/ || true
  exit 1
fi

hypr_sig="$(
  find "${runtime}/hypr" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' 2>/dev/null \
    | sort -n | tail -n1 | cut -d' ' -f2-
)"
export XDG_RUNTIME_DIR="$runtime"
if [ -n "$hypr_sig" ]; then
  export HYPRLAND_INSTANCE_SIGNATURE="$hypr_sig"
fi
errors="$(
  sudo -n -u tester env \
    XDG_RUNTIME_DIR="$runtime" \
    HYPRLAND_INSTANCE_SIGNATURE="${hypr_sig:-}" \
    hyprctl configerrors 2>/dev/null || true
)"
errors="$(printf '%s' "$errors" | tr -d '[:space:]')"
if [ -n "$errors" ] && [ "$errors" != "ok" ]; then
  echo "setup.sh: hyprctl configerrors is non-empty: $errors" >&2
  exit 1
fi

sudo -n pacman -Q >/var/tmp/strata-inventory.txt
sudo -n getconf GNU_LIBC_VERSION >/var/tmp/strata-glibc.txt
sudo -n pacman -Q gtk4 >/var/tmp/strata-gtk.txt 2>/dev/null \
  || sudo -n tee /var/tmp/strata-gtk.txt >/dev/null <<<""
sudo -n chmod 644 /var/tmp/strata-inventory.txt /var/tmp/strata-glibc.txt /var/tmp/strata-gtk.txt
