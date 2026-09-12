"""QEMU argv builder, qemu-ga/QMP helpers, and shutdown cascade.

Does not require ``Guest.load`` (PR 4). Callers pass the fields the argv needs.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from strataqemu.artifacts import RunArtifacts
from strataqemu.ssh import ssh_command, ssh_poweroff_command

log = logging.getLogger("strataqemu")

ISO_AUTOINSTALL_GUESTS = frozenset({"omarchy-4", "omarchy-3"})
CLOUD_INIT_SEED_GUESTS = frozenset({"arch", "ubuntu-2404", "fedora-workstation"})
QEMU_BINARY = "qemu-system-x86_64"
DEFAULT_CPUS = 4
DEFAULT_MEMORY_MIB = 8192
GUEST_AGENT_NAME = "org.qemu.guest_agent.0"


def uses_iso_autoinstall(guest_id: str) -> bool:
    """True for both Omarchy majors. Omitting omarchy-3 is a fail."""
    return guest_id in ISO_AUTOINSTALL_GUESTS


def uses_cloud_init_seed(guest_id: str) -> bool:
    return guest_id in CLOUD_INIT_SEED_GUESTS


def _abs(path: Path | str) -> str:
    return str(Path(path).resolve())


def build_qemu_argv(
    *,
    overlay: Path | str,
    run_dir: Path | str,
    ssh_port: int,
    vnc_port: int | None = None,
    cpus: int = DEFAULT_CPUS,
    memory_mib: int = DEFAULT_MEMORY_MIB,
    graphical: bool = False,
    graphical_ui: str = "gtk",
    disk_cache: str = "unsafe",
    ovmf_code: Path | str | None = None,
    ovmf_vars: Path | str | None = None,
    cidata_iso: Path | str | None = None,
    install_iso: Path | str | None = None,
    qemu_binary: str = QEMU_BINARY,
) -> list[str]:
    """Frozen test/image-build argv (and graphical / UEFI / cidata variants).

    ISO autoinstall requires ``cidata_iso`` as well as ``install_iso`` (both
    omarchy majors). Do not mix ``media=cdrom`` onto virtio-blk.
    """
    if graphical_ui not in ("gtk", "sdl"):
        raise ValueError(f"graphical_ui must be 'gtk' or 'sdl', not {graphical_ui!r}")
    if not graphical and vnc_port is None:
        raise ValueError("vnc_port is required unless graphical=True")
    if install_iso is not None and cidata_iso is None:
        raise ValueError(
            "ISO autoinstall requires cidata scsi-cd "
            "(omarchy-3 and omarchy-4)"
        )
    if (ovmf_code is None) ^ (ovmf_vars is None):
        raise ValueError("UEFI requires both ovmf_code and ovmf_vars")

    arts = RunArtifacts(Path(run_dir))
    overlay_path = _abs(overlay)
    argv: list[str] = [
        qemu_binary,
        "-machine",
        "q35,accel=kvm,usb=off",
        "-cpu",
        "host",
        "-smp",
        str(cpus),
        "-m",
        str(memory_mib),
        "-vga",
        "none",
    ]
    if graphical:
        argv.extend(
            [
                "-device",
                "virtio-vga-gl",
                "-display",
                f"{graphical_ui},gl=on",
            ]
        )
    else:
        argv.extend(
            [
                "-device",
                "virtio-gpu-gl-pci",
                "-display",
                "egl-headless,gl=on",
                "-vnc",
                f"127.0.0.1:{vnc_port}",
            ]
        )
    argv.extend(
        [
            "-drive",
            f"file={overlay_path},if=none,id=drive0,discard=unmap,cache={disk_cache}",
            "-device",
            "virtio-blk-pci,drive=drive0,bootindex=1",
            "-netdev",
            f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
            "-device",
            "virtio-net-pci,netdev=net0",
            "-serial",
            f"file:{_abs(arts.serial_log)}",
            "-qmp",
            f"unix:{_abs(arts.qmp_sock)},server,wait=off",
            "-chardev",
            f"socket,path={_abs(arts.qga_sock)},server=on,wait=off,id=qga",
            "-device",
            "virtio-serial-pci",
            "-device",
            f"virtserialport,chardev=qga,name={GUEST_AGENT_NAME}",
            "-usb",
            "-device",
            "usb-tablet",
        ]
    )
    if ovmf_code is not None and ovmf_vars is not None:
        argv.extend(
            [
                "-drive",
                f"if=pflash,format=raw,readonly=on,file={_abs(ovmf_code)}",
                "-drive",
                f"if=pflash,format=raw,file={_abs(ovmf_vars)}",
            ]
        )
    # One virtio-scsi-pci id=scsi0. ISO autoinstall includes the seed pair;
    # do not attach the cloud-guest snippet a second time.
    if install_iso is not None:
        argv.extend(
            [
                "-drive",
                f"file={_abs(install_iso)},media=cdrom,if=none,format=raw,id=cdrom0",
                "-device",
                "ide-cd,drive=cdrom0,bootindex=2",
                "-device",
                "virtio-scsi-pci,id=scsi0",
                "-drive",
                f"file={_abs(cidata_iso)},if=none,format=raw,readonly=on,id=cidata0",
                "-device",
                "scsi-cd,drive=cidata0,bus=scsi0.0",
            ]
        )
    elif cidata_iso is not None:
        argv.extend(
            [
                "-device",
                "virtio-scsi-pci,id=scsi0",
                "-drive",
                f"file={_abs(cidata_iso)},if=none,format=raw,readonly=on,id=cidata0",
                "-device",
                "scsi-cd,drive=cidata0,bus=scsi0.0",
            ]
        )
    return argv


def _recv_json_line(sock: socket.socket) -> dict:
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in buf:
            break
    if not buf:
        raise OSError("empty qemu json reply")
    line = bytes(buf).split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def _send_json(sock: socket.socket, payload: dict) -> None:
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def qga_guest_shutdown(socket_path: Path | str, *, timeout: float = 5.0) -> bool:
    """Send qemu-ga ``guest-shutdown``. Returns False if the socket/agent fails.

    A hang-up after the execute is success: the guest often goes away before
    a JSON return, and falling through to SSH then prints Connection refused.
    """
    path = str(Path(socket_path))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            _send_json(sock, {"execute": "guest-shutdown"})
            try:
                reply = _recv_json_line(sock)
            except (OSError, json.JSONDecodeError, TimeoutError):
                return True
    except (OSError, json.JSONDecodeError, TimeoutError) as exc:
        log.debug("qga guest-shutdown failed: %s", exc)
        return False
    if "error" in reply:
        log.debug("qga guest-shutdown error: %s", reply["error"])
        return False
    return True


def _qmp_execute(
    socket_path: Path | str,
    command: str,
    arguments: dict | None = None,
    *,
    timeout: float = 5.0,
) -> dict | None:
    """QMP handshake + one command. None on transport / greeting failure."""
    path = str(Path(socket_path))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            greeting = _recv_json_line(sock)
            if "QMP" not in greeting:
                log.debug("qmp greeting missing QMP key: %s", greeting)
                return None
            _send_json(sock, {"execute": "qmp_capabilities"})
            caps = _recv_json_line(sock)
            if "error" in caps:
                return None
            payload: dict = {"execute": command}
            if arguments:
                payload["arguments"] = arguments
            _send_json(sock, payload)
            return _recv_json_line(sock)
    except (OSError, json.JSONDecodeError, TimeoutError) as exc:
        log.debug("qmp %s failed: %s", command, exc)
        return None


def qmp_system_powerdown(socket_path: Path | str, *, timeout: float = 5.0) -> bool:
    """QMP ``system_powerdown`` (ACPI power-button). Handshake included."""
    reply = _qmp_execute(socket_path, "system_powerdown", timeout=timeout)
    if reply is None:
        return False
    if "error" in reply:
        log.debug("qmp system_powerdown error: %s", reply["error"])
        return False
    return True


def qmp_screendump(
    socket_path: Path | str,
    dest: Path | str,
    *,
    timeout: float = 5.0,
) -> bool:
    """Best-effort QMP ``screendump``. False on ``no surface`` or transport failure.

    Must not be used as the primary screenshot oracle; a ``no surface`` error
    must not fail the caller.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    reply = _qmp_execute(
        socket_path,
        "screendump",
        {"filename": str(dest_path.resolve())},
        timeout=timeout,
    )
    if reply is None:
        return False
    if "error" in reply:
        log.debug("qmp screendump error: %s", reply["error"])
        return False
    try:
        return dest_path.is_file() and dest_path.stat().st_size > 0
    except OSError:
        return False


@dataclass
class ShutdownHooks:
    """Injectable transports so unittest never needs a guest."""

    guest_shutdown: Callable[[], bool]
    ssh_poweroff: Callable[[], bool]
    acpi_power_button: Callable[[], bool]
    kill: Callable[[], None]


def run_shutdown(hooks: ShutdownHooks) -> str:
    """qemu-ga ``guest-shutdown``, then SSH poweroff, then ACPI, then ``kill()``.

    A successful earlier path does not invoke later ones (including kill).
    Returns the path that ran: ``qga``, ``ssh``, ``acpi``, or ``kill``.
    """
    if hooks.guest_shutdown():
        return "qga"
    if hooks.ssh_poweroff():
        return "ssh"
    if hooks.acpi_power_button():
        return "acpi"
    hooks.kill()
    return "kill"


class Machine:
    """VM handle. ``start()`` (spawn QEMU) is later PRs; shutdown/kill/ssh are here."""

    def __init__(
        self,
        overlay: Path | str,
        run_dir: Path | str,
        *,
        ssh_port: int,
        vnc_port: int | None = None,
        graphical: bool = False,
        identity: Path | str | None = None,
        user: str = "tester",
        process: subprocess.Popen | None = None,
    ) -> None:
        self.overlay = Path(overlay)
        self.artifacts = RunArtifacts(Path(run_dir))
        self.ssh_port = ssh_port
        self.vnc_port = vnc_port
        self.graphical = graphical
        self.identity = Path(identity) if identity is not None else None
        self.user = user
        self._proc = process

    def argv(self, **kwargs) -> list[str]:
        return build_qemu_argv(
            overlay=self.overlay,
            run_dir=self.artifacts.root,
            ssh_port=self.ssh_port,
            vnc_port=self.vnc_port,
            graphical=self.graphical,
            **kwargs,
        )

    def ssh(self, command: str, *, pty: bool = False) -> list[str]:
        if self.identity is None:
            raise ValueError("identity is required for ssh")
        return ssh_command(
            port=self.ssh_port,
            identity=self.identity,
            user=self.user,
            remote_command=command,
            pty=pty,
        )

    def kill(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.kill()

    def shutdown(
        self,
        timeout: float = 60,
        *,
        hooks: ShutdownHooks | None = None,
    ) -> str:
        del timeout  # used by wait loops in later PRs; cascade itself is sequential
        if hooks is None:
            hooks = ShutdownHooks(
                guest_shutdown=lambda: qga_guest_shutdown(self.artifacts.qga_sock),
                ssh_poweroff=self._ssh_poweroff,
                acpi_power_button=lambda: qmp_system_powerdown(
                    self.artifacts.qmp_sock
                ),
                kill=self.kill,
            )
        return run_shutdown(hooks)

    def _ssh_poweroff(self) -> bool:
        if self.identity is None:
            return False
        argv = ssh_poweroff_command(
            port=self.ssh_port,
            identity=self.identity,
            user=self.user,
        )
        try:
            proc = subprocess.run(
                argv,
                check=False,
                timeout=15,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        # Guest going away often yields 255 (connection closed).
        return proc.returncode in (0, 255)
