# strata-qemu-testing

Reproducible QEMU guests for Strata installer and desktop-session testing.

This README is **first-run host setup only**. The design (guest matrix, QEMU
argv, recipes) is in [docs/design.md](docs/design.md).

## First run

Linux x86_64 host with KVM. macOS and Windows are out of scope.

1. Install mise **outside this repo**. Prefer the distro package (Arch
   `pacman -S mise`, Fedora COPR, Debian/Ubuntu extrepo/PPA). Review any
   upstream installer before piping it to a shell. This repository does not
   `curl | sh` mise from a task. Confirm `mise bootstrap --help` exists.

2. Clone, review `mise.toml` (it is executable config), then:

```bash
mise trust
mise install             # Python 3.11 from [tools] / mise.lock
mise bootstrap           # host packages + tools + check-host
mise run check-host      # fail-closed capability check; generates $CACHE/keys/
```

If the host package manager is not apt, dnf, or pacman, install qemu
(including GL modules), OVMF, xorriso, ssh, and curl yourself, then run
`mise run check-host`.
