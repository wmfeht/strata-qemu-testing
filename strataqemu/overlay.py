"""qcow2 overlay create (absolute backing, backing_file_strict) and cache prune."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from strataqemu import artifacts, config

log = logging.getLogger("strataqemu")


def create_overlay_argv(
    golden: Path | str,
    overlay: Path | str,
    *,
    qemu_img: str = "qemu-img",
) -> list[str]:
    """Argv for ``qemu-img create`` with a strict absolute backing file.

    Matches the frozen command:

    ``qemu-img create -f qcow2 -F qcow2 -o backing_file_strict=on -b <abs> <overlay>``
    """
    golden_abs = str(Path(golden).resolve())
    return [
        qemu_img,
        "create",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-o",
        "backing_file_strict=on",
        "-b",
        golden_abs,
        str(overlay),
    ]


def _without_backing_file_strict(argv: list[str]) -> list[str]:
    """Drop ``backing_file_strict`` from ``-o`` for qemu-img that rejects it."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "-o" and i + 1 < len(argv):
            kept = [
                part
                for part in argv[i + 1].split(",")
                if part and "backing_file_strict" not in part
            ]
            if kept:
                out.extend(["-o", ",".join(kept)])
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def create_overlay(
    golden: Path | str,
    overlay: Path | str,
    *,
    qemu_img: str = "qemu-img",
) -> Path:
    """Create a throwaway overlay. Does not open the golden for writing.

    Argv always includes ``backing_file_strict=on``. If this host's qemu-img
    rejects that parameter, retry with the same absolute ``-b`` and ``-F``.
    """
    dest = Path(overlay)
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = create_overlay_argv(golden, dest, qemu_img=qemu_img)
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        err = f"{exc.stderr or ''}{exc.stdout or ''}"
        if "backing_file_strict" not in err.lower():
            raise
        fallback = _without_backing_file_strict(argv)
        log.warning(
            "qemu-img rejected backing_file_strict; retrying with absolute backing"
        )
        subprocess.run(fallback, check=True, capture_output=True, text=True)
    return dest


def copy_uefi_vars(
    dest: Path | str,
    *,
    template_vars: Path | str,
    golden_vars: Path | str | None = None,
) -> Path:
    """Fresh copy of golden ``*.vars.fd`` if present, else the template vars.

    Uses ``copyfile`` so the source is opened read-only.
    """
    src_path: Path
    if golden_vars is not None and Path(golden_vars).is_file():
        src_path = Path(golden_vars)
    else:
        src_path = Path(template_vars)
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_path, dest_path)
    return dest_path


def prune(*, images: bool = False, cache_dir: Path | None = None) -> None:
    """Delete ``$CACHE/runs/*``. With ``images=True``, also ``$CACHE/images/*``.

    Never deletes ``$CACHE/keys/``.
    """
    root = cache_dir if cache_dir is not None else config.cache_dir()
    runs = artifacts.runs_dir(root)
    if runs.exists():
        shutil.rmtree(runs)
        log.info("pruned %s", runs)
    if images:
        goldens = artifacts.images_dir(root)
        if goldens.exists():
            shutil.rmtree(goldens)
            log.info("pruned %s", goldens)
