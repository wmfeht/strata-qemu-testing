"""Build or refresh a golden from its recipe.

Incremental: a content-addressed qcow2 matching the current recipe+source
digest is printed and the command exits 0. ``--force`` rebuilds.
"""

from __future__ import annotations

import json
import logging
import os
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
from strataqemu.cloudinit import write_cidata_iso_from_recipe
from strataqemu.guest import Guest, GuestError, load_guest
from strataqemu.overlay import copy_uefi_vars
from strataqemu.ports import (
    AddressAlreadyInUse,
    allocate_ssh_port,
    allocate_vnc_port,
    is_address_already_in_use,
    retry_on_addr_in_use,
)
from strataqemu.qemu import Machine, build_qemu_argv
from strataqemu.ssh import scp_command, scp_download_command

log = logging.getLogger("strataqemu")

# Noble GDM / cloud-init first boot can exceed boot_timeout_s; wait cloud-init
# with a separate budget, then setup.sh uses build_timeout_s.
CLOUD_INIT_TIMEOUT_S = 600
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


class ImageBuildError(RuntimeError):
    """Live image-build failure."""


def ovmf_vars_candidates(
    share_roots: Sequence[Path] | None = None,
) -> list[Path]:
    roots = tuple(share_roots) if share_roots else DEFAULT_FIRMWARE_SHARE_ROOTS
    return [root / rel for root in roots for rel in OVMF_VARS_RELATIVE]


def find_ovmf_vars(share_roots: Sequence[Path] | None = None) -> Path | None:
    return find_ovmf_code(ovmf_vars_candidates(share_roots))


def golden_filename(guest: Guest) -> str:
    return f"{guest.id}-{guest.golden_digest()}.qcow2"


def golden_qcow2(guest: Guest, cache: Path) -> Path:
    return artifacts.images_dir(cache) / golden_filename(guest)


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


def wait_ssh(
    machine: Machine,
    *,
    timeout: float,
    run: RunFn | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
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
            time.sleep(2)
            continue
        if proc.returncode == 0:
            return
        last = (proc.stderr or proc.stdout or "").strip()
        time.sleep(2)
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
        )
        log.info("qemu argv: %s", " ".join(argv))
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

    try:
        blob = run_bootstrap(guest, cache, run=run)
        _assert_free_space(cache, guest, blob)
        convert_and_resize(blob, working, guest.disk_gb, run=run)

        pubkey = _pubkey_for(host.ssh_key)
        cidata = write_cidata_iso_from_recipe(
            run_dir / "cidata.iso",
            recipe_dir=guest.recipe_dir,
            pubkey=pubkey,
            instance_id=guest.id,
            hostname=guest.id,
        )

        ovmf_vars: Path | None = None
        ovmf_code = host.ovmf_code
        if guest.firmware == "uefi":
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
        )
        wait_ssh(machine, timeout=guest.boot_timeout_s, run=run)
        wait_cloud_init(machine, run=run)

        setup = guest.recipe_dir / "setup.sh"
        _scp_to_guest(machine, setup, "/tmp/setup.sh", run=run)
        env_prefix = ""
        if guest.packages.snapshot_url:
            env_prefix = f"SNAPSHOT_URL={guest.packages.snapshot_url} "
        _ssh_run(
            machine,
            f"{env_prefix}sudo -n bash /tmp/setup.sh",
            timeout=guest.build_timeout_s,
            check=True,
            run=run,
        )

        inv_dir = run_dir / "inventory"
        inv_dir.mkdir(parents=True, exist_ok=True)
        _scp_from_guest(
            machine, "/var/tmp/strata-inventory.txt", inv_dir / "dpkg.txt", run=run
        )
        _scp_from_guest(
            machine, "/var/tmp/strata-glibc.txt", inv_dir / "glibc.txt", run=run
        )
        _scp_from_guest(
            machine, "/var/tmp/strata-gtk.txt", inv_dir / "gtk.txt", run=run
        )

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

        dpkg = (inv_dir / "dpkg.txt").read_text(encoding="utf-8", errors="replace")
        glibc = (inv_dir / "glibc.txt").read_text(encoding="utf-8", errors="replace").strip()
        gtk = (inv_dir / "gtk.txt").read_text(encoding="utf-8", errors="replace").strip()
        payload = {
            "guest": guest.id,
            "golden": golden.name,
            "source": {
                "url": guest.source_url,
                "sha256": guest.source_sha256,
            },
            "recipe_digest": guest.recipe_digest(),
            "golden_digest": guest.golden_digest(),
            "built_at": stamp,
            "dpkg": dpkg,
            "glibc": glibc,
            "gtk": gtk,
        }
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
        installed = build_live(
            guest, cache, host=host, run=run, popen=popen
        )
    except Exception as exc:
        print(f"image-build: {exc}", file=sys.stderr)
        return 1
    print(str(installed.resolve()))
    return 0
