"""Cache and key paths shared by python -m and mise tasks."""

from __future__ import annotations

import os
from pathlib import Path

CACHE_ENV = "STRATA_QEMU_CACHE"
INSTALL_SH_ENV = "STRATA_QEMU_INSTALL_SH"
UPDATE_FROM_ENV = "STRATA_QEMU_UPDATE_FROM"
CACHE_DIRNAME = "strata-qemu-testing"
SSH_KEY_NAME = "id_ed25519"


def install_sh_from_env() -> Path | None:
    """Host ``install.sh`` to upload instead of curling ``lgse/strata`` main.

    Used to exercise a PR copy (for example lgse/strata#743) inside the guest.
    """
    raw = os.environ.get(INSTALL_SH_ENV)
    if not raw:
        return None
    return Path(raw).expanduser()


def cache_dir() -> Path:
    """Return the cache root.

    Override with STRATA_QEMU_CACHE. Default is
    $XDG_CACHE_HOME/strata-qemu-testing, else ~/.cache/strata-qemu-testing.
    """
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / CACHE_DIRNAME
    return Path.home() / ".cache" / CACHE_DIRNAME


def keys_dir(root: Path | None = None) -> Path:
    return (root if root is not None else cache_dir()) / "keys"


def ssh_private_key(root: Path | None = None) -> Path:
    return keys_dir(root) / SSH_KEY_NAME
