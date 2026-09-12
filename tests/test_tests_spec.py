"""Host tests for tests_spec session / GNOME oracle functions. No QEMU VM."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strataqemu.guest import load_guest
from strataqemu.qemu import Machine
from strataqemu.tests_spec import (
    GDBUS_NAME_HAS_OWNER_ARGV,
    GUEST_ARCHIVE_REMOTE,
    INSTALL_FROM_RELEASE_STEPS,
    INSTALL_SH_FLAGS,
    SCREENSHOT_TOOL_MISSING,
    SESSION_ONLY_FORBIDDEN,
    STRATA_BUS_NAME,
    STRATA_VERSION_COMMAND,
    SessionSmokeError,
    assert_session_only_commands,
    capture_guest_screenshot,
    command_is_forbidden_for_session_only,
    compositor_process_name,
    extra_qmp_screendump,
    gdbus_name_has_owner_command,
    github_latest_version,
    hyprctl_class_oracle_command,
    install_arch_script,
    install_sh_argv,
    install_smoke_command,
    missing_golden_message,
    parse_archive_version,
    parse_install_sh_sha256,
    parse_name_has_owner,
    parse_observed_version,
    parse_session_exports,
    run_install_from_release_steps,
    run_session_only_steps,
    screenshot_tool_for_compositor,
    sha256_file,
    smoke_desktop_script,
    smoke_install_script,
    smoke_session_script,
    strata_version_is_cli,
    supports_install_from_release,
    versions_match,
    wait_gnome_bus_name,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.remote: list[str] = []
        self.which_screenshot = 0
        self.which_grim = 0
        self.session_stdout = (
            "SESSION_ID=c1\n"
            "XDG_RUNTIME_DIR=/run/user/1000\n"
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
            "WAYLAND_DISPLAY=wayland-0\n"
        )
        self.session_code = 0
        self.gdbus_stdout = "(true,)\n"
        self.write_png_on_scp = True
        self.install_digest = hashlib.sha256(b"fixture-install-sh").hexdigest()
        self.hyprctl_code = 0
        self.exec_line = "Exec=/home/tester/.local/bin/strata %U\n"
        self.version_stdout = "0.9.0\n"

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        name = Path(str(argv[0])).name if argv else ""
        if name.startswith("qemu-system"):
            raise AssertionError(f"spawned qemu-system: {argv}")
        if name == "scp":
            dest = Path(str(argv[-1]))
            if self.write_png_on_scp and dest.suffix == ".png":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(PNG)
            return _completed(0)
        remote = str(argv[-1]) if argv else ""
        self.remote.append(remote)
        if "command -v grim" in remote:
            return _completed(
                self.which_grim,
                stdout="" if self.which_grim else "/usr/bin/grim\n",
            )
        if "command -v gnome-screenshot" in remote:
            return _completed(
                self.which_screenshot,
                stdout="" if self.which_screenshot else "/usr/bin/gnome-screenshot\n",
            )
        if "smoke-session.sh" in remote:
            return _completed(self.session_code, stdout=self.session_stdout)
        if "gnome-screenshot" in remote:
            return _completed(0)
        if " grim " in f" {remote} " or remote.strip().startswith("grim "):
            return _completed(0)
        if "smoke-install.sh" in remote or "install-arch.sh" in remote:
            return _completed(
                0,
                stdout=(
                    f"INSTALL_SH_SHA256={self.install_digest}\n"
                    "Installed Strata v9.9.9\n"
                ),
            )
        if "Exec=" in remote or STRATA_BUS_NAME + ".desktop" in remote:
            return _completed(0, stdout=self.exec_line)
        if "test -x" in remote:
            return _completed(0)
        if "--version" in remote and "strata" in remote:
            return _completed(0, stdout=self.version_stdout)
        if "gtk-launch" in remote or "gio launch" in remote:
            return _completed(0)
        if "hyprctl clients" in remote:
            return _completed(self.hyprctl_code, stdout='{"class":"ok"}\n')
        if "NameHasOwner" in remote:
            return _completed(0, stdout=self.gdbus_stdout)
        return _completed(0)


class ParseOracleTests(unittest.TestCase):
    def test_name_has_owner_true_and_false(self) -> None:
        self.assertTrue(parse_name_has_owner("(true,)\n"))
        self.assertFalse(parse_name_has_owner("(false,)\n"))
        with self.assertRaises(SessionSmokeError):
            parse_name_has_owner("garbage")

    def test_screenshot_tool_missing_maps_to_golden_bug(self) -> None:
        self.assertIn("rebuild the golden", SCREENSHOT_TOOL_MISSING)

    def test_session_exports(self) -> None:
        env = parse_session_exports(
            "SESSION_ID=c1\nWAYLAND_DISPLAY=wayland-0\n# comment\n"
        )
        self.assertEqual(env["SESSION_ID"], "c1")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")

    def test_ubuntu_compositor_is_gnome_shell(self) -> None:
        guest = load_guest("ubuntu-2404")
        self.assertEqual(compositor_process_name(guest), "gnome-shell")
        self.assertEqual(screenshot_tool_for_compositor("gnome-shell"), "gnome-screenshot")

    def test_fedora_compositor_is_gnome_shell(self) -> None:
        guest = load_guest("fedora-workstation")
        self.assertEqual(compositor_process_name(guest), "gnome-shell")
        self.assertTrue(supports_install_from_release(guest))

    def test_arch_compositor_is_hyprland_grim(self) -> None:
        guest = load_guest("arch")
        self.assertEqual(compositor_process_name(guest), "Hyprland")
        self.assertEqual(screenshot_tool_for_compositor("Hyprland"), "grim")
        self.assertTrue(supports_install_from_release(guest))
        self.assertIn("hyprctl clients", hyprctl_class_oracle_command())
        self.assertIn(STRATA_BUS_NAME, hyprctl_class_oracle_command())

    def test_omarchy4_compositor_is_hyprland_grim(self) -> None:
        guest = load_guest("omarchy-4")
        self.assertEqual(compositor_process_name(guest), "Hyprland")
        self.assertTrue(supports_install_from_release(guest))

    def test_omarchy3_compositor_is_hyprland_grim(self) -> None:
        guest = load_guest("omarchy-3")
        self.assertEqual(compositor_process_name(guest), "Hyprland")
        self.assertTrue(supports_install_from_release(guest))

    def test_missing_golden_message_matches_design(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        self.assertEqual(
            msg, "run `mise run image-build -- ubuntu-2404` first"
        )
        self.assertEqual(
            missing_golden_message("fedora-workstation"),
            "run `mise run image-build -- fedora-workstation` first",
        )

    def test_session_only_forbids_install_and_window_oracles(self) -> None:
        self.assertTrue(command_is_forbidden_for_session_only("gtk-launch io.github.lgse.Strata"))
        self.assertTrue(command_is_forbidden_for_session_only("bash install.sh"))
        self.assertTrue(
            command_is_forbidden_for_session_only("strata --version")
        )
        self.assertTrue(
            command_is_forbidden_for_session_only(
                "gdbus call --session --method org.freedesktop.DBus.NameHasOwner "
                "io.github.lgse.Strata"
            )
        )
        self.assertFalse(
            command_is_forbidden_for_session_only(
                "SMOKE_COMPOSITOR=gnome-shell bash /tmp/smoke-session.sh"
            )
        )


class SessionOnlyDriveTests(unittest.TestCase):
    def test_session_only_does_not_wait_on_bus_or_launch(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps = run_session_only_steps(
                machine,
                compositor="gnome-shell",
                screenshot_dest=dest,
                qmp_dest=tmp / "qmp.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
            names = [s["name"] for s in steps]
            self.assertEqual(names, ["session", "screenshot"])
            blob = "\n".join(commands)
            self.assertIn("smoke-session.sh", blob)
            self.assertIn("gnome-screenshot", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)
            self.assertNotIn(STRATA_BUS_NAME, blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            assert_session_only_commands(commands)
            self.assertTrue(dest.is_file())
            self.assertTrue(dest.read_bytes().startswith(b"\x89PNG"))

    def test_gnome_screenshot_timeout_falls_back_to_vnc(self) -> None:
        fake = _FakeRun()

        def timeout_run(argv, **kwargs):
            remote = str(argv[-1]) if argv else ""
            if "gnome-screenshot" in remote and "command -v" not in remote:
                raise subprocess.TimeoutExpired(argv, 15)
            return fake(argv, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            dest = tmp / "shot.png"

            def vnc_ok(path, *, display):
                self.assertEqual(display, 5901)
                dest_path = Path(path)
                dest_path.write_bytes(PNG)
                return True

            capture_guest_screenshot(
                machine,
                {"WAYLAND_DISPLAY": "wayland-0"},
                dest,
                run=timeout_run,
                vnc_capture_fn=vnc_ok,
            )
            self.assertTrue(dest.is_file())
            self.assertTrue(dest.read_bytes().startswith(b"\x89PNG"))

    def test_missing_screenshot_tool_golden_bug_string(self) -> None:
        fake = _FakeRun()
        fake.which_screenshot = 1
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                capture_guest_screenshot(
                    machine,
                    {"WAYLAND_DISPLAY": "wayland-0"},
                    tmp / "shot.png",
                    run=fake,
                )
        self.assertEqual(str(ctx.exception), SCREENSHOT_TOOL_MISSING)

    def test_wait_gnome_bus_is_separate_from_session_only(self) -> None:
        fake = _FakeRun()
        fake.gdbus_stdout = "(true,)\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            wait_gnome_bus_name(
                machine,
                timeout=2,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
        self.assertTrue(any("NameHasOwner" in c for c in commands))
        self.assertIn(STRATA_BUS_NAME, gdbus_name_has_owner_command())
        self.assertTrue(
            any("NameHasOwner" in part for part in GDBUS_NAME_HAS_OWNER_ARGV)
        )

    def test_qmp_no_surface_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            machine = Machine(tmp / "o.qcow2", tmp, ssh_port=1)
            with patch(
                "strataqemu.tests_spec.qmp_screendump", return_value=False
            ) as dump:
                ok = extra_qmp_screendump(machine, tmp / "q.png")
            self.assertFalse(ok)
            dump.assert_called()

    def test_scripts_exist(self) -> None:
        self.assertTrue(smoke_session_script().is_file())
        self.assertTrue(smoke_desktop_script().is_file())
        self.assertTrue(smoke_install_script().is_file())
        self.assertTrue(os.access(smoke_install_script(), os.X_OK))
        desktop = smoke_desktop_script().read_text(encoding="utf-8")
        self.assertIn("NameHasOwner", desktop)
        self.assertIn("grim", desktop)
        self.assertIn("hyprctl clients", desktop)
        body = [
            line
            for line in desktop.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("gtk-launch" in line for line in body))

    def test_session_only_arch_uses_grim_not_install(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps = run_session_only_steps(
                machine,
                compositor="Hyprland",
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
            )
            names = [s["name"] for s in steps]
            self.assertEqual(names, ["session", "screenshot"])
            blob = "\n".join(commands)
            self.assertIn("grim", blob)
            self.assertNotIn("gnome-screenshot", blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)
            assert_session_only_commands(commands)

    def test_missing_grim_golden_bug_string(self) -> None:
        fake = _FakeRun()
        fake.which_grim = 1
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                capture_guest_screenshot(
                    machine,
                    {"WAYLAND_DISPLAY": "wayland-0"},
                    tmp / "shot.png",
                    tool="grim",
                    run=fake,
                )
        self.assertEqual(str(ctx.exception), SCREENSHOT_TOOL_MISSING)

    def test_install_from_release_steps_include_version_hyprland(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("arch"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        self.assertEqual(extras["install_sh_sha256"], fake.install_digest)
        self.assertEqual(extras["intended_version"], "0.9.0")
        self.assertEqual(extras["observed_version"], "0.9.0")
        self.assertNotEqual(extras["observed_version"], "9.9.9")
        self.assertEqual(extras["install_method"], "install.sh")
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertIn("gtk-launch", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertIn("grim", blob)
        self.assertIn(STRATA_VERSION_COMMAND, blob)
        self.assertNotIn("NameHasOwner", blob)
        self.assertNotIn("--archive", blob)
        helper = smoke_install_script().read_text(encoding="utf-8")
        self.assertIn("--non-interactive", helper)
        self.assertIn("--with-desktop-entry", helper)
        self.assertIn("--without-file-chooser", helper)
        self.assertIn(fake.install_digest, extras["install_sh_sha256"])

    def test_install_from_missing_grim_before_window_wait(self) -> None:
        fake = _FakeRun()
        fake.which_grim = 1
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            with self.assertRaises(SessionSmokeError) as ctx:
                run_install_from_release_steps(
                    machine,
                    guest=load_guest("arch"),
                    screenshot_dest=tmp / "shot.png",
                    session_timeout=5,
                    run=fake,
                    commands=commands,
                    sleep=lambda _s: None,
                    intended_version="0.9.0",
                )
        self.assertEqual(str(ctx.exception), SCREENSHOT_TOOL_MISSING)
        blob = "\n".join(commands)
        self.assertNotIn("gtk-launch", blob)
        self.assertNotIn("hyprctl clients", blob)


    def test_ubuntu_install_from_release_uses_gnome_bus_name(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("ubuntu-2404"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertIn(STRATA_VERSION_COMMAND, blob)
        self.assertIn("NameHasOwner", blob)
        self.assertIn(STRATA_BUS_NAME, blob)
        self.assertIn("gtk-launch", blob)
        self.assertIn("gnome-screenshot", blob)
        self.assertNotIn("hyprctl clients", blob)
        self.assertNotIn("--archive", blob)
        self.assertEqual(extras["observed_version"], "0.9.0")
        self.assertEqual(extras["intended_version"], "0.9.0")
        self.assertNotIn("archive_sha256", extras)

    def test_fedora_install_from_release_uses_gnome_bus_name(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("fedora-workstation"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="v0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("NameHasOwner", blob)
        self.assertIn("gnome-screenshot", blob)
        self.assertNotIn("hyprctl clients", blob)
        self.assertEqual(extras["intended_version"], "0.9.0")
        self.assertEqual(extras["observed_version"], "0.9.0")

    def test_local_archive_scps_host_path_and_passes_archive_flag(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            archive = tmp / "strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz"
            payload = b"fixture-archive-bytes"
            archive.write_bytes(payload)
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("ubuntu-2404"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                install_from="local-archive",
                archive_path=archive,
            )
            names = [s["name"] for s in steps]
            self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
            blob = "\n".join(commands)
            self.assertIn("--archive", blob)
            self.assertIn(GUEST_ARCHIVE_REMOTE, blob)
            self.assertIn(str(archive), blob)
            self.assertIn("smoke-install.sh", blob)
            self.assertEqual(extras["archive_sha256"], sha256_file(archive))
            self.assertEqual(
                extras["archive_sha256"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(extras["intended_version"], "0.9.0")
            self.assertEqual(extras["observed_version"], "0.9.0")

    def test_local_archive_missing_path_fail_closed(self) -> None:
        fake = _FakeRun()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_install_from_release_steps(
                    machine,
                    guest=load_guest("ubuntu-2404"),
                    screenshot_dest=tmp / "shot.png",
                    run=fake,
                    install_from="local-archive",
                    intended_version="0.9.0",
                )
        self.assertIn("archive", str(ctx.exception).lower())
        self.assertNotIn("not implemented", str(ctx.exception))

    def test_version_mismatch_fails(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "0.1.0\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2", tmp, ssh_port=22022, identity=identity
            )
            with self.assertRaises(SessionSmokeError) as ctx:
                run_install_from_release_steps(
                    machine,
                    guest=load_guest("arch"),
                    screenshot_dest=tmp / "shot.png",
                    session_timeout=5,
                    run=fake,
                    sleep=lambda _s: None,
                    intended_version="0.9.0",
                )
        self.assertIn("version mismatch", str(ctx.exception))
        self.assertIn("0.9.0", str(ctx.exception))
        self.assertIn("0.1.0", str(ctx.exception))


class VersionParseTests(unittest.TestCase):
    def test_observed_version_strips_prefix(self) -> None:
        self.assertEqual(parse_observed_version("0.9.0\n"), "0.9.0")
        self.assertEqual(parse_observed_version("strata 0.9.0\n"), "0.9.0")
        self.assertEqual(parse_observed_version("v0.9.0\n"), "0.9.0")
        self.assertTrue(versions_match("v0.9.0", "0.9.0"))
        with self.assertRaises(SessionSmokeError):
            parse_observed_version("\n")

    def test_archive_version_from_filename(self) -> None:
        self.assertEqual(
            parse_archive_version(
                "/tmp/strata-0.9.0-x86_64-unknown-linux-gnu.tar.gz"
            ),
            "0.9.0",
        )
        self.assertEqual(
            parse_archive_version("strata-v1.2.3-aarch64-unknown-linux-gnu.tar.gz"),
            "1.2.3",
        )
        self.assertEqual(
            parse_archive_version("strata-0.9.0-rc.1-x86_64-unknown-linux-gnu.tar.gz"),
            "0.9.0-rc.1",
        )
        with self.assertRaises(SessionSmokeError):
            parse_archive_version("/tmp/not-an-archive.tar.gz")

    def test_github_latest_version_uses_injected_opener(self) -> None:
        class _Resp:
            def __init__(self) -> None:
                self._url = (
                    "https://github.com/lgse/strata/releases/tag/v1.2.3"
                )

            def read(self) -> bytes:
                return b'{"tag_name":"v1.2.3"}'

            def geturl(self) -> str:
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        def opener(_req, timeout=15):
            del timeout
            return _Resp()

        self.assertEqual(github_latest_version(opener=opener), "1.2.3")

    def test_install_argv_helpers(self) -> None:
        self.assertEqual(
            install_sh_argv(),
            list(INSTALL_SH_FLAGS),
        )
        argv = install_sh_argv(archive=GUEST_ARCHIVE_REMOTE)
        self.assertEqual(argv[:3], list(INSTALL_SH_FLAGS))
        self.assertEqual(argv[3:], ["--archive", GUEST_ARCHIVE_REMOTE])
        cmd = install_smoke_command(archive=GUEST_ARCHIVE_REMOTE)
        self.assertIn("--archive", cmd)
        self.assertIn(GUEST_ARCHIVE_REMOTE, cmd)
        self.assertIn("smoke-install.sh", cmd)
        arch_cmd = install_smoke_command(forbid_omarchy=True)
        self.assertIn("SMOKE_FORBID_OMARCHY=1", arch_cmd)
        omarchy_cmd = install_smoke_command(forbid_omarchy=False)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", omarchy_cmd)
        self.assertEqual(
            list(INSTALL_SH_FLAGS),
            [
                "--non-interactive",
                "--with-desktop-entry",
                "--without-file-chooser",
            ],
        )
        self.assertNotIn("--with-omarchy-keybinds", INSTALL_SH_FLAGS)
        self.assertNotIn("--with-omarchy-keybinds", install_sh_argv())
        self.assertTrue(strata_version_is_cli(0, "0.9.0\n"))
        self.assertTrue(strata_version_is_cli(0, "strata 1.2.3\n"))
        self.assertFalse(strata_version_is_cli(0, ""))
        self.assertFalse(
            strata_version_is_cli(0, "Gtk-Message: Failed to connect\n")
        )


class InstallArchHelperTests(unittest.TestCase):
    def test_helper_saves_then_execs_existing_flags(self) -> None:
        helper = install_arch_script()
        self.assertTrue(helper.is_file())
        text = helper.read_text(encoding="utf-8")
        self.assertIn("https://raw.githubusercontent.com/lgse/strata/main/install.sh", text)
        self.assertIn("--non-interactive", text)
        self.assertIn("--with-desktop-entry", text)
        self.assertIn("--without-file-chooser", text)
        self.assertIn("INSTALL_SH_SHA256", text)
        commands = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("|" in line and "bash" in line for line in commands))
        self.assertNotIn("strata --version", text)
        self.assertIn("omarchy unexpectedly present", text)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            record = tmp / "install-argv"
            home = tmp / "home"
            home.mkdir()
            dest = tmp / "downloaded-install.sh"

            curl = bindir / "curl"
            curl.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                "dest=\"\"\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  if [[ \"$1\" == \"-o\" ]]; then dest=\"$2\"; shift 2; continue; fi\n"
                "  shift\n"
                "done\n"
                "cat > \"$dest\" <<'INNER'\n"
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"${INSTALL_ARGV_RECORD:?}\"\n"
                "mkdir -p \"$HOME/.local/bin\"\n"
                "echo stub > \"$HOME/.local/bin/strata\"\n"
                "chmod +x \"$HOME/.local/bin/strata\"\n"
                "INNER\n"
                "chmod +x \"$dest\"\n",
                encoding="utf-8",
            )
            curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
            for name in ("sha256sum", "awk", "bash", "chmod", "mkdir", "cat", "echo"):
                found = shutil.which(name)
                self.assertIsNotNone(found, name)
                assert found is not None
                os.symlink(found, bindir / name)

            env = os.environ.copy()
            env["PATH"] = str(bindir)
            env["HOME"] = str(home)
            env["INSTALL_SH_DEST"] = str(dest)
            env["INSTALL_ARGV_RECORD"] = str(record)
            env["OMARCHY_SHARE"] = str(tmp / "no-omarchy-share")
            env["OMARCHY_LOCAL"] = str(tmp / "no-omarchy-local")
            bash = shutil.which("bash")
            self.assertIsNotNone(bash)
            assert bash is not None
            proc = subprocess.run(
                [bash, str(helper)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            self.assertEqual(parse_install_sh_sha256(proc.stdout), digest)
            argv = record.read_text(encoding="utf-8").split()
            self.assertEqual(
                argv,
                [
                    "--non-interactive",
                    "--with-desktop-entry",
                    "--without-file-chooser",
                ],
            )
            self.assertTrue((home / ".local/bin/strata").is_file())


class Omarchy4InstallFromTests(unittest.TestCase):
    def test_install_from_release_hyprland_oracle_existing_flags(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertNotIn("--with-omarchy-keybinds", blob)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertIn("grim", blob)
        self.assertIn("gtk-launch", blob)
        self.assertNotIn("NameHasOwner", blob)
        self.assertEqual(extras["install_method"], "install.sh")

    def test_version_step_skipped_when_not_cli(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "Gtk-Message: Failed to open display\n"
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-4"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        version = [s for s in steps if s["name"] == "version"][0]
        self.assertEqual(version["status"], "skip")
        self.assertIn("not a CLI", version["reason"])
        self.assertNotIn("observed_version", extras)
        self.assertIn("session", [s["name"] for s in steps])
        self.assertIn("install", [s["name"] for s in steps])
        self.assertIn("window", [s["name"] for s in steps])


class Omarchy3InstallFromTests(unittest.TestCase):
    def test_install_from_release_hyprland_oracle_existing_flags(self) -> None:
        fake = _FakeRun()
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            dest = tmp / "screenshot.png"
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-3"),
                screenshot_dest=dest,
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        names = [s["name"] for s in steps]
        self.assertEqual(list(names), list(INSTALL_FROM_RELEASE_STEPS))
        blob = "\n".join(commands)
        self.assertIn("smoke-install.sh", blob)
        self.assertIn("--non-interactive", blob)
        self.assertIn("--with-desktop-entry", blob)
        self.assertIn("--without-file-chooser", blob)
        self.assertNotIn("--with-omarchy-keybinds", blob)
        self.assertNotIn("SMOKE_FORBID_OMARCHY", blob)
        self.assertIn("hyprctl clients", blob)
        self.assertIn("grim", blob)
        self.assertIn("gtk-launch", blob)
        self.assertNotIn("NameHasOwner", blob)
        self.assertEqual(extras["install_method"], "install.sh")

    def test_version_step_skipped_when_not_cli(self) -> None:
        fake = _FakeRun()
        fake.version_stdout = "Gtk-Message: Failed to open display\n"
        fake.session_stdout += "HYPRLAND_INSTANCE_SIGNATURE=sig\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "overlay.qcow2",
                tmp,
                ssh_port=22022,
                identity=identity,
            )
            commands: list[str] = []
            steps, extras = run_install_from_release_steps(
                machine,
                guest=load_guest("omarchy-3"),
                screenshot_dest=tmp / "screenshot.png",
                session_timeout=5,
                run=fake,
                commands=commands,
                sleep=lambda _s: None,
                intended_version="0.9.0",
            )
        version = [s for s in steps if s["name"] == "version"][0]
        self.assertEqual(version["status"], "skip")
        self.assertIn("not a CLI", version["reason"])
        self.assertNotIn("observed_version", extras)
        self.assertIn("session", [s["name"] for s in steps])
        self.assertIn("install", [s["name"] for s in steps])
        self.assertIn("window", [s["name"] for s in steps])


if __name__ == "__main__":
    unittest.main()
