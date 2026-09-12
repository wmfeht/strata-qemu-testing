#!/usr/bin/env bash
# 3.8.4 has no omarchy-cidata-load. tty1 always runs the gum configurator.
# Copy the attached CIDATA dump into /root, stub the configurator, patch
# authorized_keys + sshd into .automated_script.sh, skip the post-install
# "Reboot Now" gum confirm, then kill tty1 so .zlogin respawns the silent
# archinstall path.
set -euo pipefail

if [[ -x /usr/local/bin/omarchy-cidata-load ]]; then
  echo "skip-wizard: omarchy-cidata-load present; leaving autoinstall alone"
  exit 0
fi
if pgrep -f '/usr/bin/archinstall' >/dev/null 2>&1; then
  echo "skip-wizard: archinstall already running"
  exit 0
fi
if [[ -f /run/omarchy-skip-wizard-done ]]; then
  echo "skip-wizard: already completed"
  exit 0
fi

mkdir -p /run/cidata
if ! mountpoint -q /run/cidata; then
  if [[ -e /dev/disk/by-label/CIDATA ]]; then
    mount -o ro /dev/disk/by-label/CIDATA /run/cidata
  elif [[ -e /dev/disk/by-label/cidata ]]; then
    mount -o ro /dev/disk/by-label/cidata /run/cidata
  else
    echo "skip-wizard: no cidata label" >&2
    exit 1
  fi
fi

for name in user_configuration.json user_credentials.json \
  user_encrypt_installation.txt authorized_keys; do
  if [[ ! -f /run/cidata/$name ]]; then
    echo "skip-wizard: missing $name on cidata" >&2
    exit 1
  fi
  cp -f "/run/cidata/$name" "/root/$name"
done
umount /run/cidata || true
: > /root/user_full_name.txt
: > /root/user_email_address.txt

printf '%s\n' '#!/bin/bash' 'exit 0' > /root/configurator
chmod +x /root/configurator

if ! command -v python3 >/dev/null 2>&1; then
  echo "skip-wizard: python3 is required to patch .automated_script.sh" >&2
  exit 1
fi

python3 - <<'ENDPATCH'
from pathlib import Path

p = Path("/root/.automated_script.sh")
text = p.read_text()
needle = "  configure_login_for_unencrypted_install\n"
insert = (
    '  mkdir -p "/mnt/home/$OMARCHY_USER/.ssh"\n'
    "  if [[ -f /root/authorized_keys ]]; then\n"
    '    cp /root/authorized_keys "/mnt/home/$OMARCHY_USER/.ssh/authorized_keys"\n'
    '    chmod 700 "/mnt/home/$OMARCHY_USER/.ssh"\n'
    '    chmod 600 "/mnt/home/$OMARCHY_USER/.ssh/authorized_keys"\n'
    '    chown -R 1000:1000 "/mnt/home/$OMARCHY_USER/.ssh"\n'
    "  fi\n"
    "  arch-chroot /mnt systemctl enable sshd.service 2>/dev/null \\\n"
    "    || arch-chroot /mnt systemctl enable ssh.service 2>/dev/null \\\n"
    "    || true\n"
    "  if arch-chroot /mnt command -v ufw >/dev/null 2>&1; then\n"
    "    arch-chroot /mnt ufw allow ssh || true\n"
    "  fi\n"
    "\n"
)
if needle not in text:
    raise SystemExit("skip-wizard: could not patch .automated_script.sh")
if "authorized_keys" not in text:
    p.write_text(text.replace(needle, insert + needle, 1))
    print("skip-wizard: patched authorized_keys + sshd")
else:
    print("skip-wizard: authorized_keys hook already present")

finished = Path("/root/omarchy/install/post-install/finished.sh")
if not finished.is_file():
    raise SystemExit("skip-wizard: missing finished.sh")
ft = finished.read_text()
gum_if = (
    'if gum confirm --padding "0 0 0 $((PADDING_LEFT + 32))" '
    '--show-help=false --default --affirmative "Reboot Now" '
    '--negative "" ""; then'
)
chroot_if = (
    "if [[ -n ${OMARCHY_CHROOT_INSTALL:-} ]] || "
    + gum_if[3:]
)
if gum_if not in ft:
    raise SystemExit("skip-wizard: finished.sh has no Reboot Now gum confirm")
if chroot_if in ft:
    print("skip-wizard: finished.sh already skips reboot prompt in chroot")
else:
    finished.write_text(ft.replace(gum_if, chroot_if, 1))
    print("skip-wizard: patched finished.sh to skip reboot prompt in chroot")
ENDPATCH

mkdir -p /run
: > /run/omarchy-skip-wizard-done
# Getty/.zlogin restarts .automated_script.sh against the stub configurator.
# kill returns 1 if any pid already exited (gum dies with configurator).
tty1_pids=$(ps -t tty1 -o pid= 2>/dev/null || true)
for pid in $tty1_pids; do
  [[ "$pid" == "1" ]] && continue
  kill -9 "$pid" 2>/dev/null || true
done
echo "skip-wizard: tty1 killed; automated_script will respawn"
