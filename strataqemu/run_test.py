"""``run-test --session-only`` and ``vm-run``: throwaway overlay of a golden.

Missing golden fails closed with the image-build-first message and never
invokes ``image-build``. ``--session-only`` is session + screenshot only.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from strataqemu import artifacts, config
from strataqemu.cli import CheckHostResult, check_host
from strataqemu.guest import Guest, GuestError, load_guest
from strataqemu.image_build import (
    ImageBuildError,
    find_ovmf_vars,
    golden_qcow2,
    ovmf_vars_candidates,
    wait_ssh,
)
from strataqemu.overlay import copy_uefi_vars, create_overlay
from strataqemu.ports import (
    AddressAlreadyInUse,
    allocate_ssh_port,
    allocate_vnc_port,
    is_address_already_in_use,
    retry_on_addr_in_use,
)
from strataqemu.qemu import Machine, build_qemu_argv
from strataqemu.tests_spec import (
    INSTALL_FROM_FAIL_CLOSED,
    SESSION_TIMEOUT_S,
    SessionSmokeError,
    compositor_process_name,
    missing_golden_message,
    run_install_from_release_steps,
    run_session_only_steps,
    supports_install_from_release,
)

log = logging.getLogger("strataqemu")

RunFn = Callable[..., subprocess.CompletedProcess]
PopenFn = Callable[..., subprocess.Popen]
CreateOverlayFn = Callable[..., Path]
SETTLE_S = 0.4


class RunTestError(RuntimeError):
    """Fail-closed run-test / vm-run error."""


def golden_vars_fd(guest: Guest, cache: Path) -> Path:
    return artifacts.images_dir(cache) / f"{guest.id}.vars.fd"


def require_golden(guest: Guest, cache: Path) -> Path:
    """Return the content-addressed golden or raise ``RunTestError``."""
    path = golden_qcow2(guest, cache)
    if path.is_file():
        return path
    raise RunTestError(missing_golden_message(guest.id))


def choose_graphical_ui(help_text: str | None = None) -> str:
    """``gtk`` if QEMU has it, else ``sdl``. Does not boot a VM."""
    text = help_text if help_text is not None else _qemu_display_help()
    lowered = text.lower()
    has_gtk = False
    has_sdl = False
    for raw in lowered.splitlines():
        token = raw.strip().split(None, 1)
        if not token:
            continue
        name = token[0].strip(",")
        if name == "gtk":
            has_gtk = True
        elif name == "sdl":
            has_sdl = True
    if "gtk" in lowered and not has_sdl and not has_gtk:
        has_gtk = True
    if has_gtk:
        return "gtk"
    if has_sdl:
        return "sdl"
    return "gtk"


def _qemu_display_help(qemu_binary: str = "qemu-system-x86_64") -> str:
    try:
        proc = subprocess.run(
            [qemu_binary, "-display", "help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return f"{proc.stdout}{proc.stderr}"


def run_test_qemu_argv(
    *,
    overlay: Path | str,
    run_dir: Path | str,
    ssh_port: int,
    vnc_port: int,
    cpus: int,
    memory_mib: int,
    ovmf_code: Path | str | None = None,
    ovmf_vars: Path | str | None = None,
) -> list[str]:
    """Frozen test display stack on a throwaway overlay (``cache=unsafe``)."""
    return build_qemu_argv(
        overlay=overlay,
        run_dir=run_dir,
        ssh_port=ssh_port,
        vnc_port=vnc_port,
        cpus=cpus,
        memory_mib=memory_mib,
        graphical=False,
        disk_cache="unsafe",
        ovmf_code=ovmf_code,
        ovmf_vars=ovmf_vars,
    )


def vm_run_qemu_argv(
    *,
    overlay: Path | str,
    run_dir: Path | str,
    ssh_port: int,
    vnc_port: int | None = None,
    cpus: int,
    memory_mib: int,
    graphical: bool = False,
    graphical_ui: str = "gtk",
    ovmf_code: Path | str | None = None,
    ovmf_vars: Path | str | None = None,
) -> list[str]:
    """Throwaway overlay argv. ``--graphical`` is virtio-vga-gl + gtk/sdl, no VNC."""
    return build_qemu_argv(
        overlay=overlay,
        run_dir=run_dir,
        ssh_port=ssh_port,
        vnc_port=None if graphical else vnc_port,
        cpus=cpus,
        memory_mib=memory_mib,
        graphical=graphical,
        graphical_ui=graphical_ui,
        disk_cache="unsafe",
        ovmf_code=ovmf_code,
        ovmf_vars=ovmf_vars,
    )


def _write_result(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _copy_run_vars(guest: Guest, cache: Path, dest: Path) -> Path:
    golden_vars = golden_vars_fd(guest, cache)
    template = find_ovmf_vars()
    if golden_vars.is_file():
        src_template = template if template is not None else golden_vars
        return copy_uefi_vars(
            dest, template_vars=src_template, golden_vars=golden_vars
        )
    if template is None:
        searched = ", ".join(str(p) for p in ovmf_vars_candidates())
        raise RunTestError(
            f"OVMF_VARS template not found (search order: {searched})"
        )
    return copy_uefi_vars(dest, template_vars=template)


def _spawn_overlay_vm(
    *,
    guest: Guest,
    overlay: Path,
    run_dir: Path,
    ovmf_code: Path | None,
    ovmf_vars: Path | None,
    identity: Path,
    graphical: bool,
    graphical_ui: str,
    popen: PopenFn | None = None,
    settle_s: float = SETTLE_S,
    inherit_stdio: bool = False,
) -> Machine:
    arts = artifacts.RunArtifacts(run_dir)
    launcher = popen or subprocess.Popen

    def attempt() -> Machine:
        ssh_port = allocate_ssh_port()
        vnc_port: int | None = None if graphical else allocate_vnc_port()
        arts.ssh_port_file.write_text(f"{ssh_port}\n", encoding="utf-8")
        if graphical:
            argv = vm_run_qemu_argv(
                overlay=overlay,
                run_dir=run_dir,
                ssh_port=ssh_port,
                vnc_port=None,
                cpus=guest.cpus,
                memory_mib=guest.memory_mib,
                graphical=True,
                graphical_ui=graphical_ui,
                ovmf_code=ovmf_code,
                ovmf_vars=ovmf_vars,
            )
        else:
            assert vnc_port is not None
            argv = run_test_qemu_argv(
                overlay=overlay,
                run_dir=run_dir,
                ssh_port=ssh_port,
                vnc_port=vnc_port,
                cpus=guest.cpus,
                memory_mib=guest.memory_mib,
                ovmf_code=ovmf_code,
                ovmf_vars=ovmf_vars,
            )
        log.info("qemu argv: %s", " ".join(argv))
        popen_kwargs: dict = {}
        logf = None
        if inherit_stdio:
            popen_kwargs["stdin"] = None
        else:
            logf = arts.qemu_log.open("ab")
            popen_kwargs["stdout"] = logf
            popen_kwargs["stderr"] = subprocess.STDOUT
            popen_kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = launcher(argv, **popen_kwargs)
        except Exception:
            if logf is not None:
                logf.close()
            raise
        if settle_s > 0:
            time.sleep(settle_s)
        if proc.poll() is not None:
            if logf is not None:
                logf.close()
            text = ""
            try:
                text = arts.qemu_log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            if is_address_already_in_use(text):
                raise AddressAlreadyInUse(text)
            raise RunTestError(
                f"qemu exited {proc.returncode} immediately\n{text[-4000:]}"
            )
        return Machine(
            overlay,
            run_dir,
            ssh_port=ssh_port,
            vnc_port=vnc_port,
            graphical=graphical,
            identity=identity,
            user=guest.user.name,
            process=proc,
        )

    return retry_on_addr_in_use(attempt)


def _shutdown_machine(machine: Machine) -> None:
    try:
        machine.shutdown()
    except Exception:
        machine.kill()
    if machine._proc is not None:
        try:
            machine._proc.wait(timeout=15)
        except Exception:
            machine.kill()


def discard_throwaway_disks(run_dir: Path) -> None:
    """Drop overlay/vars/sockets. Keep screenshot, result.json, and logs."""
    arts = artifacts.RunArtifacts(run_dir)
    for path in arts.throwaway_paths():
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError as exc:
            log.debug("could not remove %s: %s", path, exc)


def _finish_run_dir(run_dir: Path, *, keep: bool, failed: bool) -> None:
    """Keep evidence. Delete the overlay on success unless ``--keep``."""
    if keep or failed:
        print(f"kept run dir: {run_dir}", file=sys.stderr)
        return
    discard_throwaway_disks(run_dir)
    print(f"run dir: {run_dir}")


def _prepare_overlay(
    guest: Guest,
    cache: Path,
    run_dir: Path,
    *,
    create_overlay_fn: CreateOverlayFn | None = None,
) -> tuple[Path, Path | None]:
    golden = require_golden(guest, cache)
    arts = artifacts.RunArtifacts(run_dir)
    maker = create_overlay_fn or create_overlay
    overlay = maker(golden, arts.overlay)
    ovmf_vars: Path | None = None
    if guest.firmware == "uefi":
        ovmf_vars = _copy_run_vars(guest, cache, arts.ovmf_vars)
    return overlay, ovmf_vars


def _load_or_usage(guest_id: str | None, command: str) -> Guest | int:
    if not guest_id:
        print(
            f"{command}: guest id is required (e.g. ubuntu-2404)",
            file=sys.stderr,
        )
        if command == "vm-run":
            print(
                "usage: python -m strataqemu vm-run [--graphical] [--keep] <guest>",
                file=sys.stderr,
            )
        else:
            print(
                "usage: python -m strataqemu run-test "
                "[--session-only | --install-from release] [--keep] <guest>",
                file=sys.stderr,
            )
        return 2
    try:
        return load_guest(guest_id)
    except GuestError as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 2


def run_run_test(
    guest_id: str | None,
    *,
    session_only: bool = False,
    install_from: str | None = None,
    keep: bool = False,
    cache_dir: Path | None = None,
    check_host_fn: Callable[[], CheckHostResult] | None = None,
    run: RunFn | None = None,
    popen: PopenFn | None = None,
    create_overlay_fn: CreateOverlayFn | None = None,
    settle_s: float = SETTLE_S,
) -> int:
    """CLI body for ``run-test``. Not a stub. Never calls ``image-build``."""
    loaded = _load_or_usage(guest_id, "run-test")
    if isinstance(loaded, int):
        return loaded
    guest = loaded

    install_release = (
        install_from == "release"
        and supports_install_from_release(guest)
        and not session_only
    )
    if install_from and not session_only and not install_release:
        print(INSTALL_FROM_FAIL_CLOSED, file=sys.stderr)
        return 2
    if not session_only and not install_release:
        print(
            "run-test: pass --session-only "
            "(install / version / window-after-install are later PRs)",
            file=sys.stderr,
        )
        return 2

    cache = cache_dir if cache_dir is not None else config.cache_dir()
    try:
        golden = require_golden(guest, cache)
    except RunTestError as exc:
        print(f"run-test: {exc}", file=sys.stderr)
        return 1

    host_fn = check_host_fn if check_host_fn is not None else check_host
    host = host_fn()
    if not host.ok:
        for err in host.errors:
            print(err, file=sys.stderr)
        return 1
    if host.ssh_key is None:
        print("run-test: check-host ok but SSH key missing", file=sys.stderr)
        return 1
    if guest.firmware == "uefi" and host.ovmf_code is None:
        print("run-test: check-host ok but OVMF code missing", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifacts.runs_dir(cache) / f"{stamp}-{guest.id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    arts = artifacts.RunArtifacts(run_dir)
    machine: Machine | None = None
    failed = False
    started = time.monotonic()
    recorded: list[str] = []
    result: dict = {
        "guest": guest.id,
        "golden": golden.name,
        "ok": False,
        "steps": [],
    }

    try:
        overlay, ovmf_vars = _prepare_overlay(
            guest, cache, run_dir, create_overlay_fn=create_overlay_fn
        )
        ovmf_code = host.ovmf_code if guest.firmware == "uefi" else None
        machine = _spawn_overlay_vm(
            guest=guest,
            overlay=overlay,
            run_dir=run_dir,
            ovmf_code=ovmf_code,
            ovmf_vars=ovmf_vars,
            identity=host.ssh_key,
            graphical=False,
            graphical_ui="gtk",
            popen=popen,
            settle_s=settle_s,
            inherit_stdio=False,
        )
        wait_ssh(machine, timeout=guest.boot_timeout_s, run=run)
        shot = run_dir / "screenshot.png"
        qmp_path = run_dir / "qmp-session.png"
        extras: dict = {}
        if install_release:
            steps, extras = run_install_from_release_steps(
                machine,
                guest=guest,
                screenshot_dest=shot,
                qmp_dest=qmp_path,
                session_timeout=SESSION_TIMEOUT_S,
                run=run,
                commands=recorded,
            )
        else:
            steps = run_session_only_steps(
                machine,
                compositor=compositor_process_name(guest),
                screenshot_dest=shot,
                qmp_dest=qmp_path,
                session_timeout=SESSION_TIMEOUT_S,
                run=run,
                commands=recorded,
            )
        result.update(
            {
                "ok": True,
                "steps": steps,
                "screenshot": str(shot),
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        result.update(extras)
        _write_result(arts.result_json, result)
        if install_release:
            print(f"run-test: ok ({guest.id})")
        else:
            print(f"run-test: session ok ({guest.id})")
        print(f"screenshot: {shot}")
        return 0
    except KeyboardInterrupt:
        failed = True
        result["error"] = "interrupted"
        _write_result(arts.result_json, result)
        print("run-test: interrupted", file=sys.stderr)
        return 130
    except (
        RunTestError,
        SessionSmokeError,
        ImageBuildError,
        TimeoutError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        failed = True
        result["error"] = str(exc)
        result["commands"] = recorded
        _write_result(arts.result_json, result)
        log.exception("run-test failed")
        print(f"run-test: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        failed = True
        result["error"] = str(exc)
        _write_result(arts.result_json, result)
        log.exception("run-test failed")
        print(f"run-test: {exc}", file=sys.stderr)
        return 1
    finally:
        if machine is not None:
            _shutdown_machine(machine)
        _finish_run_dir(run_dir, keep=keep, failed=failed)


def run_vm_run(
    guest_id: str | None,
    *,
    graphical: bool = False,
    keep: bool = False,
    cache_dir: Path | None = None,
    check_host_fn: Callable[[], CheckHostResult] | None = None,
    popen: PopenFn | None = None,
    create_overlay_fn: CreateOverlayFn | None = None,
    settle_s: float = SETTLE_S,
    graphical_ui: str | None = None,
    wait: bool = True,
) -> int:
    """CLI body for ``vm-run``. Throwaway overlay; no ``--maintain``."""
    loaded = _load_or_usage(guest_id, "vm-run")
    if isinstance(loaded, int):
        return loaded
    guest = loaded

    cache = cache_dir if cache_dir is not None else config.cache_dir()
    try:
        golden = require_golden(guest, cache)
    except RunTestError as exc:
        print(f"vm-run: {exc}", file=sys.stderr)
        return 1
    del golden

    host_fn = check_host_fn if check_host_fn is not None else check_host
    host = host_fn()
    if not host.ok:
        for err in host.errors:
            print(err, file=sys.stderr)
        return 1
    if host.ssh_key is None:
        print("vm-run: check-host ok but SSH key missing", file=sys.stderr)
        return 1
    if guest.firmware == "uefi" and host.ovmf_code is None:
        print("vm-run: check-host ok but OVMF code missing", file=sys.stderr)
        return 1

    ui = graphical_ui
    if graphical and ui is None:
        ui = choose_graphical_ui()
    if ui is None:
        ui = "gtk"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifacts.runs_dir(cache) / f"{stamp}-{guest.id}-vm"
    run_dir.mkdir(parents=True, exist_ok=True)
    machine: Machine | None = None
    failed = False

    try:
        overlay, ovmf_vars = _prepare_overlay(
            guest, cache, run_dir, create_overlay_fn=create_overlay_fn
        )
        ovmf_code = host.ovmf_code if guest.firmware == "uefi" else None
        machine = _spawn_overlay_vm(
            guest=guest,
            overlay=overlay,
            run_dir=run_dir,
            ovmf_code=ovmf_code,
            ovmf_vars=ovmf_vars,
            identity=host.ssh_key,
            graphical=graphical,
            graphical_ui=ui,
            popen=popen,
            settle_s=settle_s,
            inherit_stdio=True,
        )
        print(f"vm-run: {guest.id} ssh_port={machine.ssh_port}")
        if wait and machine._proc is not None:
            machine._proc.wait()
        return 0
    except KeyboardInterrupt:
        failed = True
        print("vm-run: interrupted", file=sys.stderr)
        return 130
    except (RunTestError, OSError, subprocess.CalledProcessError) as exc:
        failed = True
        print(f"vm-run: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        failed = True
        log.exception("vm-run failed")
        print(f"vm-run: {exc}", file=sys.stderr)
        return 1
    finally:
        if machine is not None:
            _shutdown_machine(machine)
        if not keep and not failed:
            shutil.rmtree(run_dir, ignore_errors=True)
        elif failed or keep:
            print(f"kept run dir: {run_dir}", file=sys.stderr)
