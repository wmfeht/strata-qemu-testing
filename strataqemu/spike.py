"""Throwaway Ubuntu Noble overlay proving Type=wayland + in-guest screenshot.

Operator-gated live KVM path. Argv builders here are the unit-test surface;
they never spawn ``qemu-system-x86_64``. Not ``images/ubuntu-2404/`` and not
``Guest.load`` (PR 4).
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from strataqemu import artifacts, config
from strataqemu.cli import (
    DEFAULT_FIRMWARE_SHARE_ROOTS,
    check_host,
    find_ovmf_code,
)
from strataqemu.overlay import copy_uefi_vars, create_overlay, create_overlay_argv
from strataqemu.ports import (
    AddressAlreadyInUse,
    allocate_ssh_port,
    allocate_vnc_port,
    is_address_already_in_use,
    retry_on_addr_in_use,
)
from strataqemu.qemu import (
    DEFAULT_CPUS,
    DEFAULT_MEMORY_MIB,
    Machine,
    build_qemu_argv,
    qmp_screendump,
)
from strataqemu.session import (
    FsEntry,
    GraphicalSession,
    SessionError,
    target_graphical_session,
)
from strataqemu.ssh import scp_download_command

log = logging.getLogger("strataqemu")

SPIKE_ID = "spike-wayland-ubuntu"
NOBLE_CLOUDIMG_NAME = "noble-server-cloudimg-amd64.img"
NOBLE_CLOUDIMG_URL = (
    "https://cloud-images.ubuntu.com/noble/current/"
    "noble-server-cloudimg-amd64.img"
)
SPIKE_DISK_GB = 40
BOOT_TIMEOUT_S = 180
SESSION_TIMEOUT_S = 180
CLOUD_INIT_TIMEOUT_S = 600
DESKTOP_INSTALL_TIMEOUT_S = 3600
GUEST_SCREENSHOT_REMOTE = "/tmp/strata-window.png"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

OVMF_VARS_RELATIVE = (
    Path("edk2/x64/OVMF_VARS.4m.fd"),
    Path("OVMF/OVMF_VARS_4M.fd"),
    Path("edk2/ovmf/OVMF_VARS.fd"),
)

DESKTOP_SETUP_SCRIPT = r"""
set -eu
export DEBIAN_FRONTEND=noninteractive
sudo -n apt-get update
sudo -n apt-get install -y ubuntu-desktop-minimal gnome-screenshot
sudo -n mkdir -p /etc/gdm3
sudo -n tee /etc/gdm3/custom.conf >/dev/null <<'EOF'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=tester
WaylandEnable=true
EOF
sudo -n systemctl set-default graphical.target
sudo -n systemctl mask gnome-initial-setup.service \
  gnome-initial-setup-first-login.service gnome-tour.service \
  || true
sudo -n -u tester mkdir -p /home/tester/.config
sudo -n -u tester touch /home/tester/.config/gnome-initial-setup-done
sudo -n loginctl enable-linger tester
sudo -n systemctl enable qemu-guest-agent || true
sudo -n systemctl start qemu-guest-agent || true
"""


def spike_overlay_create_argv(
    backing: Path | str,
    overlay: Path | str,
    *,
    qemu_img: str = "qemu-img",
) -> list[str]:
    """``qemu-img create`` argv for the spike overlay (strict absolute backing)."""
    return create_overlay_argv(backing, overlay, qemu_img=qemu_img)


def spike_qemu_argv(
    *,
    overlay: Path | str,
    run_dir: Path | str,
    ssh_port: int,
    vnc_port: int,
    ovmf_code: Path | str | None = None,
    ovmf_vars: Path | str | None = None,
    cidata_iso: Path | str | None = None,
) -> list[str]:
    """Frozen test display stack for the Noble spike. Never graphical/GTK."""
    return build_qemu_argv(
        overlay=overlay,
        run_dir=run_dir,
        ssh_port=ssh_port,
        vnc_port=vnc_port,
        cpus=DEFAULT_CPUS,
        memory_mib=DEFAULT_MEMORY_MIB,
        graphical=False,
        ovmf_code=ovmf_code,
        ovmf_vars=ovmf_vars,
        cidata_iso=cidata_iso,
    )


def ovmf_vars_candidates(
    share_roots: Sequence[Path] | None = None,
) -> list[Path]:
    roots = tuple(share_roots) if share_roots else DEFAULT_FIRMWARE_SHARE_ROOTS
    return [root / rel for root in roots for rel in OVMF_VARS_RELATIVE]


def find_ovmf_vars(share_roots: Sequence[Path] | None = None) -> Path | None:
    return find_ovmf_code(ovmf_vars_candidates(share_roots))


def _cloudinit_user_data(pubkey: str) -> str:
    key = pubkey.strip()
    return (
        "#cloud-config\n"
        "hostname: spike-ubuntu\n"
        "manage_etc_hosts: true\n"
        "users:\n"
        "  - name: tester\n"
        "    gecos: Tester\n"
        "    groups: [sudo]\n"
        "    shell: /bin/bash\n"
        "    lock_passwd: false\n"
        "    sudo: 'ALL=(ALL) NOPASSWD: ALL'\n"
        "    ssh_authorized_keys:\n"
        f"      - {key}\n"
        "ssh_pwauth: true\n"
        "chpasswd:\n"
        "  expire: false\n"
        "  list: |\n"
        "    tester:foobar\n"
        "package_update: true\n"
        "packages:\n"
        "  - qemu-guest-agent\n"
        "runcmd:\n"
        "  - systemctl enable --now qemu-guest-agent\n"
        "  - loginctl enable-linger tester\n"
    )


def cidata_xorriso_argv(dest: Path | str) -> list[str]:
    """``xorriso`` argv; run with cwd containing ``user-data`` and ``meta-data``."""
    return [
        "xorriso",
        "-as",
        "mkisofs",
        "-R",
        "-V",
        "cidata",
        "-o",
        str(Path(dest)),
        "user-data",
        "meta-data",
    ]


def write_cidata_iso(dest: Path, *, pubkey: str) -> Path:
    """Minimal NoCloud seed (volume id ``cidata``). Spike-only; not cloudinit.py."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.parent / "cidata-src"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / "user-data").write_text(_cloudinit_user_data(pubkey), encoding="utf-8")
    (work / "meta-data").write_text(
        "instance-id: spike-wayland-ubuntu\nlocal-hostname: spike-ubuntu\n",
        encoding="utf-8",
    )
    argv = cidata_xorriso_argv(dest.resolve())
    subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        cwd=work,
    )
    return dest


def download_noble_cloudimg(
    dest: Path,
    *,
    url: str = NOBLE_CLOUDIMG_URL,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 10_000_000:
        log.info("using cached cloudimg %s", dest)
        return dest
    partial = dest.with_suffix(dest.suffix + ".partial")
    log.info("downloading %s -> %s", url, dest)
    subprocess.run(
        [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "3",
            "-o",
            str(partial),
            url,
        ],
        check=True,
    )
    partial.replace(dest)
    return dest


def ensure_qcow2_backing(src: Path, converted: Path) -> Path:
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(src)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(info.stdout)
    if data.get("format") == "qcow2":
        return src
    converted.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["qemu-img", "convert", "-O", "qcow2", str(src), str(converted)],
        check=True,
    )
    return converted


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
) -> subprocess.CompletedProcess[str]:
    argv = machine.ssh(command)
    log.debug("ssh: %s", command)
    try:
        proc = subprocess.run(
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
        raise RuntimeError(
            f"ssh command failed ({proc.returncode}): {command}\n"
            f"{proc.stdout}{proc.stderr}"
        )
    return proc


def cloud_init_is_done(returncode: int, output: str) -> bool:
    """``cloud-init status --wait`` exits 2 on recoverable errors; ``done`` is enough."""
    blob = output.lower()
    if "status: done" in blob:
        return True
    return returncode == 0


def _wait_cloud_init(machine: Machine) -> None:
    proc = _ssh_run(
        machine,
        "cloud-init status --wait",
        timeout=CLOUD_INIT_TIMEOUT_S,
    )
    blob = f"{proc.stdout}{proc.stderr}"
    if not cloud_init_is_done(proc.returncode, blob):
        raise RuntimeError(
            f"cloud-init did not finish ({proc.returncode}): {blob[-2000:]}"
        )


def _wait_ssh(machine: Machine, *, timeout: float) -> None:
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
            raise RuntimeError(
                f"qemu exited {machine._proc.returncode} while waiting for SSH\n"
                f"{qemu_log[-4000:]}"
            )
        try:
            proc = _ssh_run(machine, "true", timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
            time.sleep(2)
            continue
        if proc.returncode == 0:
            return
        last = (proc.stderr or proc.stdout or "").strip()
        time.sleep(2)
    raise TimeoutError(f"SSH did not become ready in {timeout}s: {last}")


def _remote_listing(machine: Machine, path: str) -> list[FsEntry]:
    script = (
        "python3 - <<'PY'\n"
        "import json, stat\n"
        "from pathlib import Path\n"
        f"rd = Path({path!r})\n"
        "out = []\n"
        "if rd.is_dir():\n"
        "    for p in sorted(rd.iterdir(), key=lambda x: x.name):\n"
        "        try:\n"
        "            st = p.lstat()\n"
        "        except OSError:\n"
        "            continue\n"
        "        out.append({\n"
        "            'name': p.name,\n"
        "            'is_socket': stat.S_ISSOCK(st.st_mode),\n"
        "            'is_dir': stat.S_ISDIR(st.st_mode),\n"
        "            'mtime': st.st_mtime,\n"
        "        })\n"
        "print(json.dumps(out))\n"
        "PY"
    )
    proc = _ssh_run(machine, script, timeout=30)
    if proc.returncode != 0:
        return []
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    entries: list[FsEntry] = []
    for item in items:
        entries.append(
            FsEntry(
                name=str(item.get("name", "")),
                is_socket=bool(item.get("is_socket")),
                is_dir=bool(item.get("is_dir")),
                mtime=float(item.get("mtime") or 0),
            )
        )
    return entries


def _probe_graphical_session(machine: Machine) -> GraphicalSession:
    uid_proc = _ssh_run(machine, "id -u", timeout=15, check=True)
    uid = int(uid_proc.stdout.strip())
    ssh_sid = _ssh_run(
        machine, "printf '%s' \"${XDG_SESSION_ID:-}\"", timeout=15
    ).stdout.strip() or None
    list_text = _ssh_run(
        machine, "loginctl --no-legend list-sessions", timeout=15, check=True
    ).stdout
    show_by_sid: dict[str, str] = {}
    for line in list_text.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        show = _ssh_run(
            machine, f"loginctl show-session {shlex.quote(sid)}", timeout=15
        )
        if show.returncode == 0:
            show_by_sid[sid] = show.stdout
    runtime = f"/run/user/{uid}"
    runtime_entries = _remote_listing(machine, runtime)
    hypr_entries = _remote_listing(machine, f"{runtime}/hypr")
    return target_graphical_session(
        uid=uid,
        list_sessions_text=list_text,
        show_session_by_sid=show_by_sid,
        runtime_entries=runtime_entries,
        hypr_entries=hypr_entries,
        ssh_session_id=ssh_sid,
    )


def _wait_wayland(machine: Machine, *, timeout: float) -> GraphicalSession:
    deadline = time.monotonic() + timeout
    last = "no probe yet"
    while time.monotonic() < deadline:
        try:
            session = _probe_graphical_session(machine)
            return session
        except SessionError as exc:
            if "x11" in str(exc).lower():
                raise
            last = str(exc)
        time.sleep(2)
    raise TimeoutError(
        f"no active Type=wayland seat0 session in {timeout}s: {last}"
    )


def _env_prefix(env: dict[str, str]) -> str:
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())


def _guest_screenshot(machine: Machine, session: GraphicalSession, dest: Path) -> None:
    which = _ssh_run(machine, "command -v gnome-screenshot", timeout=15)
    if which.returncode != 0:
        raise RuntimeError(
            "screenshot tool missing; gnome-screenshot is not on PATH"
        )
    prefix = _env_prefix(session.env)
    cmd = f"{prefix} gnome-screenshot -f {shlex.quote(GUEST_SCREENSHOT_REMOTE)}"
    _ssh_run(machine, cmd, timeout=60, check=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = scp_download_command(
        port=machine.ssh_port,
        identity=machine.identity if machine.identity is not None else "",
        remote_path=GUEST_SCREENSHOT_REMOTE,
        local_path=dest,
    )
    scp = subprocess.run(argv, check=False, capture_output=True, text=True)
    _append_log(
        machine.artifacts.root / "ssh.log",
        f"$ scp {GUEST_SCREENSHOT_REMOTE} -> {dest}\n"
        f"[exit {scp.returncode}]\n{scp.stdout}{scp.stderr}",
    )
    if scp.returncode != 0:
        raise RuntimeError(f"scp screenshot failed: {scp.stderr}")
    data = dest.read_bytes() if dest.is_file() else b""
    if len(data) < 256 or not data.startswith(PNG_MAGIC):
        raise RuntimeError(
            f"in-guest screenshot is empty or not a PNG ({dest}, {len(data)} bytes)"
        )


def _best_effort_qmp_dump(machine: Machine, dest: Path) -> bool:
    try:
        return qmp_screendump(machine.artifacts.qmp_sock, dest)
    except OSError as exc:
        log.debug("qmp screendump extra failed: %s", exc)
        return False


def _spawn_qemu(
    *,
    overlay: Path,
    run_dir: Path,
    ovmf_code: Path,
    ovmf_vars: Path,
    cidata_iso: Path,
    identity: Path,
) -> Machine:
    arts = artifacts.RunArtifacts(run_dir)

    def attempt() -> Machine:
        ssh_port = allocate_ssh_port()
        vnc_port = allocate_vnc_port()
        arts.ssh_port_file.write_text(f"{ssh_port}\n", encoding="utf-8")
        argv = spike_qemu_argv(
            overlay=overlay,
            run_dir=run_dir,
            ssh_port=ssh_port,
            vnc_port=vnc_port,
            ovmf_code=ovmf_code,
            ovmf_vars=ovmf_vars,
            cidata_iso=cidata_iso,
        )
        log.info("qemu argv: %s", " ".join(argv))
        logf = arts.qemu_log.open("ab")
        try:
            proc = subprocess.Popen(
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
            raise RuntimeError(
                f"qemu exited {proc.returncode} immediately\n{text[-4000:]}"
            )
        return Machine(
            overlay,
            run_dir,
            ssh_port=ssh_port,
            vnc_port=vnc_port,
            identity=identity,
            process=proc,
        )

    return retry_on_addr_in_use(attempt)


def _write_result(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _install_desktop(machine: Machine) -> None:
    log.info("installing ubuntu-desktop-minimal + gnome-screenshot")
    _ssh_run(
        machine,
        DESKTOP_SETUP_SCRIPT,
        timeout=DESKTOP_INSTALL_TIMEOUT_S,
        check=True,
    )


def _reboot_and_wait(machine: Machine, *, timeout: float) -> None:
    log.info("rebooting guest into graphical.target")
    try:
        _ssh_run(machine, "sudo -n systemctl reboot", timeout=20)
    except (RuntimeError, subprocess.TimeoutExpired):
        pass
    deadline = time.monotonic() + timeout
    dropped = False
    while time.monotonic() < deadline:
        if machine._proc is not None and machine._proc.poll() is not None:
            raise RuntimeError("qemu exited during reboot")
        try:
            proc = _ssh_run(machine, "true", timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            dropped = True
            break
        if proc.returncode != 0:
            dropped = True
            break
        time.sleep(2)
    if not dropped:
        log.debug("SSH did not drop after reboot request; still waiting")
    remaining = max(15.0, deadline - time.monotonic())
    _wait_ssh(machine, timeout=remaining)


def run_spike_wayland_ubuntu(*, keep: bool = False) -> int:
    """Boot a throwaway Noble overlay; assert Type=wayland + guest screenshot."""
    host = check_host()
    if not host.ok:
        for err in host.errors:
            print(err, file=sys.stderr)
        return 1
    if host.ovmf_code is None or host.ssh_key is None:
        print("check-host ok but OVMF or SSH key missing", file=sys.stderr)
        return 1

    vars_template = find_ovmf_vars()
    if vars_template is None:
        searched = ", ".join(str(p) for p in ovmf_vars_candidates())
        print(
            f"OVMF_VARS template not found (search order: {searched}).",
            file=sys.stderr,
        )
        return 1

    cache = config.cache_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifacts.runs_dir(cache) / f"{stamp}-{SPIKE_ID}"
    run_dir.mkdir(parents=True, exist_ok=True)
    arts = artifacts.RunArtifacts(run_dir)
    machine: Machine | None = None
    failed = False
    started = time.monotonic()
    result: dict = {"guest": SPIKE_ID, "ok": False}

    try:
        pubkey = host.ssh_key.with_name(host.ssh_key.name + ".pub").read_text(
            encoding="utf-8"
        )
        downloads = artifacts.downloads_dir(cache)
        img = download_noble_cloudimg(downloads / NOBLE_CLOUDIMG_NAME)
        backing = ensure_qcow2_backing(
            img, downloads / "noble-server-cloudimg-amd64.qcow2"
        )
        try:
            backing.chmod(0o444)
        except OSError:
            pass
        overlay_argv = spike_overlay_create_argv(backing, arts.overlay)
        log.info("overlay argv: %s", " ".join(overlay_argv))
        try:
            create_overlay(backing, arts.overlay)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"overlay create failed: {exc.stderr or exc.stdout or exc}"
            ) from exc
        subprocess.run(
            ["qemu-img", "resize", str(arts.overlay), f"{SPIKE_DISK_GB}G"],
            check=True,
            capture_output=True,
            text=True,
        )
        cidata = write_cidata_iso(run_dir / "cidata.iso", pubkey=pubkey)
        copy_uefi_vars(arts.ovmf_vars, template_vars=vars_template)
        machine = _spawn_qemu(
            overlay=arts.overlay,
            run_dir=run_dir,
            ovmf_code=host.ovmf_code,
            ovmf_vars=arts.ovmf_vars,
            cidata_iso=cidata,
            identity=host.ssh_key,
        )
        _wait_ssh(machine, timeout=BOOT_TIMEOUT_S)
        _wait_cloud_init(machine)
        _install_desktop(machine)
        _reboot_and_wait(machine, timeout=BOOT_TIMEOUT_S)
        session = _wait_wayland(machine, timeout=SESSION_TIMEOUT_S)
        shot = run_dir / "screendump-session.png"
        _guest_screenshot(machine, session, shot)
        qmp_path = run_dir / "qmp-session.png"
        qmp_ok = _best_effort_qmp_dump(machine, qmp_path)
        result.update(
            {
                "ok": True,
                "wayland_sid": session.sid,
                "env": session.env,
                "screenshot": str(shot),
                "qmp_screendump": str(qmp_path) if qmp_ok else None,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        _write_result(arts.result_json, result)
        print(f"spike-wayland-ubuntu: Type=wayland session {session.sid}")
        print(f"screenshot: {shot}")
        return 0
    except KeyboardInterrupt:
        failed = True
        result["error"] = "interrupted"
        _write_result(arts.result_json, result)
        print("spike-wayland-ubuntu: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        failed = True
        result["error"] = str(exc)
        _write_result(arts.result_json, result)
        log.exception("spike-wayland-ubuntu failed")
        print(f"spike-wayland-ubuntu: {exc}", file=sys.stderr)
        return 1
    finally:
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
        if not keep and not failed:
            shutil.rmtree(run_dir, ignore_errors=True)
        elif failed:
            print(f"kept run dir (failure): {run_dir}", file=sys.stderr)
