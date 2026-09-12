"""Argparse CLI: check-host and later-PR stubs."""

from __future__ import annotations

import argparse
import glob
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from strataqemu import config

log = logging.getLogger("strataqemu")

REQUIRED_BINARIES = (
    "qemu-system-x86_64",
    "qemu-img",
    "xorriso",
    "ssh",
    "curl",
)

# Relative to each firmware share root (default /usr/share). 4M non-secboot first.
OVMF_CODE_RELATIVE = (
    Path("edk2/x64/OVMF_CODE.4m.fd"),
    Path("OVMF/OVMF_CODE_4M.fd"),
    Path("edk2/ovmf/OVMF_CODE.fd"),
)

DEFAULT_FIRMWARE_SHARE_ROOTS: tuple[Path, ...] = (Path("/usr/share"),)

# Generic floor when no guest is selected: 8 GiB guest + 1 GiB host slack.
GENERIC_MEM_FLOOR_MIB = 9 * 1024

STUB_COMMANDS = ("image-build", "run-test", "vm-run", "image-prune")

# Named when qemu-system-x86_64 is missing. Not a generic "install qemu".
QEMU_BOOTSTRAP_HINT = """\
Install host packages with `mise bootstrap` (apt / dnf / pacman).
QEMU system emulator:
  pacman: qemu-system-x86
  apt:    qemu-system-x86
  dnf:    qemu-system-x86
GL modules matching egl-headless + virtio-gpu-gl-pci:
  pacman: qemu-ui-egl-headless, qemu-hw-display-virtio-gpu-pci-gl
  apt:    qemu-system-gui
  dnf:    qemu-device-display-virtio-gpu-pci-gl
"""


@dataclass(frozen=True)
class CheckHostEnv:
    """Injectable host state so unit tests never touch this machine's /dev/kvm."""

    path: str
    kvm_path: Path
    kvm_accessible: Callable[[Path], bool]
    cache_dir: Path
    firmware_share_roots: Sequence[Path]
    virgl_ok: bool
    mem_available_mib: int | None
    euid: int
    keygen: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class CheckHostResult:
    ok: bool
    errors: tuple[str, ...]
    ovmf_code: Path | None = None
    ssh_key: Path | None = None


def ovmf_code_candidates(share_roots: Sequence[Path] | None = None) -> list[Path]:
    roots = tuple(share_roots) if share_roots else DEFAULT_FIRMWARE_SHARE_ROOTS
    return [root / rel for root in roots for rel in OVMF_CODE_RELATIVE]


def _is_secboot(path: Path) -> bool:
    return "secboot" in path.name.lower()


def _is_4m(path: Path) -> bool:
    return "4m" in path.name.lower()


def find_ovmf_code(candidates: Sequence[Path]) -> Path | None:
    """First existing 4M non-secboot firmware in search order.

    Skips any path whose name contains ``secboot``. Among hits, prefers a
    4M-named file over a later generic ``OVMF_CODE.fd``.
    """
    found: list[Path] = []
    for cand in candidates:
        if cand.is_file() and not _is_secboot(cand):
            found.append(cand)
    if not found:
        return None
    four_m = [p for p in found if _is_4m(p)]
    if four_m:
        return four_m[0]
    return found[0]


def probe_virgl_egl() -> bool:
    """libvirglrenderer + an EGL DRM device. Does not spawn QEMU."""
    lib_patterns = (
        "/usr/lib/libvirglrenderer.so*",
        "/usr/lib64/libvirglrenderer.so*",
        "/usr/lib/*/libvirglrenderer.so*",
    )
    has_lib = any(glob.glob(pattern) for pattern in lib_patterns)
    dri = Path("/dev/dri")
    has_device = dri.is_dir() and any(dri.glob("renderD*"))
    return has_lib and has_device


def read_mem_available_mib() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            kib = int(line.split()[1])
            return kib // 1024
    return None


def default_env() -> CheckHostEnv:
    return CheckHostEnv(
        path=os.environ.get("PATH", ""),
        kvm_path=Path("/dev/kvm"),
        kvm_accessible=lambda p: os.access(p, os.R_OK | os.W_OK),
        cache_dir=config.cache_dir(),
        firmware_share_roots=DEFAULT_FIRMWARE_SHARE_ROOTS,
        virgl_ok=probe_virgl_egl(),
        mem_available_mib=read_mem_available_mib(),
        euid=os.geteuid(),
    )


def ensure_ssh_key(key_path: Path, *, path: str | None = None) -> Path:
    """Generate ed25519 key with empty passphrase if missing."""
    if key_path.is_file():
        return key_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    keygen = shutil.which("ssh-keygen", path=path) or shutil.which("ssh-keygen")
    if keygen is None:
        raise FileNotFoundError(
            "ssh-keygen not found; OpenSSH is required to generate $CACHE/keys/"
        )
    subprocess.run(
        [keygen, "-t", "ed25519", "-f", str(key_path), "-N", ""],
        check=True,
        capture_output=True,
        text=True,
    )
    return key_path


def check_host(env: CheckHostEnv | None = None) -> CheckHostResult:
    """Fail-closed capability check. Does not install packages or spawn QEMU."""
    env = env if env is not None else default_env()
    errors: list[str] = []

    if env.euid == 0:
        errors.append(
            "Refusing to run as root. Never run QEMU workflows as root."
        )

    kvm = env.kvm_path
    if not kvm.exists():
        errors.append(
            f"{kvm} does not exist. KVM is required; TCG fallback is not supported. "
            "Load the kvm module. Hint: add your user to the kvm group and re-login."
        )
    elif not env.kvm_accessible(kvm):
        errors.append(
            f"{kvm} is not readable and writable. "
            "Add your user to the kvm group and re-login. "
            "TCG fallback is not supported."
        )

    missing_bins: list[str] = []
    for name in REQUIRED_BINARIES:
        if shutil.which(name, path=env.path) is None:
            missing_bins.append(name)
    if "qemu-system-x86_64" in missing_bins:
        errors.append(
            "qemu-system-x86_64 is not on PATH. "
            + QEMU_BOOTSTRAP_HINT.strip()
        )
    for name in missing_bins:
        if name == "qemu-system-x86_64":
            continue
        errors.append(
            f"{name} is not on PATH. Install host packages with `mise bootstrap`."
        )

    if not env.virgl_ok:
        errors.append(
            "virgl/egl is not usable (need libvirglrenderer and an EGL DRM device, "
            "or qemu-system-x86_64 -display egl-headless,gl=on -device virtio-gpu-gl-pci). "
            "Install virgl/EGL userspace with `mise bootstrap`; a working GPU/EGL stack "
            "is still fail-closed if the device is missing."
        )

    candidates = ovmf_code_candidates(env.firmware_share_roots)
    ovmf = find_ovmf_code(candidates)
    if ovmf is None:
        searched = ", ".join(str(p) for p in candidates)
        errors.append(
            "OVMF 4M non-secboot firmware not found "
            f"(search order: {searched}). "
            "Install edk2-ovmf / ovmf with `mise bootstrap`. "
            "Secure Boot firmware (.secboot) is not accepted."
        )

    mem = env.mem_available_mib
    if mem is None:
        errors.append("Could not read MemAvailable; cannot verify host RAM floor.")
    elif mem < GENERIC_MEM_FLOOR_MIB:
        errors.append(
            f"MemAvailable is {mem} MiB; need at least {GENERIC_MEM_FLOOR_MIB} MiB "
            "(9 GiB floor when no guest is selected)."
        )

    if errors:
        return CheckHostResult(ok=False, errors=tuple(errors), ovmf_code=ovmf)

    key_path = config.ssh_private_key(env.cache_dir)
    try:
        if env.keygen is not None:
            if not key_path.is_file():
                env.keygen(key_path)
        else:
            ensure_ssh_key(key_path, path=env.path)
    except (OSError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        return CheckHostResult(
            ok=False,
            errors=(f"failed to generate SSH key at {key_path}: {exc}",),
            ovmf_code=ovmf,
        )

    return CheckHostResult(
        ok=True,
        errors=(),
        ovmf_code=ovmf,
        ssh_key=key_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m strataqemu",
        description=(
            "Host-side QEMU/KVM workflows for Strata distro-matrix testing. "
            "Documented operator entry is mise run <task>."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging to stderr",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check-host",
        help="Fail-closed host check: /dev/kvm, qemu, virgl/egl, OVMF 4M, xorriso, ssh, curl",
    )

    image_build = sub.add_parser(
        "image-build",
        help="Build or refresh a golden from its recipe (later PR)",
    )
    image_build.add_argument("guest", nargs="?", help="Guest id")
    image_build.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even if a matching golden exists",
    )

    run_test = sub.add_parser(
        "run-test",
        help="Boot overlay and run smokes (later PR)",
    )
    run_test.add_argument("guest", nargs="?", help="Guest id")
    run_test.add_argument(
        "--session-only",
        action="store_true",
        help="Session/window smoke without install.sh",
    )
    run_test.add_argument(
        "--install-from",
        choices=("release", "local-archive"),
        help="Install Strata from GitHub latest or a local archive",
    )
    run_test.add_argument(
        "--keep",
        action="store_true",
        help="Keep the throwaway overlay after success",
    )

    vm_run = sub.add_parser(
        "vm-run",
        help="Interactive throwaway overlay (later PR)",
    )
    vm_run.add_argument("guest", nargs="?", help="Guest id")
    vm_run.add_argument(
        "--graphical",
        action="store_true",
        help="GTK/SDL GL display instead of egl-headless",
    )
    vm_run.add_argument(
        "--keep",
        action="store_true",
        help="Keep the throwaway overlay",
    )

    prune = sub.add_parser(
        "image-prune",
        help="Drop overlays and old run dirs (later PR)",
    )
    prune.add_argument(
        "--images",
        action="store_true",
        help="Also delete golden images",
    )
    return parser


def _run_check_host() -> int:
    result = check_host()
    for err in result.errors:
        print(err, file=sys.stderr)
    if not result.ok:
        return 1
    print("check-host: ok")
    if result.ovmf_code is not None:
        print(f"OVMF: {result.ovmf_code}")
    if result.ssh_key is not None:
        print(f"SSH key: {result.ssh_key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(message)s",
    )

    if args.command == "check-host":
        return _run_check_host()

    if args.command in STUB_COMMANDS:
        print(
            f"{args.command}: not implemented yet (fail closed). See docs/design.md.",
            file=sys.stderr,
        )
        return 2

    parser.error(f"unknown command {args.command!r}")
    return 2
