"""Session and GNOME desktop-oracle steps for ``run-test``.

Injectable SSH/run-dir so host unittests never spawn QEMU. ``--session-only``
runs session + in-guest screenshot; it does not gtk-launch Strata or wait on
the application bus name.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from strataqemu.guest import Guest
from strataqemu.qemu import Machine, qmp_screendump
from strataqemu.ssh import scp_command, scp_download_command

log = logging.getLogger("strataqemu")

RunFn = Callable[..., subprocess.CompletedProcess]

SESSION_TIMEOUT_S = 60
GUEST_SCREENSHOT_REMOTE = "/tmp/strata-window.png"
SCREENSHOT_TOOL_MISSING = "screenshot tool missing; rebuild the golden"
STRATA_BUS_NAME = "io.github.lgse.Strata"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 256
SESSION_ONLY_STEPS = ("session", "screenshot")
# Substrings that must not appear in --session-only guest commands.
SESSION_ONLY_FORBIDDEN = (
    "install.sh",
    "strata --version",
    "gtk-launch",
    "NameHasOwner",
    STRATA_BUS_NAME,
)
GDBUS_NAME_HAS_OWNER_ARGV = (
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.freedesktop.DBus",
    "--object-path",
    "/org/freedesktop/DBus",
    "--method",
    "org.freedesktop.DBus.NameHasOwner",
    STRATA_BUS_NAME,
)


class SessionSmokeError(RuntimeError):
    """Fail-closed session / screenshot / GNOME oracle error."""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def guest_tests_dir() -> Path:
    return repo_root() / "guest-tests"


def smoke_session_script() -> Path:
    return guest_tests_dir() / "smoke-session.sh"


def smoke_desktop_script() -> Path:
    return guest_tests_dir() / "smoke-desktop.sh"


def missing_golden_message(guest_id: str) -> str:
    return f"run `mise run image-build -- {guest_id}` first"


def compositor_process_name(guest: Guest | str) -> str:
    """Process name for the session compositor check.

    ``ubuntu-2404`` (and GNOME/Mutter recipes) use ``gnome-shell``.
    """
    if isinstance(guest, Guest):
        guest_id = guest.id
        kind = guest.session.kind.lower()
        compositor = guest.session.compositor.lower()
    else:
        guest_id = guest
        kind = ""
        compositor = ""
    if guest_id == "ubuntu-2404" or kind == "gnome" or compositor == "mutter":
        return "gnome-shell"
    if compositor == "hyprland" or kind == "hyprland":
        return "Hyprland"
    return compositor or "gnome-shell"


def parse_session_exports(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines from ``smoke-session.sh`` stdout."""
    wanted = {
        "SESSION_ID",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
    }
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in wanted:
            env[key] = value
    return env


def session_env_from_exports(exports: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
    ):
        if key in exports and exports[key]:
            env[key] = exports[key]
    return env


def env_prefix(env: Mapping[str, str]) -> str:
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())


def parse_name_has_owner(output: str) -> bool:
    """Parse ``gdbus … NameHasOwner`` output (``(true,)`` / ``(false,)``)."""
    blob = output.lower()
    if "true" in blob:
        return True
    if "false" in blob:
        return False
    raise SessionSmokeError(f"unparseable NameHasOwner reply: {output!r}")


def screenshot_tool_missing(which_returncode: int) -> bool:
    return which_returncode != 0


def gdbus_name_has_owner_command() -> str:
    return " ".join(shlex.quote(part) for part in GDBUS_NAME_HAS_OWNER_ARGV)


def session_only_step_names() -> tuple[str, ...]:
    return SESSION_ONLY_STEPS


def command_is_forbidden_for_session_only(command: str) -> bool:
    return any(token in command for token in SESSION_ONLY_FORBIDDEN)


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def ssh_run(
    machine: Machine,
    command: str,
    *,
    timeout: float = 120,
    check: bool = False,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if commands is not None:
        commands.append(command)
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
        raise SessionSmokeError(
            f"ssh command failed ({proc.returncode}): {command}\n"
            f"{proc.stdout}{proc.stderr}"
        )
    return proc


def scp_to_guest(
    machine: Machine,
    source: Path,
    dest: str,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    if machine.identity is None:
        raise SessionSmokeError("identity is required for scp")
    note = f"scp {source} {dest}"
    if commands is not None:
        commands.append(note)
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
        f"$ {note}\n[exit {proc.returncode}]\n{proc.stdout}{proc.stderr}",
    )
    if proc.returncode != 0:
        raise SessionSmokeError(f"scp failed: {proc.stderr or proc.stdout}")


def scp_from_guest(
    machine: Machine,
    remote: str,
    dest: Path,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> None:
    if machine.identity is None:
        raise SessionSmokeError("identity is required for scp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    note = f"scp {remote} {dest}"
    if commands is not None:
        commands.append(note)
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
        f"$ {note}\n[exit {proc.returncode}]\n{proc.stdout}{proc.stderr}",
    )
    if proc.returncode != 0:
        raise SessionSmokeError(
            f"scp download failed: {proc.stderr or proc.stdout}"
        )


def run_session_step(
    machine: Machine,
    *,
    compositor: str,
    timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    script: Path | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, str]:
    """Upload and run ``smoke-session.sh`` until it passes or ``timeout``."""
    script_path = script if script is not None else smoke_session_script()
    if not script_path.is_file():
        raise SessionSmokeError(f"missing session smoke script {script_path}")
    scp_to_guest(
        machine, script_path, "/tmp/smoke-session.sh", run=run, commands=commands
    )
    remote = (
        f"SMOKE_COMPOSITOR={shlex.quote(compositor)} bash /tmp/smoke-session.sh"
    )
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        proc = ssh_run(
            machine, remote, timeout=30, run=run, commands=commands
        )
        blob = f"{proc.stdout}{proc.stderr}"
        if proc.returncode == 0:
            exports = parse_session_exports(proc.stdout)
            if "WAYLAND_DISPLAY" not in exports:
                raise SessionSmokeError(
                    f"session smoke produced no WAYLAND_DISPLAY: {blob}"
                )
            return exports
        if "x11" in blob.lower():
            raise SessionSmokeError(blob.strip() or "Type=x11 is a hard fail")
        last = blob.strip() or f"exit {proc.returncode}"
        nap(2)
    raise TimeoutError(f"session smoke failed in {timeout}s: {last}")


def capture_guest_screenshot(
    machine: Machine,
    env: Mapping[str, str],
    dest: Path,
    *,
    run: RunFn | None = None,
    commands: list[str] | None = None,
) -> Path:
    """In-guest ``gnome-screenshot``. Missing binary is a golden bug."""
    which = ssh_run(
        machine,
        "command -v gnome-screenshot",
        timeout=15,
        run=run,
        commands=commands,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
    prefix = env_prefix(env)
    cmd = f"{prefix} gnome-screenshot -f {shlex.quote(GUEST_SCREENSHOT_REMOTE)}"
    ssh_run(
        machine, cmd, timeout=60, check=True, run=run, commands=commands
    )
    scp_from_guest(
        machine, GUEST_SCREENSHOT_REMOTE, dest, run=run, commands=commands
    )
    data = dest.read_bytes() if dest.is_file() else b""
    if len(data) < MIN_PNG_BYTES or not data.startswith(PNG_MAGIC):
        raise SessionSmokeError(
            f"in-guest screenshot is empty or not a PNG ({dest}, {len(data)} bytes)"
        )
    return dest


def extra_qmp_screendump(machine: Machine, dest: Path) -> bool:
    """Best-effort QMP dump. ``no surface`` must not fail the step."""
    try:
        return qmp_screendump(machine.artifacts.qmp_sock, dest)
    except OSError as exc:
        log.debug("qmp screendump extra failed: %s", exc)
        return False


def wait_gnome_bus_name(
    machine: Machine,
    *,
    timeout: float = 45,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Wait until the session bus owns ``io.github.lgse.Strata``.

    Not used by ``--session-only`` (window-after-install is a later PR).
    """
    remote = gdbus_name_has_owner_command()
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        proc = ssh_run(
            machine, remote, timeout=15, run=run, commands=commands
        )
        blob = f"{proc.stdout}{proc.stderr}"
        try:
            if parse_name_has_owner(blob):
                return
        except SessionSmokeError:
            last = blob.strip() or f"exit {proc.returncode}"
        else:
            last = blob.strip() or "NameHasOwner false"
        nap(1)
    raise TimeoutError(
        f"bus name {STRATA_BUS_NAME} not owned in {timeout}s: {last}"
    )


def run_gnome_desktop_oracle(
    machine: Machine,
    *,
    run: RunFn | None = None,
    script: Path | None = None,
    commands: list[str] | None = None,
) -> None:
    """Upload and run GNOME ``smoke-desktop.sh`` (bus name + screenshot)."""
    script_path = script if script is not None else smoke_desktop_script()
    if not script_path.is_file():
        raise SessionSmokeError(f"missing desktop smoke script {script_path}")
    scp_to_guest(
        machine, script_path, "/tmp/smoke-desktop.sh", run=run, commands=commands
    )
    proc = ssh_run(
        machine,
        "bash /tmp/smoke-desktop.sh",
        timeout=60,
        run=run,
        commands=commands,
    )
    blob = f"{proc.stdout}{proc.stderr}"
    if proc.returncode != 0:
        if SCREENSHOT_TOOL_MISSING in blob:
            raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
        raise SessionSmokeError(blob.strip() or "desktop oracle failed")


def run_session_only_steps(
    machine: Machine,
    *,
    compositor: str,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """Session + screenshot only. Does not launch Strata or wait on its bus."""
    recorded = commands if commands is not None else []
    steps: list[dict] = []
    started = time.monotonic()
    exports = run_session_step(
        machine,
        compositor=compositor,
        timeout=session_timeout,
        run=run,
        commands=recorded,
        sleep=sleep,
    )
    steps.append(
        {
            "name": "session",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "sid": exports.get("SESSION_ID"),
        }
    )
    env = session_env_from_exports(exports)
    shot_started = time.monotonic()
    capture_guest_screenshot(
        machine, env, screenshot_dest, run=run, commands=recorded
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    steps.append(
        {
            "name": "screenshot",
            "status": "pass",
            "seconds": round(time.monotonic() - shot_started, 1),
            "path": str(screenshot_dest),
        }
    )
    for command in recorded:
        if command_is_forbidden_for_session_only(command):
            raise SessionSmokeError(
                f"--session-only invoked a forbidden command: {command}"
            )
    return steps


def assert_session_only_commands(commands: Sequence[str]) -> None:
    """Fail if any recorded guest command is outside the session-only contract."""
    for command in commands:
        if command_is_forbidden_for_session_only(command):
            raise SessionSmokeError(
                f"--session-only invoked a forbidden command: {command}"
            )
