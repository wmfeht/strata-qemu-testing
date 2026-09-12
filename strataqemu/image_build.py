"""Build or refresh a golden from its recipe.

Incremental: a content-addressed qcow2 matching the current recipe+source
digest is printed and the command exits 0. ``--force`` rebuilds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from strataqemu import artifacts, config
from strataqemu.cli import (
    DEFAULT_FIRMWARE_SHARE_ROOTS,
    CheckHostResult,
    check_host,
    find_ovmf_code,
)
from strataqemu.cloudinit import (
    write_cidata_iso_from_recipe,
    write_omarchy_cidata_iso,
)
from strataqemu.guest import Guest, GuestError, load_guest
from strataqemu.overlay import copy_uefi_vars
from strataqemu.ports import (
    AddressAlreadyInUse,
    allocate_ssh_port,
    allocate_vnc_port,
    is_address_already_in_use,
    retry_on_addr_in_use,
)
from strataqemu.qemu import (
    Machine,
    build_qemu_argv,
    qmp_screendump,
    uses_iso_autoinstall,
    vnc_framebuffer_png,
)
from strataqemu.ssh import scp_command, scp_download_command

log = logging.getLogger("strataqemu")

# Noble GDM / cloud-init first boot can exceed boot_timeout_s; wait cloud-init
# with a separate budget, then setup.sh uses build_timeout_s.
CLOUD_INIT_TIMEOUT_S = 600
# Consecutive pubkey rejects (sshd is up but tester cannot log in). The live
# ISO wizard answers SSH this way forever if cidata was not loaded. A healthy
# autoinstall also rejects tester until reboot; qcow2 growth cancels the streak.
SSH_AUTH_REJECT_MAX = 30
# qcow2 actual size after partitioning/mkfs; empty working disks stay ~200KiB.
ISO_AUTOINSTALL_DISK_PROGRESS_BYTES = 1024 * 1024
SSH_WAIT_SLEEP_S = 2.0
GIB = 1024**3
# When the cloudimg is not on disk yet, assume 1 GiB for the free-space floor.
UNKNOWN_SOURCE_BYTES = GIB

OVMF_VARS_RELATIVE = (
    Path("edk2/x64/OVMF_VARS.4m.fd"),
    Path("OVMF/OVMF_VARS_4M.fd"),
    Path("edk2/ovmf/OVMF_VARS.fd"),
)

RunFn = Callable[..., subprocess.CompletedProcess]
PopenFn = Callable[..., subprocess.Popen]

RECIPE_UPLOAD_NAMES = (
    "setup.sh",
    "greetd-config.toml",
    "hyprland.lua",
    "hyprland.conf",
)
# Well-known test password (docs/design.md). ISO autoinstall sudoers is
# password sudo until setup.sh writes NOPASSWD.
WELL_KNOWN_TEST_PASSWORD = "foobar"


class ImageBuildError(RuntimeError):
    """Live image-build failure."""


def ovmf_vars_candidates(
    share_roots: Sequence[Path] | None = None,
) -> list[Path]:
    roots = tuple(share_roots) if share_roots else DEFAULT_FIRMWARE_SHARE_ROOTS
    return [root / rel for root in roots for rel in OVMF_VARS_RELATIVE]


def find_ovmf_vars(share_roots: Sequence[Path] | None = None) -> Path | None:
    return find_ovmf_code(ovmf_vars_candidates(share_roots))


def inventory_basename(guest: Guest) -> str:
    """Local inventory filename under the run dir.

    Fedora records ``rpm -qa`` as ``rpm.txt``; Ubuntu (and Arch today)
    keep the existing ``dpkg.txt`` path.
    """
    if guest.packages.manager == "dnf":
        return "rpm.txt"
    return "dpkg.txt"


def inventory_provenance_key(guest: Guest) -> str:
    """Provenance JSON key for the package inventory blob."""
    if guest.packages.manager == "dnf":
        return "rpm"
    return "dpkg"


def setup_ssh_command(guest: Guest) -> str:
    """Remote command that runs the uploaded recipe ``setup.sh``.

    Cloud-init goldens already have NOPASSWD from user-data, so ``sudo -n``
    works. ISO autoinstall (Omarchy) only grants password sudo
    (``ALL=(ALL) ALL``) until setup.sh writes NOPASSWD, so authenticate
    with the well-known test password via ``sudo -S``.
    """
    env_prefix = ""
    if guest.packages.snapshot_url:
        env_prefix = f"SNAPSHOT_URL={shlex.quote(guest.packages.snapshot_url)} "
    script = "bash /tmp/setup.sh"
    if guest.source_kind == "iso-autoinstall":
        password = shlex.quote(WELL_KNOWN_TEST_PASSWORD)
        return (
            f"{env_prefix}printf '%s\\n' {password} "
            f"| sudo -S -p '' {script}"
        )
    return f"{env_prefix}sudo -n {script}"


def recipe_files_to_upload(guest: Guest) -> tuple[Path, ...]:
    """setup.sh plus session drop-ins that setup copies into the guest."""
    found: list[Path] = []
    for name in RECIPE_UPLOAD_NAMES:
        path = guest.recipe_dir / name
        if path.is_file():
            found.append(path)
    if not any(path.name == "setup.sh" for path in found):
        raise ImageBuildError(f"missing setup.sh in {guest.recipe_dir}")
    return tuple(found)


def golden_filename(guest: Guest) -> str:
    return f"{guest.id}-{guest.golden_digest()}.qcow2"


def golden_qcow2(guest: Guest, cache: Path) -> Path:
    return artifacts.images_dir(cache) / golden_filename(guest)


def golden_vars_fd(guest: Guest, cache: Path) -> Path:
    """Post-install OVMF vars; golden pair is ``(qcow2, vars.fd)``."""
    return artifacts.images_dir(cache) / f"{guest.id}.vars.fd"


def golden_symlink(guest: Guest, cache: Path) -> Path:
    return artifacts.images_dir(cache) / f"{guest.id}.qcow2"


def provenance_path(guest: Guest, cache: Path) -> Path:
    return artifacts.images_dir(cache) / f"{guest.id}.json"


def required_free_bytes(disk_gb: int, source_bytes: int) -> int:
    """``2 * disk_gb + source + 10`` GiB (working copy + golden + slack)."""
    return (2 * disk_gb + 10) * GIB + max(0, source_bytes)


def source_size_bytes(download: Path) -> int:
    try:
        if download.is_file():
            return download.stat().st_size
    except OSError:
        return UNKNOWN_SOURCE_BYTES
    return UNKNOWN_SOURCE_BYTES


def working_qemu_argv(
    *,
    overlay: Path | str,
    run_dir: Path | str,
    ssh_port: int,
    vnc_port: int,
    cpus: int,
    memory_mib: int,
    ovmf_code: Path | str | None,
    ovmf_vars: Path | str | None,
    cidata_iso: Path | str,
    install_iso: Path | str | None = None,
) -> list[str]:
    """Frozen GL argv for the image-build working disk (``cache=writeback``)."""
    return build_qemu_argv(
        overlay=overlay,
        run_dir=run_dir,
        ssh_port=ssh_port,
        vnc_port=vnc_port,
        cpus=cpus,
        memory_mib=memory_mib,
        graphical=False,
        disk_cache="writeback",
        ovmf_code=ovmf_code,
        ovmf_vars=ovmf_vars,
        cidata_iso=cidata_iso,
        install_iso=install_iso,
    )


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def _ssh_run(
    machine: Machine,
    command: str,
    *,
    timeout: float = 120,
    check: bool = False,
    run: RunFn | None = None,
) -> subprocess.CompletedProcess[str]:
    runner = run or subprocess.run
    argv = machine.ssh(command)
    log.debug("ssh: %s", command)
    try:
        proc = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        _append_log(
            machine.artifacts.root / "ssh.log",
            f"$ {command}\n[timeout {timeout}s]\n{out}{err}",
        )
        raise
    blob = f"$ {command}\n[exit {proc.returncode}]\n{proc.stdout}{proc.stderr}"
    _append_log(machine.artifacts.root / "ssh.log", blob)
    if check and proc.returncode != 0:
        raise ImageBuildError(
            f"ssh command failed ({proc.returncode}): {command}\n"
            f"{proc.stdout}{proc.stderr}"
        )
    return proc


def ssh_auth_rejected(returncode: int, output: str) -> bool:
    """True when sshd answered but tester cannot log in.

    The live ISO drops unauthenticated sessions after a few pubkey
    failures (``Connection closed``). That is still a reject, not
    "sshd is not up yet" — resetting the streak on close never reached
    ``SSH_AUTH_REJECT_MAX``.
    """
    if returncode == 0:
        return False
    blob = output.lower()
    if "permission denied" in blob:
        return True
    if "connection closed" in blob:
        return True
    if "connection reset" in blob:
        return True
    return False


def working_disk_bytes(machine: Machine) -> int:
    """Actual qcow2 file size. Grows once autoinstall starts writing ``/dev/vda``."""
    try:
        return machine.overlay.stat().st_size
    except OSError:
        return 0


def ssh_not_listening(output: str) -> bool:
    """True when sshd has not answered yet (keep waiting, reset reject streak)."""
    blob = output.lower()
    if "connection refused" in blob:
        return True
    if "timed out" in blob or "timeout" in blob:
        return True
    if "banner" in blob:
        return True
    return False


def _default_max_attempts(timeout: float) -> int:
    """One loop body per sleep; never unbounded even if timeout is huge."""
    return max(1, int(timeout // SSH_WAIT_SLEEP_S) + 1)


def wait_ssh(
    machine: Machine,
    *,
    timeout: float,
    run: RunFn | None = None,
    sleep: Callable[[float], None] | None = None,
    max_attempts: int | None = None,
    auth_reject_max: int = SSH_AUTH_REJECT_MAX,
) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    attempts = 0
    auth_rejects = 0
    limit = _default_max_attempts(timeout) if max_attempts is None else max_attempts
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        attempts += 1
        if attempts > limit:
            raise TimeoutError(
                f"SSH did not become ready after {limit} attempts: {last}"
            )
        if machine._proc is not None and machine._proc.poll() is not None:
            qemu_log = ""
            try:
                qemu_log = machine.artifacts.qemu_log.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                pass
            raise ImageBuildError(
                f"qemu exited {machine._proc.returncode} while waiting for SSH\n"
                f"{qemu_log[-4000:]}"
            )
        try:
            proc = _ssh_run(machine, "true", timeout=15, run=run)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
            if ssh_not_listening(last):
                auth_rejects = 0
            nap(SSH_WAIT_SLEEP_S)
            continue
        if proc.returncode == 0:
            return
        last = (proc.stderr or proc.stdout or "").strip()
        if ssh_auth_rejected(proc.returncode, last):
            auth_rejects += 1
            log.debug(
                "SSH auth rejected (%s/%s): %s",
                auth_rejects,
                auth_reject_max,
                last.splitlines()[-1] if last else last,
            )
            if auth_rejects >= auth_reject_max:
                capture_build_timeout_evidence(machine)
                raise ImageBuildError(
                    f"SSH rejected tester {auth_rejects} times "
                    f"(authorized_keys not installed?): {last}"
                )
        elif ssh_not_listening(last):
            auth_rejects = 0
        nap(SSH_WAIT_SLEEP_S)
    raise TimeoutError(f"SSH did not become ready in {timeout}s: {last}")


def cloud_init_is_done(returncode: int, output: str) -> bool:
    blob = output.lower()
    if "status: done" in blob:
        return True
    return returncode == 0


def wait_cloud_init(
    machine: Machine,
    *,
    timeout: float = CLOUD_INIT_TIMEOUT_S,
    run: RunFn | None = None,
) -> None:
    proc = _ssh_run(
        machine, "cloud-init status --wait", timeout=timeout, run=run
    )
    blob = f"{proc.stdout}{proc.stderr}"
    if not cloud_init_is_done(proc.returncode, blob):
        raise ImageBuildError(
            f"cloud-init did not finish ({proc.returncode}): {blob[-2000:]}"
        )


def capture_build_timeout_evidence(machine: Machine) -> None:
    """Keep serial (already in the run dir), a screenshot, and best-effort QMP."""
    shot = machine.artifacts.root / "timeout.png"
    try:
        if machine.vnc_port is not None:
            vnc_framebuffer_png(shot, display=machine.vnc_port)
    except OSError:
        pass
    try:
        qmp_screendump(
            machine.artifacts.qmp_sock,
            machine.artifacts.root / "qmp-timeout.png",
        )
    except OSError:
        pass


def wait_iso_autoinstall(
    machine: Machine,
    *,
    major: int,
    timeout: float,
    run: RunFn | None = None,
    sleep: Callable[[float], None] | None = None,
    max_attempts: int | None = None,
) -> str:
    """Wait until SSH accepts tester and ``omarchy version`` is ``{major}.x``.

    The live ISO's sshd rejects ``tester`` both on the wizard and during a
    healthy autoinstall until reboot. Do not fail-fast on pubkey rejection;
    wait until ``timeout`` (omarchy-4: 3600s). Log qcow2 growth so a stuck
    wizard is distinguishable from an install in progress.
    """
    deadline = time.monotonic() + timeout
    last = ""
    ssh_ready = False
    attempts = 0
    logged_progress = False
    limit = _default_max_attempts(timeout) if max_attempts is None else max_attempts
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        attempts += 1
        if attempts > limit:
            capture_build_timeout_evidence(machine)
            raise TimeoutError(
                f"omarchy version {major}.x not ready after {limit} attempts: {last}"
            )
        if machine._proc is not None and machine._proc.poll() is not None:
            qemu_log = ""
            try:
                qemu_log = machine.artifacts.qemu_log.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                pass
            raise ImageBuildError(
                f"qemu exited {machine._proc.returncode} while waiting for "
                f"omarchy {major}.x\n{qemu_log[-4000:]}"
            )
        if not ssh_ready:
            try:
                proc = _ssh_run(machine, "true", timeout=15, run=run)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last = str(exc)
                nap(SSH_WAIT_SLEEP_S)
                continue
            if proc.returncode == 0:
                ssh_ready = True
            else:
                last = (proc.stderr or proc.stdout or "").strip()
                disk = working_disk_bytes(machine)
                if (
                    disk >= ISO_AUTOINSTALL_DISK_PROGRESS_BYTES
                    and not logged_progress
                ):
                    logged_progress = True
                    log.info(
                        "working disk %s bytes "
                        "(autoinstall writing, waiting for reboot)",
                        disk,
                    )
                elif ssh_auth_rejected(proc.returncode, last):
                    log.debug(
                        "SSH rejected tester (live ISO, waiting): %s",
                        last.splitlines()[-1] if last else last,
                    )
                nap(SSH_WAIT_SLEEP_S)
                continue
        try:
            proc = _ssh_run(machine, "omarchy version", timeout=15, run=run)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
            nap(SSH_WAIT_SLEEP_S)
            continue
        blob = f"{proc.stdout}{proc.stderr}"
        if omarchy_version_matches_major(blob, major):
            return blob.strip()
        last = blob.strip() or f"exit {proc.returncode}"
        nap(SSH_WAIT_SLEEP_S)
    capture_build_timeout_evidence(machine)
    raise TimeoutError(
        f"omarchy version {major}.x not ready in {timeout}s: {last}"
    )


def convert_and_resize(
    src: Path,
    dest: Path,
    disk_gb: int,
    *,
    run: RunFn | None = None,
) -> Path:
    """Copy ``src`` to a new qcow2 working disk and grow it. Never overlay."""
    runner = run or subprocess.run
    dest.parent.mkdir(parents=True, exist_ok=True)
    runner(
        ["qemu-img", "convert", "-O", "qcow2", str(src), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    runner(
        ["qemu-img", "resize", str(dest), f"{disk_gb}G"],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def create_blank_qcow2_argv(
    dest: Path | str,
    disk_gb: int,
    *,
    qemu_img: str = "qemu-img",
) -> list[str]:
    """Blank disk for ISO autoinstall. Never ``qemu-img convert`` the ISO."""
    return [qemu_img, "create", "-f", "qcow2", str(dest), f"{disk_gb}G"]


def create_blank_qcow2(
    dest: Path,
    disk_gb: int,
    *,
    run: RunFn | None = None,
) -> Path:
    """Create an empty qcow2. Do not convert the install ISO into the disk."""
    runner = run or subprocess.run
    dest.parent.mkdir(parents=True, exist_ok=True)
    runner(
        create_blank_qcow2_argv(dest, disk_gb),
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def omarchy_major_for_guest(guest: Guest | str) -> int:
    guest_id = guest.id if isinstance(guest, Guest) else guest
    if guest_id == "omarchy-4":
        return 4
    if guest_id == "omarchy-3":
        return 3
    raise ImageBuildError(f"{guest_id} is not an ISO autoinstall guest")


def omarchy_version_matches_major(output: str, major: int) -> bool:
    """True when ``omarchy version`` prints a ``{major}.x`` string.

    Rejects the other major, empty output, and configurator/wizard hang text.
    """
    text = output.strip()
    if not text:
        return False
    lowered = text.lower()
    for needle in ("configurator", "wizard", "waiting for", "interactive"):
        if needle in lowered:
            return False
    match = re.search(r"\b(\d+)\.(\d+)", text)
    if match:
        return int(match.group(1)) == major
    match = re.search(r"\bomarchy\s+(\d+)\b", lowered)
    if match:
        return int(match.group(1)) == major
    return False


def run_bootstrap(
    guest: Guest,
    cache: Path,
    *,
    run: RunFn | None = None,
) -> Path:
    script = guest.recipe_dir / "bootstrap.sh"
    env = os.environ.copy()
    env["STRATA_QEMU_CACHE"] = str(cache)
    env["SOURCE_URL"] = guest.source_url
    env["SOURCE_SHA256"] = guest.source_sha256
    env["SOURCE_FILENAME"] = guest.source_filename()
    runner = run or subprocess.run
    proc = runner(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise ImageBuildError(
            f"bootstrap.sh failed ({proc.returncode}): "
            f"{proc.stderr or proc.stdout}"
        )
    dest = artifacts.downloads_dir(cache) / guest.source_filename()
    if not dest.is_file():
        raise ImageBuildError(f"bootstrap.sh did not produce {dest}")
    return dest


def _spawn_qemu(
    *,
    guest: Guest,
    overlay: Path,
    run_dir: Path,
    ovmf_code: Path | None,
    ovmf_vars: Path | None,
    cidata_iso: Path,
    identity: Path,
    popen: PopenFn | None = None,
    install_iso: Path | None = None,
) -> Machine:
    arts = artifacts.RunArtifacts(run_dir)
    launcher = popen or subprocess.Popen

    def attempt() -> Machine:
        ssh_port = allocate_ssh_port()
        vnc_port = allocate_vnc_port()
        arts.ssh_port_file.write_text(f"{ssh_port}\n", encoding="utf-8")
        argv = working_qemu_argv(
            overlay=overlay,
            run_dir=run_dir,
            ssh_port=ssh_port,
            vnc_port=vnc_port,
            cpus=guest.cpus,
            memory_mib=guest.memory_mib,
            ovmf_code=ovmf_code,
            ovmf_vars=ovmf_vars,
            cidata_iso=cidata_iso,
            install_iso=install_iso,
        )
        log.debug("qemu argv: %s", " ".join(argv))
        logf = arts.qemu_log.open("ab")
        try:
            proc = launcher(
                argv,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            logf.close()
            raise
        time.sleep(0.4)
        if proc.poll() is not None:
            logf.close()
            text = ""
            try:
                text = arts.qemu_log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            if is_address_already_in_use(text):
                raise AddressAlreadyInUse(text)
            raise ImageBuildError(
                f"qemu exited {proc.returncode} immediately\n{text[-4000:]}"
            )
        return Machine(
            overlay,
            run_dir,
            ssh_port=ssh_port,
            vnc_port=vnc_port,
            identity=identity,
            user=guest.user.name,
            process=proc,
        )

    return retry_on_addr_in_use(attempt)


def _scp_to_guest(
    machine: Machine,
    source: Path,
    dest: str,
    *,
    run: RunFn | None = None,
) -> None:
    if machine.identity is None:
        raise ImageBuildError("identity is required for scp")
    argv = scp_command(
        port=machine.ssh_port,
        identity=machine.identity,
        source=str(source),
        destination=dest,
        user=machine.user,
    )
    runner = run or subprocess.run
    proc = runner(argv, check=False, capture_output=True, text=True)
    _append_log(
        machine.artifacts.root / "ssh.log",
        f"$ scp {source} -> {dest}\n[exit {proc.returncode}]\n"
        f"{proc.stdout}{proc.stderr}",
    )
    if proc.returncode != 0:
        raise ImageBuildError(f"scp failed: {proc.stderr or proc.stdout}")


def _scp_from_guest(
    machine: Machine,
    remote: str,
    dest: Path,
    *,
    run: RunFn | None = None,
) -> None:
    if machine.identity is None:
        raise ImageBuildError("identity is required for scp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = scp_download_command(
        port=machine.ssh_port,
        identity=machine.identity,
        remote_path=remote,
        local_path=dest,
        user=machine.user,
    )
    runner = run or subprocess.run
    proc = runner(argv, check=False, capture_output=True, text=True)
    _append_log(
        machine.artifacts.root / "ssh.log",
        f"$ scp {remote} -> {dest}\n[exit {proc.returncode}]\n"
        f"{proc.stdout}{proc.stderr}",
    )
    if proc.returncode != 0:
        raise ImageBuildError(
            f"scp download failed: {proc.stderr or proc.stdout}"
        )


def _install_symlink(link: Path, golden: Path) -> None:
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(golden.name)


def _write_provenance(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _assert_free_space(cache: Path, guest: Guest, download: Path | None) -> None:
    source_bytes = source_size_bytes(download) if download else UNKNOWN_SOURCE_BYTES
    needed = required_free_bytes(guest.disk_gb, source_bytes)
    try:
        free = shutil.disk_usage(cache).free
    except OSError as exc:
        raise ImageBuildError(f"could not measure free space on {cache}: {exc}") from exc
    if free < needed:
        need_gib = needed / GIB
        free_gib = free / GIB
        raise ImageBuildError(
            f"not enough free space on {cache}: "
            f"{free_gib:.1f} GiB free, need {need_gib:.1f} GiB "
            f"(2 * disk_gb + cloudimg + 10)"
        )


def _pubkey_for(ssh_key: Path) -> str:
    pub = ssh_key.with_name(ssh_key.name + ".pub")
    if not pub.is_file():
        raise ImageBuildError(f"missing SSH public key {pub}")
    return pub.read_text(encoding="utf-8")


def build_live(
    guest: Guest,
    cache: Path,
    *,
    host: CheckHostResult,
    run: RunFn | None = None,
    popen: PopenFn | None = None,
) -> Path:
    """Fetch, boot, setup, and install the content-addressed golden."""
    if host.ssh_key is None:
        raise ImageBuildError("check-host ok but SSH key missing")
    if guest.firmware == "uefi" and host.ovmf_code is None:
        raise ImageBuildError("check-host ok but OVMF code missing")

    images = artifacts.images_dir(cache)
    images.mkdir(parents=True, exist_ok=True)
    downloads = artifacts.downloads_dir(cache)
    downloads.mkdir(parents=True, exist_ok=True)

    existing_download = downloads / guest.source_filename()
    _assert_free_space(
        cache, guest, existing_download if existing_download.is_file() else None
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifacts.runs_dir(cache) / f"{stamp}-{guest.id}-build"
    run_dir.mkdir(parents=True, exist_ok=True)
    working = run_dir / "working.qcow2"
    machine: Machine | None = None
    golden = golden_qcow2(guest, cache)

    iso_autoinstall = (
        guest.source_kind == "iso-autoinstall" or uses_iso_autoinstall(guest.id)
    )
    omarchy_version = ""

    try:
        blob = run_bootstrap(guest, cache, run=run)
        _assert_free_space(cache, guest, blob)
        install_iso: Path | None = None
        if iso_autoinstall:
            create_blank_qcow2(working, guest.disk_gb, run=run)
            install_iso = blob
        else:
            convert_and_resize(blob, working, guest.disk_gb, run=run)

        pubkey = _pubkey_for(host.ssh_key)
        if iso_autoinstall:
            cidata = write_omarchy_cidata_iso(
                run_dir / "cidata.iso",
                recipe_dir=guest.recipe_dir,
                pubkey=pubkey,
            )
        else:
            cidata = write_cidata_iso_from_recipe(
                run_dir / "cidata.iso",
                recipe_dir=guest.recipe_dir,
                pubkey=pubkey,
                instance_id=guest.id,
                hostname=guest.id,
            )

        ovmf_vars: Path | None = None
        ovmf_code: Path | None = None
        if guest.firmware == "uefi":
            ovmf_code = host.ovmf_code
            vars_template = find_ovmf_vars()
            if vars_template is None:
                searched = ", ".join(str(p) for p in ovmf_vars_candidates())
                raise ImageBuildError(
                    f"OVMF_VARS template not found (search order: {searched})"
                )
            arts = artifacts.RunArtifacts(run_dir)
            ovmf_vars = copy_uefi_vars(arts.ovmf_vars, template_vars=vars_template)

        machine = _spawn_qemu(
            guest=guest,
            overlay=working,
            run_dir=run_dir,
            ovmf_code=ovmf_code,
            ovmf_vars=ovmf_vars,
            cidata_iso=cidata,
            identity=host.ssh_key,
            popen=popen,
            install_iso=install_iso,
        )
        if iso_autoinstall:
            omarchy_version = wait_iso_autoinstall(
                machine,
                major=omarchy_major_for_guest(guest),
                timeout=guest.build_timeout_s,
                run=run,
            )
        else:
            wait_ssh(machine, timeout=guest.boot_timeout_s, run=run)
            wait_cloud_init(machine, run=run)

        for src in recipe_files_to_upload(guest):
            _scp_to_guest(machine, src, f"/tmp/{src.name}", run=run)
        _ssh_run(
            machine,
            setup_ssh_command(guest),
            timeout=guest.build_timeout_s,
            check=True,
            run=run,
        )

        inv_dir = run_dir / "inventory"
        inv_dir.mkdir(parents=True, exist_ok=True)
        inv_name = inventory_basename(guest)
        _scp_from_guest(
            machine, "/var/tmp/strata-inventory.txt", inv_dir / inv_name, run=run
        )
        _scp_from_guest(
            machine, "/var/tmp/strata-glibc.txt", inv_dir / "glibc.txt", run=run
        )
        _scp_from_guest(
            machine, "/var/tmp/strata-gtk.txt", inv_dir / "gtk.txt", run=run
        )
        if iso_autoinstall:
            try:
                _scp_from_guest(
                    machine,
                    "/var/tmp/strata-omarchy-version.txt",
                    inv_dir / "omarchy-version.txt",
                    run=run,
                )
            except ImageBuildError:
                pass

        path_used = machine.shutdown()
        log.info("shutdown via %s", path_used)
        if machine._proc is not None:
            try:
                machine._proc.wait(timeout=60)
            except Exception:
                machine.kill()
                if machine._proc is not None:
                    machine._proc.wait(timeout=15)
        machine = None

        if golden.exists() or golden.is_symlink():
            golden.unlink()
        shutil.move(str(working), str(golden))
        try:
            golden.chmod(0o444)
        except OSError:
            pass
        _install_symlink(golden_symlink(guest, cache), golden)
        if (
            iso_autoinstall
            and guest.firmware == "uefi"
            and ovmf_vars is not None
            and ovmf_vars.is_file()
        ):
            dest_vars = golden_vars_fd(guest, cache)
            shutil.copyfile(ovmf_vars, dest_vars)

        inventory = (inv_dir / inv_name).read_text(
            encoding="utf-8", errors="replace"
        )
        glibc = (inv_dir / "glibc.txt").read_text(encoding="utf-8", errors="replace").strip()
        gtk = (inv_dir / "gtk.txt").read_text(encoding="utf-8", errors="replace").strip()
        payload = {
            "guest": guest.id,
            "golden": golden.name,
            "source": {
                "url": guest.source_url,
                "sha256": guest.source_sha256,
                "filename": guest.source_filename(),
            },
            "recipe_digest": guest.recipe_digest(),
            "golden_digest": guest.golden_digest(),
            "built_at": stamp,
            inventory_provenance_key(guest): inventory,
            "glibc": glibc,
            "gtk": gtk,
        }
        if iso_autoinstall:
            recorded = omarchy_version
            version_file = inv_dir / "omarchy-version.txt"
            if version_file.is_file():
                recorded = version_file.read_text(
                    encoding="utf-8", errors="replace"
                ).strip() or recorded
            payload["omarchy_version"] = recorded
            payload["vars"] = golden_vars_fd(guest, cache).name
        _write_provenance(provenance_path(guest, cache), payload)
        shutil.rmtree(run_dir, ignore_errors=True)
        return golden
    except Exception:
        if machine is not None:
            try:
                machine.shutdown()
            except Exception:
                machine.kill()
            if machine._proc is not None:
                try:
                    machine._proc.wait(timeout=15)
                except Exception:
                    machine.kill()
        log.exception("image-build live path failed; kept %s", run_dir)
        raise


def _format_minutes(seconds: int) -> str:
    minutes = max(1, (max(0, seconds) + 59) // 60)
    if minutes == 1:
        return "1 minute"
    return f"{minutes} minutes"


def build_duration_note(guest: Guest) -> str:
    """One-line expectation for a live golden build (stderr, not stdout)."""
    timeout = _format_minutes(guest.build_timeout_s)
    if guest.source_kind == "iso-autoinstall":
        typical = "20-60 minutes"
        what = "ISO autoinstall and setup"
    else:
        typical = "10-30 minutes"
        what = "first boot, package install, and setup"
    return (
        f"image-build: {guest.id} {what} typically takes {typical} "
        f"(timeout {timeout}). Quiet until done; SSH transcripts stay in "
        f"the run dir, not the terminal."
    )


def run_image_build(
    guest_id: str | None,
    *,
    force: bool = False,
    cache_dir: Path | None = None,
    check_host_fn: Callable[[], CheckHostResult] | None = None,
    run: RunFn | None = None,
    popen: PopenFn | None = None,
) -> int:
    """CLI body for ``image-build``. Never a generic ``not implemented`` stub."""
    if not guest_id:
        print(
            "image-build: guest id is required (e.g. ubuntu-2404)",
            file=sys.stderr,
        )
        print(
            "usage: python -m strataqemu image-build [--force] <guest>",
            file=sys.stderr,
        )
        return 2

    cache = cache_dir if cache_dir is not None else config.cache_dir()
    try:
        guest = load_guest(guest_id)
    except GuestError as exc:
        print(f"image-build: {exc}", file=sys.stderr)
        return 2

    golden = golden_qcow2(guest, cache)
    if golden.is_file() and not force:
        print(str(golden.resolve()))
        return 0

    host_fn = check_host_fn if check_host_fn is not None else check_host
    host = host_fn()
    if not host.ok:
        for err in host.errors:
            print(err, file=sys.stderr)
        return 1

    try:
        print(build_duration_note(guest), file=sys.stderr)
        installed = build_live(
            guest, cache, host=host, run=run, popen=popen
        )
    except Exception as exc:
        print(f"image-build: {exc}", file=sys.stderr)
        return 1
    print(str(installed.resolve()))
    return 0
