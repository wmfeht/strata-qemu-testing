"""Cache layout and per-run artifact paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from strataqemu import config


def cache_root(root: Path | None = None) -> Path:
    return root if root is not None else config.cache_dir()


def images_dir(root: Path | None = None) -> Path:
    return cache_root(root) / "images"


def runs_dir(root: Path | None = None) -> Path:
    return cache_root(root) / "runs"


def downloads_dir(root: Path | None = None) -> Path:
    return cache_root(root) / "downloads"


@dataclass(frozen=True)
class RunArtifacts:
    """Well-known files under ``$CACHE/runs/<id>/``."""

    root: Path

    @property
    def overlay(self) -> Path:
        return self.root / "overlay.qcow2"

    @property
    def serial_log(self) -> Path:
        return self.root / "serial.log"

    @property
    def qemu_log(self) -> Path:
        return self.root / "qemu.log"

    @property
    def qmp_sock(self) -> Path:
        return self.root / "qmp.sock"

    @property
    def qga_sock(self) -> Path:
        return self.root / "qga.sock"

    @property
    def ovmf_vars(self) -> Path:
        return self.root / "OVMF_VARS.fd"

    @property
    def result_json(self) -> Path:
        return self.root / "result.json"

    @property
    def ssh_port_file(self) -> Path:
        return self.root / "ssh_port"
