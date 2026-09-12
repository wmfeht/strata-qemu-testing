"""Session, screenshot, install, version, and desktop-oracle steps for ``run-test``.

Injectable SSH/run-dir so host unittests never spawn QEMU. ``--session-only``
runs session + in-guest screenshot; it does not gtk-launch Strata, run
``install.sh``, or wait on the application bus name. ``--install-from``
release/local-archive on ``arch``, ``ubuntu-2404``, and ``fedora-workstation``
runs session → install → version → desktop-entry → window.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from strataqemu.guest import Guest
from strataqemu.qemu import Machine, qmp_screendump, vnc_framebuffer_png
from strataqemu.ssh import scp_command, scp_download_command

log = logging.getLogger("strataqemu")

RunFn = Callable[..., subprocess.CompletedProcess]

SESSION_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 180
WINDOW_TIMEOUT_S = 45
DESKTOP_ENTRY_TIMEOUT_S = 10
VERSION_TIMEOUT_S = 10
GUEST_SCREENSHOT_REMOTE = "/tmp/strata-window.png"
GUEST_ARCHIVE_REMOTE = "/tmp/strata-archive.tar.gz"
SCREENSHOT_TOOL_MISSING = "screenshot tool missing; rebuild the golden"
GNOME_SCREENSHOT_TIMEOUT_S = 15
STRATA_BUS_NAME = "io.github.lgse.Strata"
STRATA_DESKTOP_FILE = "io.github.lgse.Strata.desktop"
STRATA_BIN_REL = ".local/bin/strata"
STRATA_VERSION_COMMAND = f"$HOME/{STRATA_BIN_REL} --version"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 256
SESSION_ONLY_STEPS = ("session", "screenshot")
INSTALL_FROM_RELEASE_STEPS = (
    "session",
    "install",
    "version",
    "desktop-entry",
    "window",
)
INSTALL_FROM_RELEASE_GUESTS = frozenset(
    {
        "arch",
        "ubuntu-2404",
        "fedora-workstation",
        "omarchy-4",
        "omarchy-3",
    }
)
VERSION_CLI_RE = re.compile(r"^(strata\s+)?v?\d+\.\d+", re.IGNORECASE)
INSTALL_FROM_FAIL_CLOSED = (
    "run-test: --install-from is not supported for this guest; "
    "use --session-only"
)
INSTALL_SH_SHA256_PREFIX = "INSTALL_SH_SHA256="
INSTALL_SH_URL = "https://raw.githubusercontent.com/lgse/strata/main/install.sh"
INSTALL_SH_FLAGS = (
    "--non-interactive",
    "--with-desktop-entry",
    "--without-file-chooser",
)
GITHUB_LATEST_RELEASE = "https://github.com/lgse/strata/releases/latest"
ARCHIVE_VERSION_RE = re.compile(r"^strata-v?(.+)$", re.IGNORECASE)
_ARCHIVE_TARGET_MARKERS = (
    "-x86_64-",
    "-aarch64-",
    "-arm64-",
    "-x86_64.",
    "-aarch64.",
    "-arm64.",
)
LOCAL_ARCHIVE_MISSING_PATH = (
    "run-test: --install-from local-archive requires a host archive path"
)
# Substrings that must not appear in --session-only guest commands.
SESSION_ONLY_FORBIDDEN = (
    "install.sh",
    "strata --version",
    "gtk-launch",
    "gio launch",
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


def smoke_install_script() -> Path:
    return guest_tests_dir() / "smoke-install.sh"


def missing_golden_message(guest_id: str) -> str:
    return f"run `mise run image-build -- {guest_id}` first"


def compositor_process_name(guest: Guest | str) -> str:
    """Process name for the session compositor check.

    ``ubuntu-2404`` (and GNOME/Mutter recipes) use ``gnome-shell``.
    ``arch`` (Hyprland) uses ``Hyprland``.
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
    if (
        guest_id in {"arch", "omarchy-4", "omarchy-3"}
        or compositor == "hyprland"
        or kind == "hyprland"
    ):
        return "Hyprland"
    return compositor or "gnome-shell"


def screenshot_tool_for_compositor(compositor: str) -> str:
    """In-guest capture binary. Hyprland uses ``grim``; GNOME uses gnome-screenshot."""
    if compositor == "Hyprland":
        return "grim"
    return "gnome-screenshot"


def supports_install_from_release(guest: Guest | str) -> bool:
    guest_id = guest.id if isinstance(guest, Guest) else guest
    return guest_id in INSTALL_FROM_RELEASE_GUESTS


def install_arch_script() -> Path:
    return repo_root() / "images" / "common" / "install-arch.sh"


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


def gdbus_name_has_owner_command(env: Mapping[str, str] | None = None) -> str:
    body = " ".join(shlex.quote(part) for part in GDBUS_NAME_HAS_OWNER_ARGV)
    if env:
        return f"{env_prefix(env)} {body}"
    return body


def install_sh_argv(*, archive: str | None = None) -> list[str]:
    argv = list(INSTALL_SH_FLAGS)
    if archive:
        argv.extend(["--archive", archive])
    return argv


def install_smoke_command(
    *,
    archive: str | None = None,
    forbid_omarchy: bool = False,
) -> str:
    """Host-side SSH command that runs the uploaded guest install smoke."""
    prefix = "SMOKE_FORBID_OMARCHY=1 " if forbid_omarchy else ""
    flags = " ".join(shlex.quote(part) for part in install_sh_argv(archive=archive))
    return f"{prefix}bash /tmp/smoke-install.sh {flags}".rstrip()


def strata_version_is_cli(returncode: int, output: str) -> bool:
    """True when ``strata --version`` printed a version and did not open GTK."""
    del returncode
    line = ""
    for raw in output.splitlines():
        stripped = raw.strip()
        if stripped:
            line = stripped
            break
    if not line or not VERSION_CLI_RE.match(line):
        return False
    return True


def parse_observed_version(text: str) -> str:
    """First line of ``strata --version``. Not an installer-log parse."""
    line = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped:
            line = stripped
            break
    if not line:
        raise SessionSmokeError("strata --version produced empty stdout")
    line = re.sub(r"^strata\s+", "", line, flags=re.IGNORECASE)
    return _strip_v_prefix(line)


def _strip_v_prefix(value: str) -> str:
    text = value.strip()
    if len(text) > 1 and text[0] in "vV" and text[1].isdigit():
        return text[1:]
    return text


def normalize_version(value: str) -> str:
    return _strip_v_prefix(value.strip())


def versions_match(observed: str, intended: str) -> bool:
    return normalize_version(observed) == normalize_version(intended)


def parse_archive_version(filename: str) -> str:
    """Version from ``strata-VERSION-x86_64-unknown-linux-gnu.tar.gz``."""
    name = Path(filename).name
    for ext in (".tar.gz", ".tar.xz", ".tgz", ".zip", ".tar"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    match = ARCHIVE_VERSION_RE.match(name)
    if not match:
        raise SessionSmokeError(
            f"cannot parse intended version from archive name {Path(filename).name!r}"
        )
    rest = match.group(1)
    for marker in _ARCHIVE_TARGET_MARKERS:
        idx = rest.lower().find(marker)
        if idx != -1:
            rest = rest[:idx]
            break
    else:
        rest = re.sub(r"-(x86_64|aarch64|arm64)$", "", rest, flags=re.IGNORECASE)
    rest = rest.strip()
    if not re.match(r"\d+\.\d+", rest):
        raise SessionSmokeError(
            f"cannot parse intended version from archive name {Path(filename).name!r}"
        )
    return rest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def github_latest_version(
    url: str = GITHUB_LATEST_RELEASE,
    *,
    opener: Callable[..., object] | None = None,
) -> str:
    """Resolve GitHub ``/releases/latest`` to a tag (no leading ``v``)."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "strata-qemu-testing",
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=15) as resp:  # type: ignore[misc]
            body = resp.read()
            final = resp.geturl() if hasattr(resp, "geturl") else url
    except (OSError, urllib.error.URLError) as exc:
        raise SessionSmokeError(
            f"could not resolve latest Strata release from {url}: {exc}"
        ) from exc
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    tag = ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            tag = str(data.get("tag_name") or data.get("tag") or "")
    except json.JSONDecodeError:
        tag = ""
    if not tag and final:
        tag = str(final).rstrip("/").rsplit("/", 1)[-1]
    if not tag or tag in {"latest", "releases"}:
        raise SessionSmokeError(
            f"could not resolve latest Strata release from {url}"
        )
    return normalize_version(tag)


def resolve_intended_version(
    *,
    install_from: str,
    archive_path: Path | None = None,
    intended_version: str | None = None,
    intended_version_fn: Callable[[], str] | None = None,
) -> str:
    if intended_version:
        return normalize_version(intended_version)
    if intended_version_fn is not None:
        return normalize_version(intended_version_fn())
    if install_from == "local-archive":
        if archive_path is None:
            raise SessionSmokeError("local-archive requires a host archive path")
        return parse_archive_version(str(archive_path))
    return github_latest_version()


def parse_install_sh_sha256(text: str) -> str:
    """Read ``INSTALL_SH_SHA256=`` from the Arch helper stdout."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(INSTALL_SH_SHA256_PREFIX):
            continue
        digest = line.split("=", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return digest
    raise SessionSmokeError("install helper did not print INSTALL_SH_SHA256")


def hyprctl_class_oracle_command(env: Mapping[str, str] | None = None) -> str:
    jq_filter = f'.[] | select(.class == "{STRATA_BUS_NAME}")'
    body = f"hyprctl clients -j | jq -e {shlex.quote(jq_filter)}"
    if env:
        return f"{env_prefix(env)} {body}"
    return body


def launch_desktop_entry_command(env: Mapping[str, str]) -> str:
    """gtk-launch, else gio launch, in the graphical env. Not used by --session-only.

    The launcher is backgrounded: gtk-launch/gio wait for the GTK app to exit.
    """
    inner = (
        "if command -v gtk-launch >/dev/null 2>&1; then "
        f"nohup gtk-launch {STRATA_BUS_NAME} >/tmp/strata-launch.log 2>&1 & "
        "elif command -v gio >/dev/null 2>&1; then "
        f"nohup gio launch \"$HOME/.local/share/applications/{STRATA_DESKTOP_FILE}\" "
        ">/tmp/strata-launch.log 2>&1 & "
        "else echo 'gtk-launch and gio launch missing' >&2; exit 1; fi; "
        "disown || true; sleep 1; exit 0"
    )
    prefix = env_prefix(env)
    return f"{prefix} bash -lc {shlex.quote(inner)}"


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
    tool: str | None = None,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    vnc_capture_fn: Callable[..., bool] | None = None,
    screenshot_timeout: float = GNOME_SCREENSHOT_TIMEOUT_S,
) -> Path:
    """In-guest ``grim`` / ``gnome-screenshot``. Missing binary is a golden bug.

    GNOME 50 in QEMU denies ``org.gnome.Shell.Screenshot`` and the screenshot
    portal never completes; after the in-guest tool is confirmed on PATH,
    a timed-out ``gnome-screenshot`` falls back to the QEMU VNC framebuffer.
    """
    binary = tool or "gnome-screenshot"
    which = ssh_run(
        machine,
        f"command -v {shlex.quote(binary)}",
        timeout=15,
        run=run,
        commands=commands,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
    prefix = env_prefix(env)
    if binary == "grim":
        cmd = f"{prefix} grim {shlex.quote(GUEST_SCREENSHOT_REMOTE)}"
        ssh_run(
            machine, cmd, timeout=screenshot_timeout, check=True, run=run, commands=commands
        )
        scp_from_guest(
            machine, GUEST_SCREENSHOT_REMOTE, dest, run=run, commands=commands
        )
    else:
        cmd = f"{prefix} gnome-screenshot -f {shlex.quote(GUEST_SCREENSHOT_REMOTE)}"
        grabbed = False
        try:
            ssh_run(
                machine,
                cmd,
                timeout=screenshot_timeout,
                check=True,
                run=run,
                commands=commands,
            )
            scp_from_guest(
                machine, GUEST_SCREENSHOT_REMOTE, dest, run=run, commands=commands
            )
            grabbed = True
        except (subprocess.TimeoutExpired, SessionSmokeError):
            grabbed = False
        if not grabbed or not _png_ok(dest):
            grabber = vnc_capture_fn or vnc_framebuffer_png
            if machine.vnc_port is None or not grabber(
                dest, display=machine.vnc_port
            ):
                raise SessionSmokeError(
                    "gnome-screenshot did not produce a PNG; VNC fallback failed"
                )
    data = dest.read_bytes() if dest.is_file() else b""
    if len(data) < MIN_PNG_BYTES or not data.startswith(PNG_MAGIC):
        raise SessionSmokeError(
            f"in-guest screenshot is empty or not a PNG ({dest}, {len(data)} bytes)"
        )
    return dest


def _png_ok(dest: Path) -> bool:
    try:
        data = dest.read_bytes()
    except OSError:
        return False
    return len(data) >= MIN_PNG_BYTES and data.startswith(PNG_MAGIC)


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
    env: Mapping[str, str] | None = None,
    timeout: float = 45,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Wait until the session bus owns ``io.github.lgse.Strata``.

    Not used by ``--session-only``.
    """
    remote = gdbus_name_has_owner_command(env)
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
        machine,
        env,
        screenshot_dest,
        tool=screenshot_tool_for_compositor(compositor),
        run=run,
        commands=recorded,
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


def wait_hyprland_class(
    machine: Machine,
    env: Mapping[str, str],
    *,
    timeout: float = WINDOW_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Wait until ``hyprctl clients`` lists class ``io.github.lgse.Strata``."""
    remote = hyprctl_class_oracle_command(env)
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    nap = sleep or time.sleep
    while time.monotonic() < deadline:
        proc = ssh_run(
            machine, remote, timeout=15, run=run, commands=commands
        )
        blob = f"{proc.stdout}{proc.stderr}"
        if proc.returncode == 0:
            return
        last = blob.strip() or f"exit {proc.returncode}"
        nap(1)
    raise TimeoutError(
        f"hyprctl class {STRATA_BUS_NAME} not present in {timeout}s: {last}"
    )


def _desktop_entry_probe_command() -> str:
    path = f"$HOME/.local/share/applications/{STRATA_DESKTOP_FILE}"
    return f"test -f {path} && grep -E '^Exec=' {path}"


def run_install_from_release_steps(
    machine: Machine,
    *,
    guest: Guest,
    screenshot_dest: Path,
    qmp_dest: Path | None = None,
    session_timeout: float = SESSION_TIMEOUT_S,
    run: RunFn | None = None,
    commands: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    install_from: str = "release",
    archive_path: Path | str | None = None,
    intended_version: str | None = None,
    intended_version_fn: Callable[[], str] | None = None,
) -> tuple[list[dict], dict]:
    """session → install → version → desktop-entry → window."""
    if not supports_install_from_release(guest):
        raise SessionSmokeError(
            f"--install-from {install_from} is not implemented for {guest.id}"
        )
    if install_from not in {"release", "local-archive"}:
        raise SessionSmokeError(
            f"--install-from {install_from} is not implemented for {guest.id}"
        )
    archive: Path | None = Path(archive_path) if archive_path is not None else None
    if install_from == "local-archive":
        if archive is None:
            raise SessionSmokeError(LOCAL_ARCHIVE_MISSING_PATH)
        if not archive.is_file():
            raise SessionSmokeError(f"run-test: archive not found: {archive}")

    intended = resolve_intended_version(
        install_from=install_from,
        archive_path=archive,
        intended_version=intended_version,
        intended_version_fn=intended_version_fn,
    )

    compositor = compositor_process_name(guest)
    tool = screenshot_tool_for_compositor(compositor)
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

    started = time.monotonic()
    helper = smoke_install_script()
    if not helper.is_file():
        raise SessionSmokeError(f"missing install smoke script {helper}")
    scp_to_guest(
        machine, helper, "/tmp/smoke-install.sh", run=run, commands=recorded
    )
    archive_digest: str | None = None
    remote_archive: str | None = None
    if archive is not None:
        archive_digest = sha256_file(archive)
        remote_archive = GUEST_ARCHIVE_REMOTE
        scp_to_guest(
            machine, archive, remote_archive, run=run, commands=recorded
        )
    proc = ssh_run(
        machine,
        install_smoke_command(
            archive=remote_archive,
            forbid_omarchy=guest.id == "arch",
        ),
        timeout=INSTALL_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    digest = parse_install_sh_sha256(f"{proc.stdout}{proc.stderr}")
    ssh_run(
        machine,
        f"test -x \"$HOME/{STRATA_BIN_REL}\"",
        timeout=15,
        check=True,
        run=run,
        commands=recorded,
    )
    steps.append(
        {
            "name": "install",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "install_sh_sha256": digest,
        }
    )

    started = time.monotonic()
    observed: str | None = None
    version_status = "skip"
    version_reason = "strata --version is not a CLI"
    try:
        ver = ssh_run(
            machine,
            STRATA_VERSION_COMMAND,
            timeout=VERSION_TIMEOUT_S,
            check=False,
            run=run,
            commands=recorded,
        )
        if strata_version_is_cli(ver.returncode, ver.stdout):
            if ver.returncode != 0:
                raise SessionSmokeError(
                    f"ssh command failed ({ver.returncode}): "
                    f"{STRATA_VERSION_COMMAND}\n{ver.stdout}{ver.stderr}"
                )
            observed = parse_observed_version(ver.stdout)
            if not versions_match(observed, intended):
                raise SessionSmokeError(
                    f"version mismatch: intended {intended}, observed {observed} "
                    f"(from {STRATA_VERSION_COMMAND})"
                )
            version_status = "pass"
            version_reason = ""
    except subprocess.TimeoutExpired:
        version_status = "skip"
        version_reason = "strata --version is not a CLI"
    version_step: dict = {
        "name": "version",
        "status": version_status,
        "seconds": round(time.monotonic() - started, 1),
    }
    if version_status == "pass":
        version_step.update(
            {
                "oracle": "strata --version",
                "intended": intended,
                "observed": observed,
            }
        )
    else:
        version_step["reason"] = version_reason
    steps.append(version_step)

    started = time.monotonic()
    entry = ssh_run(
        machine,
        _desktop_entry_probe_command(),
        timeout=DESKTOP_ENTRY_TIMEOUT_S,
        check=True,
        run=run,
        commands=recorded,
    )
    exec_blob = f"{entry.stdout}{entry.stderr}"
    if STRATA_BIN_REL not in exec_blob:
        raise SessionSmokeError(
            f"desktop Exec= does not point at ~/{STRATA_BIN_REL}: {exec_blob}"
        )
    steps.append(
        {
            "name": "desktop-entry",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
        }
    )

    started = time.monotonic()
    which = ssh_run(
        machine,
        f"command -v {shlex.quote(tool)}",
        timeout=15,
        run=run,
        commands=recorded,
    )
    if screenshot_tool_missing(which.returncode):
        raise SessionSmokeError(SCREENSHOT_TOOL_MISSING)
    ssh_run(
        machine,
        launch_desktop_entry_command(env),
        timeout=30,
        check=True,
        run=run,
        commands=recorded,
    )
    window_error: BaseException | None = None
    window_oracle = "hyprctl-class" if compositor == "Hyprland" else "bus-name"
    try:
        if compositor == "Hyprland":
            wait_hyprland_class(
                machine,
                env,
                timeout=WINDOW_TIMEOUT_S,
                run=run,
                commands=recorded,
                sleep=sleep,
            )
        else:
            wait_gnome_bus_name(
                machine,
                env=env,
                timeout=WINDOW_TIMEOUT_S,
                run=run,
                commands=recorded,
                sleep=sleep,
            )
    except (TimeoutError, SessionSmokeError) as exc:
        window_error = exc
    capture_guest_screenshot(
        machine,
        env,
        screenshot_dest,
        tool=tool,
        run=run,
        commands=recorded,
    )
    if qmp_dest is not None:
        extra_qmp_screendump(machine, qmp_dest)
    if window_error is not None:
        raise window_error
    steps.append(
        {
            "name": "window",
            "status": "pass",
            "seconds": round(time.monotonic() - started, 1),
            "path": str(screenshot_dest),
            "oracle": window_oracle,
        }
    )
    extras: dict = {
        "install_method": "install.sh",
        "install_sh_sha256": digest,
        "intended_version": intended,
        "screenshot": str(screenshot_dest),
    }
    if observed is not None:
        extras["observed_version"] = observed
    if archive_digest is not None:
        extras["archive_sha256"] = archive_digest
    return steps, extras
