"""Host tests for tests_spec session / GNOME oracle functions. No QEMU VM."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strataqemu.guest import load_guest
from strataqemu.qemu import Machine
from strataqemu.tests_spec import (
    GDBUS_NAME_HAS_OWNER_ARGV,
    SCREENSHOT_TOOL_MISSING,
    SESSION_ONLY_FORBIDDEN,
    STRATA_BUS_NAME,
    SessionSmokeError,
    assert_session_only_commands,
    capture_guest_screenshot,
    command_is_forbidden_for_session_only,
    compositor_process_name,
    extra_qmp_screendump,
    gdbus_name_has_owner_command,
    missing_golden_message,
    parse_name_has_owner,
    parse_session_exports,
    run_session_only_steps,
    screenshot_tool_missing,
    session_only_step_names,
    smoke_desktop_script,
    smoke_session_script,
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
        self.session_stdout = (
            "SESSION_ID=c1\n"
            "XDG_RUNTIME_DIR=/run/user/1000\n"
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
            "WAYLAND_DISPLAY=wayland-0\n"
        )
        self.session_code = 0
        self.gdbus_stdout = "(false,)\n"
        self.write_png_on_scp = True

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
        if "command -v gnome-screenshot" in remote:
            return _completed(
                self.which_screenshot,
                stdout="" if self.which_screenshot else "/usr/bin/gnome-screenshot\n",
            )
        if "smoke-session.sh" in remote:
            return _completed(self.session_code, stdout=self.session_stdout)
        if "gnome-screenshot" in remote:
            return _completed(0)
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
        self.assertTrue(screenshot_tool_missing(1))
        self.assertFalse(screenshot_tool_missing(0))
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
        self.assertEqual(compositor_process_name("ubuntu-2404"), "gnome-shell")

    def test_missing_golden_message_matches_design(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        self.assertEqual(
            msg, "run `mise run image-build -- ubuntu-2404` first"
        )

    def test_session_only_steps_are_session_and_screenshot(self) -> None:
        self.assertEqual(session_only_step_names(), ("session", "screenshot"))
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
        desktop = smoke_desktop_script().read_text(encoding="utf-8")
        self.assertIn("NameHasOwner", desktop)
        body = [
            line
            for line in desktop.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(any("gtk-launch" in line for line in body))


if __name__ == "__main__":
    unittest.main()
