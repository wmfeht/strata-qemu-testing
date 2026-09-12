# strata-qemu-testing

Reproducible QEMU/KVM guests for Strata installer and desktop-session testing.

This repo is complementary to Strata’s container E2E (`tests/e2e`): that suite
is widget-and-scenario evidence under Xvfb. This one boots a real desktop
session (compositor, autologin, session D-Bus) in a throwaway overlay.

The design (five-guest matrix, frozen QEMU argv, installer gates) is
[docs/design.md](docs/design.md). Adding a sixth guest is a design change.

## What works today

One guest is implemented: **`ubuntu-2404`** (Ubuntu 24.04, GDM autologin,
GNOME Shell / Wayland). You can:

- fail-closed host check
- build a content-addressed golden from the recipe
- run session + compositor + in-guest screenshot smoke (`--session-only`)
- boot an interactive throwaway overlay (`vm-run`)
- prune overlays / goldens
- run host unit tests (no KVM)

**Not implemented yet:** `arch`, `fedora-workstation`, `omarchy-4`,
`omarchy-3`; `run-test --install-from`; `strata --version`; window-after-install
(`gtk-launch` / bus-name wait). Ubuntu/Fedora installer smokes wait on Strata
flags that do not exist (`--archive PATH`, non-Arch `--non-interactive` that
trusts installed packages).

Documented entry is `mise run <task>`. `python -m strataqemu …` is the
implementation. `scripts/` are thin shims, not the operator path.

## First run

Linux x86_64 host with KVM. macOS and Windows are out of scope. Do not run
as root.

1. Install mise **outside this repo**. Prefer the distro package (Arch
   `pacman -S mise`, Fedora COPR, Debian/Ubuntu extrepo/PPA). Review any
   upstream installer before piping it to a shell. This repository does not
   `curl | sh` mise from a task. Confirm `mise bootstrap --help` exists.

2. Clone, review `mise.toml` (it is executable config), then:

```bash
mise trust
mise install             # Python 3.11 from [tools] / mise.lock
mise bootstrap           # host packages + tools + check-host
mise run check-host      # fail-closed; generates $CACHE/keys/
```

If the host package manager is not apt, dnf, or pacman, install qemu
(including GL modules), OVMF, xorriso, ssh, and curl yourself, then run
`mise run check-host`.

`mise bootstrap` does **not** install KVM, `/dev/kvm` access, or a working
GPU/EGL stack. Add your user to the `kvm` group and re-login if
`check-host` says so. TCG fallback is not supported.

**`mise bootstrap` is not `images/*/bootstrap.sh`.** The former installs host
packages. The latter downloads the guest cloud image / ISO into the cache.

## Daily commands

```bash
mise run check-host
mise run image-build -- ubuntu-2404
mise run run-test -- ubuntu-2404 --session-only
MISE_TASK_OUTPUT=interleave mise run vm-run -- ubuntu-2404 --graphical
mise run image-prune                 # overlays and run dirs; not goldens
mise run image-prune -- --images     # also drop goldens; never $CACHE/keys/
mise run test                        # unittest; no KVM
```

Put mise flags *before* the task name (`mise run --quiet image-build -- ubuntu-2404`).
With `raw_args = true`, `mise run run-test --help` reaches argparse. Flags
after `--` belong to `strataqemu`.

`image-build` is incremental: a golden matching the current recipe + source
checksum is printed and the command exits 0. `--force` rebuilds.

`run-test` **never** builds a golden. `[tasks.run-test] depends` is
`check-host` only. Missing golden exits non-zero with
`run \`mise run image-build -- ubuntu-2404\` first`.

Success keeps the run dir’s screenshot, `result.json`, and logs, and deletes
the throwaway overlay. `--keep` retains the overlay too. Failures keep the
whole run dir.

Hidden, operator-gated spike (not a daily command):
`mise run spike-wayland-ubuntu`.

## Guest: ubuntu-2404

| | |
| --- | --- |
| Source | Dated Noble cloudimg (not `…/noble/current/`) + SHA-256 in `images/ubuntu-2404/image.toml` |
| APT | `snapshot.ubuntu.com` dated index; must not predate the cloud image |
| Session | GDM autologin → GNOME Shell Wayland; initial-setup / tour masked |
| Disk / RAM / CPUs | 40 GiB / 8192 MiB / 4 |
| User | `tester` / `foobar` (test-only; user-net only, never bridge) |
| SSH | ed25519 key generated into `$CACHE/keys/` on first `check-host` |

`--session-only` asserts an active `Type=wayland` seat0 session, `gnome-shell`,
a Wayland socket, and an in-guest `gnome-screenshot`. It does **not** run
`install.sh` or launch Strata.

## Cache

Default `$XDG_CACHE_HOME/strata-qemu-testing` (else `~/.cache/strata-qemu-testing`).
Override with `STRATA_QEMU_CACHE`. Put it on a large disk, not a small `/tmp`.

```
$CACHE/images/ubuntu-2404-<digest>.qcow2   # golden (read-only backing)
$CACHE/images/ubuntu-2404.qcow2            # convenience symlink
$CACHE/images/ubuntu-2404.json             # provenance
$CACHE/downloads/                          # cloudimg blob
$CACHE/runs/<stamp>-ubuntu-2404/           # overlay, logs, screenshot, result.json
$CACHE/keys/id_ed25519                     # generated; not in git
```

Goldens are per-machine cache, rebuilt from the recipe. Only `keys/README.md`
is committed.

## Host tests

```bash
mise run test
```

These are stdlib `unittest` on the shipped CLI, recipes, and smoke scripts.
They inject fixtures and refuse `qemu-system-*`. Live `image-build` /
`run-test` need KVM, virgl, and (for `run-test`) a matching golden.
)
