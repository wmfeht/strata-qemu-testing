"""run-test --session-only and vm-run. No KVM; refuse qemu-system-*."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu import config
from strataqemu.cli import CheckHostResult, build_parser, main
from strataqemu.guest import load_guest
from strataqemu.image_build import golden_qcow2
from strataqemu.overlay import create_overlay_argv
from strataqemu.qemu import Machine
from strataqemu.run_test import (
    choose_graphical_ui,
    discard_throwaway_disks,
    require_golden,
    run_run_test,
    run_test_qemu_argv,
    run_vm_run,
    vm_run_qemu_argv,
)
from strataqemu.tests_spec import (
    INSTALL_FROM_FAIL_CLOSED,
    missing_golden_message,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _all_after(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag]


def _refuse_qemu_system(cmd, *args, **kwargs):
    name = Path(str(cmd[0])).name if cmd else ""
    if name.startswith("qemu-system"):
        raise AssertionError(f"spawned qemu-system: {cmd}")
    raise AssertionError(f"unexpected subprocess: {cmd}")


class _DummyProc:
    def __init__(self) -> None:
        self.returncode = 0

    def poll(self) -> None:
        return None

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        return None


class _FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        name = Path(str(argv[0])).name if argv else ""
        if name.startswith("qemu-system"):
            raise AssertionError(f"spawned qemu-system: {argv}")
        if name == "scp":
            dest = Path(str(argv[-1]))
            if dest.suffix == ".png":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(PNG)
            return subprocess.CompletedProcess(argv, 0, "", "")
        remote = str(argv[-1]) if argv else ""
        if "command -v grim" in remote:
            return subprocess.CompletedProcess(argv, 0, "/usr/bin/grim\n", "")
        if "command -v gnome-screenshot" in remote:
            return subprocess.CompletedProcess(
                argv, 0, "/usr/bin/gnome-screenshot\n", ""
            )
        if "smoke-session.sh" in remote:
            return subprocess.CompletedProcess(
                argv,
                0,
                "SESSION_ID=c1\n"
                "XDG_RUNTIME_DIR=/run/user/1000\n"
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
                "WAYLAND_DISPLAY=wayland-0\n"
                "HYPRLAND_INSTANCE_SIGNATURE=sig\n",
                "",
            )
        if "install-arch.sh" in remote:
            digest = "ab" * 32
            return subprocess.CompletedProcess(
                argv, 0, f"INSTALL_SH_SHA256={digest}\n", ""
            )
        if "Exec=" in remote or "Strata.desktop" in remote:
            return subprocess.CompletedProcess(
                argv, 0, "Exec=/home/tester/.local/bin/strata %U\n", ""
            )
        if "gtk-launch" in remote or "gio launch" in remote:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "hyprctl clients" in remote:
            return subprocess.CompletedProcess(argv, 0, "{}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _host_ok(tmp: Path) -> CheckHostResult:
    code = tmp / "OVMF_CODE.4m.fd"
    code.write_bytes(b"fw")
    key = tmp / "id_ed25519"
    key.write_text("k", encoding="utf-8")
    key.with_name(key.name + ".pub").write_text("p", encoding="utf-8")
    return CheckHostResult(ok=True, errors=(), ovmf_code=code, ssh_key=key)


def _cache_with_golden(tmp: Path, guest_id: str = "ubuntu-2404") -> tuple[Path, Path]:
    guest = load_guest(guest_id)
    cache = tmp / "cache"
    golden = golden_qcow2(guest, cache)
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_bytes(b"golden-bytes")
    golden.chmod(0o444)
    return cache, golden


class MissingGoldenTests(unittest.TestCase):
    def test_run_test_session_only_missing_golden_twice(self) -> None:
        guest = load_guest("ubuntu-2404")
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "ubuntu-2404",
                                "--session-only",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_fedora_session_only_missing_golden_twice(self) -> None:
        guest = load_guest("fedora-workstation")
        msg = missing_golden_message("fedora-workstation")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "fedora-workstation",
                                "--session-only",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_vm_run_missing_golden_same_message(self) -> None:
        msg = missing_golden_message("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            err = io.StringIO()
            try:
                with (
                    redirect_stderr(err),
                    patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                    patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
                    patch("strataqemu.image_build.run_image_build") as build,
                ):
                    code = main(["vm-run", "ubuntu-2404", "--graphical"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 1)
        self.assertIn(msg, err.getvalue())
        popen.assert_not_called()
        run.assert_not_called()
        build.assert_not_called()

    def test_require_golden_does_not_build(self) -> None:
        guest = load_guest("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            with self.assertRaises(Exception) as ctx:
                require_golden(guest, cache)
        self.assertIn("mise run image-build", str(ctx.exception))


class ArgvTests(unittest.TestCase):
    def test_run_test_argv_is_frozen_gl_unsafe_no_vnc_on_second_display(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            overlay = tmp / "run" / "overlay.qcow2"
            argv = run_test_qemu_argv(
                overlay=overlay,
                run_dir=tmp / "run",
                ssh_port=22022,
                vnc_port=5901,
                cpus=4,
                memory_mib=8192,
                ovmf_code=tmp / "OVMF_CODE.4m.fd",
                ovmf_vars=tmp / "run" / "OVMF_VARS.fd",
            )
        self.assertEqual(argv[0], "qemu-system-x86_64")
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-gpu-gl-pci", argv)
        self.assertNotIn("virtio-vga-gl", argv)
        self.assertEqual(_after(argv, "-display"), "egl-headless,gl=on")
        self.assertEqual(_after(argv, "-vnc"), "127.0.0.1:5901")
        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d][0]
        self.assertIn("cache=unsafe", drive0)
        self.assertNotIn("cache=writeback", drive0)
        self.assertFalse(any("cidata" in tok for tok in argv))

    def test_vm_run_graphical_gtk_no_vnc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = vm_run_qemu_argv(
                overlay=tmp / "overlay.qcow2",
                run_dir=tmp / "run",
                ssh_port=22022,
                vnc_port=5901,
                cpus=4,
                memory_mib=8192,
                graphical=True,
                graphical_ui="gtk",
            )
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-vga-gl", argv)
        self.assertNotIn("virtio-gpu-gl-pci", argv)
        self.assertEqual(_after(argv, "-display"), "gtk,gl=on")
        self.assertNotIn("-vnc", argv)
        self.assertEqual(argv.count("-vnc"), 0)
        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d][0]
        self.assertIn("cache=unsafe", drive0)

    def test_vm_run_graphical_sdl_fallback_no_vnc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = vm_run_qemu_argv(
                overlay=tmp / "overlay.qcow2",
                run_dir=tmp / "run",
                ssh_port=22022,
                cpus=4,
                memory_mib=8192,
                graphical=True,
                graphical_ui="sdl",
            )
        self.assertEqual(_after(argv, "-display"), "sdl,gl=on")
        self.assertIn("virtio-vga-gl", argv)
        self.assertNotIn("-vnc", argv)

    def test_choose_graphical_ui_prefers_gtk(self) -> None:
        self.assertEqual(
            choose_graphical_ui("gtk            GTK display\nsdl            SDL2\n"),
            "gtk",
        )
        self.assertEqual(choose_graphical_ui("sdl            SDL2\n"), "sdl")

    def test_overlay_argv_strict_absolute_backing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            golden = tmp / "images" / "ubuntu-2404-deadbeef.qcow2"
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"g")
            overlay = tmp / "runs" / "r1" / "overlay.qcow2"
            argv = create_overlay_argv(golden, overlay)
        self.assertIn("backing_file_strict=on", argv)
        self.assertTrue(Path(argv[argv.index("-b") + 1]).is_absolute())
        self.assertEqual(Path(argv[argv.index("-b") + 1]), golden.resolve())

    def test_parser_has_no_maintain(self) -> None:
        parser = build_parser()
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            parser.parse_args(["vm-run", "ubuntu-2404", "--maintain"])
        self.assertIn("maintain", err.getvalue())


class SessionOnlyWiringTests(unittest.TestCase):
    def test_session_only_with_golden_does_not_install_or_spawn_real_qemu(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            self.assertTrue(Path(golden).is_absolute() or Path(golden).exists())
            src = Path(golden)
            self.assertEqual(src.stat().st_mode & 0o222, 0)
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            mtime = golden.stat().st_mtime_ns
            contents = golden.read_bytes()
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "ubuntu-2404",
                    session_only=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(golden.read_bytes(), contents)
            self.assertEqual(golden.stat().st_mtime_ns, mtime)
            self.assertTrue(recorded_argv)
            qemu_argv = recorded_argv[0]
            self.assertEqual(qemu_argv[0], "qemu-system-x86_64")
            self.assertIn("virtio-gpu-gl-pci", qemu_argv)
            self.assertEqual(_after(qemu_argv, "-display"), "egl-headless,gl=on")
            self.assertIn("-vnc", qemu_argv)
            self.assertNotIn("virtio-vga-gl", qemu_argv)
            blob = " ".join(str(c) for c in fake.calls)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)
            self.assertNotIn("io.github.lgse.Strata", blob)
            runs = cache / "runs"
            result_files = list(runs.glob("*/result.json"))
            self.assertTrue(result_files, list(runs.rglob("*")))
            text = result_files[0].read_text(encoding="utf-8")
            self.assertIn('"name": "session"', text)
            self.assertIn('"status": "pass"', text)
            shots = list(runs.glob("*/screenshot.png"))
            self.assertTrue(shots)
            self.assertTrue(shots[0].read_bytes().startswith(b"\x89PNG"))

    def test_success_without_keep_saves_screenshot_drops_overlay(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "ubuntu-2404",
                    session_only=True,
                    keep=False,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            runs = list((cache / "runs").iterdir())
            self.assertEqual(len(runs), 1, runs)
            run_dir = runs[0]
            self.assertTrue((run_dir / "screenshot.png").is_file())
            self.assertTrue(
                (run_dir / "screenshot.png").read_bytes().startswith(b"\x89PNG")
            )
            self.assertTrue((run_dir / "result.json").is_file())
            self.assertFalse((run_dir / "overlay.qcow2").exists())
            self.assertFalse((run_dir / "OVMF_VARS.fd").exists())
            out = buf.getvalue()
            self.assertIn("screenshot:", out)
            self.assertIn(str(run_dir / "screenshot.png"), out)
            self.assertIn(str(run_dir), out)
            self.assertTrue((run_dir / "screenshot.png").exists())

    def test_discard_throwaway_keeps_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            (run_dir / "overlay.qcow2").write_bytes(b"disk")
            (run_dir / "OVMF_VARS.fd").write_bytes(b"vars")
            (run_dir / "qmp.sock").write_bytes(b"")
            (run_dir / "qga.sock").write_bytes(b"")
            (run_dir / "screenshot.png").write_bytes(PNG)
            (run_dir / "result.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "serial.log").write_text("boot\n", encoding="utf-8")
            discard_throwaway_disks(run_dir)
            self.assertFalse((run_dir / "overlay.qcow2").exists())
            self.assertFalse((run_dir / "OVMF_VARS.fd").exists())
            self.assertTrue((run_dir / "screenshot.png").is_file())
            self.assertTrue((run_dir / "result.json").is_file())
            self.assertTrue((run_dir / "serial.log").is_file())

    def test_vm_run_graphical_records_gtk_argv(self) -> None:
        recorded: list[list[str]] = []

        def popen(argv, **kwargs):
            recorded.append(list(argv))
            return _DummyProc()

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp)
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value="kill"),
            ):
                code = run_vm_run(
                    "ubuntu-2404",
                    graphical=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                    graphical_ui="gtk",
                    wait=False,
                )
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(recorded)
        argv = recorded[0]
        self.assertIn("virtio-vga-gl", argv)
        self.assertEqual(_after(argv, "-display"), "gtk,gl=on")
        self.assertNotIn("-vnc", argv)


class HelpAndMiseTests(unittest.TestCase):
    def test_run_test_help_has_session_only(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["run-test", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--session-only", text)
        self.assertIn("--keep", text)
        self.assertIn("--install-from", text)
        self.assertIn("release", text)
        self.assertIn("local-archive", text)
        self.assertNotIn("not implemented", text)

    def test_vm_run_help_has_graphical(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["vm-run", "--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue() + err.getvalue()
        self.assertIn("--graphical", text)
        self.assertIn("--keep", text)
        self.assertNotIn("not implemented", text)
        self.assertNotIn("--maintain", text)


class ArchInstallFromTests(unittest.TestCase):
    def test_missing_golden_install_from_release_twice(self) -> None:
        guest = load_guest("arch")
        msg = missing_golden_message("arch")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty-cache"
            cache.mkdir()
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            texts = []
            try:
                for _ in range(2):
                    buf = io.StringIO()
                    err = io.StringIO()
                    with (
                        redirect_stdout(buf),
                        redirect_stderr(err),
                        patch(
                            "subprocess.Popen", side_effect=_refuse_qemu_system
                        ) as popen,
                        patch(
                            "subprocess.run", side_effect=_refuse_qemu_system
                        ) as run,
                        patch(
                            "strataqemu.image_build.run_image_build"
                        ) as build,
                        patch(
                            "strataqemu.image_build.build_live"
                        ) as live,
                    ):
                        code = main(
                            [
                                "run-test",
                                "--",
                                "arch",
                                "--install-from",
                                "release",
                            ]
                        )
                    self.assertEqual(code, 1)
                    text = buf.getvalue() + err.getvalue()
                    texts.append(text)
                    self.assertIn(msg, text)
                    self.assertNotIn("not implemented", text)
                    popen.assert_not_called()
                    run.assert_not_called()
                    build.assert_not_called()
                    live.assert_not_called()
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(texts[0], texts[1])
        self.assertFalse(golden_qcow2(guest, cache).exists())

    def test_ubuntu_install_from_release_fail_closed(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(["run-test", "--", "ubuntu-2404", "--install-from", "release"])
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertIn(INSTALL_FROM_FAIL_CLOSED, text)
        popen.assert_not_called()
        run.assert_not_called()

    def test_fedora_install_from_release_fail_closed(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
            patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
        ):
            code = main(
                ["run-test", "--", "fedora-workstation", "--install-from", "release"]
            )
        self.assertEqual(code, 2)
        text = buf.getvalue() + err.getvalue()
        self.assertIn(INSTALL_FROM_FAIL_CLOSED, text)
        popen.assert_not_called()
        run.assert_not_called()

    def test_arch_local_archive_fail_closed(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with (
            redirect_stdout(buf),
            redirect_stderr(err),
            patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
        ):
            code = main(
                ["run-test", "--", "arch", "--install-from", "local-archive"]
            )
        self.assertEqual(code, 2)
        self.assertIn(INSTALL_FROM_FAIL_CLOSED, buf.getvalue() + err.getvalue())
        popen.assert_not_called()

    def test_install_from_release_records_helper_not_version(self) -> None:
        fake = _FakeRun()
        recorded_argv: list[list[str]] = []

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                recorded_argv.append(list(argv))
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, golden = _cache_with_golden(tmp, "arch")
            host = _host_ok(tmp)
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value="kill"),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "arch",
                    install_from="release",
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue(recorded_argv)
            qemu_argv = recorded_argv[0]
            self.assertNotIn("if=pflash", " ".join(qemu_argv))
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("install-arch.sh", blob)
            self.assertIn("gtk-launch", blob)
            self.assertIn("hyprctl clients", blob)
            self.assertNotIn("strata --version", blob)
            helper = (
                Path(__file__).resolve().parents[1]
                / "images"
                / "common"
                / "install-arch.sh"
            ).read_text(encoding="utf-8")
            self.assertIn("--non-interactive", helper)
            self.assertIn("--with-desktop-entry", helper)
            self.assertIn("--without-file-chooser", helper)
            result_files = list((cache / "runs").glob("*/result.json"))
            self.assertTrue(result_files)
            text = result_files[0].read_text(encoding="utf-8")
            self.assertIn('"name": "session"', text)
            self.assertIn('"name": "install"', text)
            self.assertIn('"name": "desktop-entry"', text)
            self.assertIn('"name": "window"', text)
            self.assertNotIn('"name": "version"', text)
            self.assertIn("install_sh_sha256", text)
            self.assertIn("abababab", text)
            del golden

    def test_session_only_arch_does_not_install(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "arch")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch.object(Machine, "shutdown", return_value=None),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "arch",
                    session_only=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("grim", blob)
            self.assertNotIn("install-arch.sh", blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)

    def test_session_only_fedora_uses_gnome_screenshot_not_install(self) -> None:
        fake = _FakeRun()

        def popen(argv, **kwargs):
            name = Path(str(argv[0])).name if argv else ""
            if name.startswith("qemu-system"):
                return _DummyProc()
            raise AssertionError(f"unexpected Popen: {argv}")

        def fake_overlay(golden, overlay, **kwargs):
            dest = Path(overlay)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"overlay")
            return dest

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache, _golden = _cache_with_golden(tmp, "fedora-workstation")
            vars_template = tmp / "OVMF_VARS.4m.fd"
            vars_template.write_bytes(b"vars")
            host = _host_ok(tmp)
            err = io.StringIO()
            with (
                redirect_stderr(err),
                patch(
                    "strataqemu.run_test.find_ovmf_vars",
                    return_value=vars_template,
                ),
                patch.object(Machine, "shutdown", return_value=None),
                patch(
                    "strataqemu.tests_spec.qmp_screendump", return_value=False
                ),
            ):
                code = run_run_test(
                    "fedora-workstation",
                    session_only=True,
                    keep=True,
                    cache_dir=cache,
                    check_host_fn=lambda: host,
                    run=fake,
                    popen=popen,
                    create_overlay_fn=fake_overlay,
                    settle_s=0,
                )
            self.assertEqual(code, 0, err.getvalue())
            blob = " ".join(str(c) for c in fake.calls)
            self.assertIn("gnome-screenshot", blob)
            self.assertNotIn("install.sh", blob)
            self.assertNotIn("strata --version", blob)
            self.assertNotIn("gtk-launch", blob)
            self.assertNotIn("NameHasOwner", blob)


if __name__ == "__main__":
    unittest.main()
