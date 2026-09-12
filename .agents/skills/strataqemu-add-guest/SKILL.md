---
name: strataqemu-add-guest
description: >-
  Add or modify a guest recipe under images/<id>/ in strata-qemu-testing:
  image.toml, bootstrap.sh, setup.sh, cloud-init template or Omarchy cidata,
  plus the host-side lookups and unit tests a new guest id needs. Use when
  adding a distribution, re-pinning a source image or ISO, changing a guest's
  desktop stack, or when image-build fails on a recipe.
---

# Add or change a guest recipe

A guest is `images/<id>/`. `image-build` turns it into a golden qcow2;
`run-test` boots overlays of that golden. Read
`docs/architecture.md` sections "Guest recipes" and "Image build" first
if the pipeline is unfamiliar.

## Checklist

```
- [ ] images/<id>/image.toml with a dated source_url and source_sha256
- [ ] images/<id>/bootstrap.sh (copy from a sibling; change only the default NAME)
- [ ] images/<id>/setup.sh (autologin, NOPASSWD, linger, qemu-guest-agent, screenshot tool, inventory files)
- [ ] images/<id>/user-data.yaml.tmpl  OR  images/<id>/cidata/{user_configuration.json,user_credentials.json.tmpl,user_encrypt_installation.txt,authorized_keys.tmpl}
- [ ] Host lookups updated: qemu.py guest sets, tests_spec.py compositor/install sets, image_build.py Omarchy major (if ISO)
- [ ] tests/test_guest_toml.py: a <Id>RecipeTests class
- [ ] mise run test passes
- [ ] Live: mise run image-build -- <id>, then run-test --session-only
```

## image.toml

Start from the closest sibling (`ubuntu-2404` for GNOME cloud images,
`arch` for Hyprland cloud images, `omarchy-4` for ISO autoinstall). Rules
enforced by `guest.Guest.load`:

- `id` equals the directory name; no `.` or `/`.
- `source_url` must not contain `/current/` or `/latest/`. Pin a dated
  path and the matching `source_sha256`. If the URL is a directory
  (Fedora), set `source_filename`.
- `firmware` is `uefi` or `bios`. Cloud images with a BIOS boot partition
  and no ESP (arch-boxes) need `bios`; everything else is `uefi`.
- Cloud images need `user-data.yaml.tmpl`; ISO autoinstall needs a
  `[cidata]` table with `disk = "/dev/vda"`, `encrypt = false`, and the
  four `cidata/` files.
- `[session]`, `[packages]`, `[user]` tables are required. `[user].name`
  must be `tester` for the SSH helpers' defaults to line up.
- `build_timeout_s` covers `setup.sh` (and the whole autoinstall for ISO
  guests). `boot_timeout_s` covers first SSH. Existing guests use 3600 and
  180.

When a package snapshot exists (Ubuntu `snapshot.ubuntu.com`, Arch
`archive.archlinux.org`), set `packages.snapshot_url` to the same day as
the cloud image. `image-build` exports it as `SNAPSHOT_URL` to `setup.sh`.

## bootstrap.sh

Runs on the host with `STRATA_QEMU_CACHE`, `SOURCE_URL`, `SOURCE_SHA256`,
`SOURCE_FILENAME` set. It must leave `$STRATA_QEMU_CACHE/downloads/$SOURCE_FILENAME`
with the pinned SHA-256 and print that path. Copy `images/ubuntu-2404/bootstrap.sh`
and change the default `NAME`. Only Fedora needs the directory-scrape
variant.

## setup.sh

Runs inside the guest over SSH from `/tmp/setup.sh`. Cloud-image guests
run it as `sudo -n bash`; ISO guests run it with password sudo via
`sudo -S`, so an ISO `setup.sh` must start with the
`if [[ "$(id -u)" -ne 0 ]]; then exec sudo -n bash "$0" "$@"; fi` re-exec
pattern from `images/omarchy-4/setup.sh`.

It must leave the guest in this state or `run-test` cannot work:

| Requirement | Why |
| --- | --- |
| Display manager autologin for `tester` into a Wayland session | `smoke-session.sh` needs `Type=wayland Seat=seat0 State=active` |
| `graphical.target` default; first-run wizards masked | session must be up without interaction |
| `tester ALL=(ALL) NOPASSWD: ALL` | later `sudo -n` |
| `loginctl enable-linger tester` | user bus exists for SSH commands |
| `qemu-guest-agent` enabled and started | shutdown cascade, omarchy-3 skip-wizard |
| Screenshot tool: `gnome-screenshot` (GNOME) or `grim` (Hyprland) | screenshot step; missing binary fails the run |
| `jq` on Hyprland guests | window oracle uses `hyprctl clients -j \| jq` |
| Strata runtime deps from `[packages].runtime` | install.sh with `--non-interactive` trusts installed packages |
| `/var/tmp/strata-inventory.txt`, `strata-glibc.txt`, `strata-gtk.txt`, mode 644 | copied into provenance JSON; missing file fails the build |

Do not install Strata or Omarchy on non-Omarchy guests; `arch/setup.sh`
asserts both are absent.

Extra files `setup.sh` needs (greetd config, Hyprland config) go in the
recipe dir, are listed in `image_build.RECIPE_UPLOAD_NAMES`, and are
uploaded next to `setup.sh` in `/tmp/`. New names must also be added to
`guest.DIGEST_OPTIONAL_BASENAMES` so edits rebuild the golden.

## Cloud-init template

`user-data.yaml.tmpl` must contain `{{SSH_AUTHORIZED_KEY}}` exactly once
in `ssh_authorized_keys`. Keep `tester` with `NOPASSWD`, password
`foobar`, `qemu-guest-agent` in `packages`, and `loginctl enable-linger
tester` in `runcmd`. `cloudinit.render_user_data` rejects any rendered
text containing GitHub or Tailscale token patterns.

## Omarchy cidata

Dump `user_configuration.json` from a manual archinstall run and keep it
unencrypted, `default_layout`, targeting `/dev/vda`, with 1 MiB-aligned
partitions. `cloudinit.validate_omarchy_configuration` enforces this and
the 3.x vs 4.x bootloader schema. `user_credentials.json.tmpl` must list
`tester` with `sudo: true` and a `$6$` root hash (`openssl passwd -6
foobar`). `user_encrypt_installation.txt` is the literal `false`.

## Host-side lookups that key on guest id

These must be updated or the new guest is rejected or misclassified:

- `strataqemu/qemu.py`: `ISO_AUTOINSTALL_GUESTS`, `CLOUD_INIT_SEED_GUESTS`.
- `strataqemu/tests_spec.py`: `INSTALL_FROM_RELEASE_GUESTS` (enables
  `--install-from`); `compositor_process_name` (maps to `gnome-shell` or
  `Hyprland`; falls back to `[session].compositor`, so a new GNOME or
  Hyprland guest may work without edits if `kind`/`compositor` are set).
- `strataqemu/image_build.py`: `omarchy_major_for_guest` and
  `kick_omarchy3_skip_wizard` for ISO guests.
- `strataqemu/cli.py` help strings mention `ubuntu-2404` as the example;
  no change needed.

## Tests

Add a `<Id>RecipeTests` class to `tests/test_guest_toml.py` modeled on the
sibling. At minimum: `test_load_real_recipe` (fields, `uses_*` helpers),
a `/latest/` fail-closed test, a digest-covers-files test, and a
`setup.sh` content test that greps for the autologin/NOPASSWD/screenshot
tool lines. Never spawn QEMU in tests.

## Re-pinning a source

Update `source_url`, `source_sha256`, and any `snapshot_url` together to
the same date. For Arch, `setup.sh` HEADs `${snapshot}/core/os/x86_64/core.db`
and fails if the archive day is incomplete. Run
`mise run image-build -- <id>`; the digest change forces a rebuild.

## Live verification

```bash
mise run image-build -- <id>            # 5-20 min cloud image, 10-30 min ISO
mise run run-test -- <id> --session-only
mise run vm-run -- <id> --graphical     # look at the desktop by eye
```

If the session smoke fails, `ssh.log` in the kept run dir shows the exact
`loginctl` output the script saw.
