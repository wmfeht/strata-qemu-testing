"""Spike QEMU/overlay argv and fail-closed wiring. No qemu-system-x86_64 spawn."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from strataqemu.cli import CheckHostResult, main
from strataqemu.overlay import create_overlay_argv
from strataqemu.qemu import qmp_screendump, qmp_send_key
from strataqemu.spike import (
    _cloudinit_user_data,
    cidata_xorriso_argv,
    cloud_init_is_done,
    run_spike_wayland_ubuntu,
    spike_overlay_create_argv,
    spike_qemu_argv,
)


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class SpikeOverlayArgvTests(unittest.TestCase):
    def test_strict_absolute_backing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            backing = tmp / "downloads" / "noble-server-cloudimg-amd64.img"
            backing.parent.mkdir(parents=True)
            backing.write_bytes(b"qcow")
            overlay = tmp / "runs" / "r1" / "overlay.qcow2"
            rel_backing = os.path.relpath(backing, os.getcwd())
            argv = spike_overlay_create_argv(rel_backing, overlay)
        self.assertEqual(
            argv,
            create_overlay_argv(backing.resolve(), overlay),
        )


class SpikeQemuArgvTests(unittest.TestCase):
    def test_frozen_gl_localhost_vnc_and_cidata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            overlay = tmp / "run" / "overlay.qcow2"
            run_dir = tmp / "run"
            cidata = tmp / "run" / "cidata.iso"
            ovmf_code = tmp / "OVMF_CODE.4m.fd"
            ovmf_vars = tmp / "run" / "OVMF_VARS.fd"
            with patch("subprocess.run") as run:
                argv = spike_qemu_argv(
                    overlay=overlay,
                    run_dir=run_dir,
                    ssh_port=22022,
                    vnc_port=5901,
                    ovmf_code=ovmf_code,
                    ovmf_vars=ovmf_vars,
                    cidata_iso=cidata,
                )
            run.assert_not_called()

        self.assertEqual(argv[0], "qemu-system-x86_64")
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-gpu-gl-pci", argv)
        self.assertNotIn("virtio-vga-gl", argv)
        self.assertEqual(_after(argv, "-display"), "egl-headless,gl=on")
        vnc = _after(argv, "-vnc")
        self.assertEqual(vnc, "127.0.0.1:5901")
        self.assertFalse(any("0.0.0.0" in tok for tok in argv))
        self.assertIn("virtio-scsi-pci,id=scsi0", argv)
        self.assertTrue(any("scsi-cd" in tok for tok in argv))
        self.assertIn(str(cidata.resolve()), " ".join(argv))
        self.assertIn("cache=unsafe", " ".join(argv))

    def test_cloud_init_done_accepts_recoverable_exit_2(self) -> None:
        self.assertTrue(cloud_init_is_done(2, "status: done\n"))
        self.assertTrue(cloud_init_is_done(0, "status: done\n"))
        self.assertFalse(cloud_init_is_done(1, "status: error\n"))

    def test_user_data_quotes_sudo_for_cloud_init_yaml(self) -> None:
        text = _cloudinit_user_data("ssh-ed25519 AAAA testhost")
        self.assertTrue(text.startswith("#cloud-config\n"))
        self.assertIn("sudo: 'ALL=(ALL) NOPASSWD: ALL'", text)
        self.assertNotIn("    sudo: ALL=(ALL) NOPASSWD: ALL\n", text)
        self.assertIn("ssh-ed25519 AAAA testhost", text)

    def test_cidata_iso_volume_id(self) -> None:
        argv = cidata_xorriso_argv("/tmp/cidata.iso")
        self.assertEqual(argv[0], "xorriso")
        self.assertEqual(_after(argv, "-V"), "cidata")
        self.assertIn("user-data", argv)
        self.assertIn("meta-data", argv)
        self.assertNotEqual(argv[-1], "/tmp/cidata-src")


class SpikeCheckHostFailClosedTests(unittest.TestCase):
    def test_run_does_not_spawn_qemu_when_check_host_fails(self) -> None:
        err = io.StringIO()
        with patch(
            "strataqemu.spike.check_host",
            return_value=CheckHostResult(ok=False, errors=("no kvm",)),
        ), patch("subprocess.Popen") as popen, redirect_stderr(err):
            code = run_spike_wayland_ubuntu()
        self.assertEqual(code, 1)
        popen.assert_not_called()
        self.assertIn("no kvm", err.getvalue())

    def test_cli_dispatches_keep_flag(self) -> None:
        with patch(
            "strataqemu.spike.run_spike_wayland_ubuntu", return_value=0
        ) as run:
            code = main(["spike-wayland-ubuntu", "--keep"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(keep=True)


class QmpScreendumpExtraTests(unittest.TestCase):
    def test_missing_socket_returns_false_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "qmp-session.png"
            ok = qmp_screendump(Path(td) / "missing.sock", dest)
        self.assertFalse(ok)

    def test_no_surface_returns_false_not_raise(self) -> None:
        import json
        import socket
        import threading

        with tempfile.TemporaryDirectory() as td:
            sock_path = str(Path(td) / "qmp.sock")
            dest = Path(td) / "qmp-session.png"
            ready = threading.Event()
            received: list[dict] = []

            def _line(conn: socket.socket) -> dict:
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                return json.loads(bytes(buf).split(b"\n", 1)[0])

            def server() -> None:
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(sock_path)
                srv.listen(1)
                srv.settimeout(3)
                ready.set()
                conn, _ = srv.accept()
                try:
                    conn.sendall(
                        b'{"QMP": {"version": {"qemu": {"major": 9}},'
                        b' "capabilities": []}}\n'
                    )
                    received.append(_line(conn))
                    conn.sendall(b'{"return": {}}\n')
                    received.append(_line(conn))
                    conn.sendall(
                        b'{"error": {"class": "GenericError",'
                        b' "desc": "no surface"}}\n'
                    )
                finally:
                    conn.close()
                    srv.close()

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(3))
            ok = qmp_screendump(sock_path, dest)
            thread.join(3)
        self.assertFalse(ok)
        self.assertEqual(received[0]["execute"], "qmp_capabilities")
        self.assertEqual(received[1]["execute"], "screendump")
        self.assertFalse(dest.exists() and dest.stat().st_size > 0)


class QmpSendKeyTests(unittest.TestCase):
    def test_send_key_chords_ctrl_comma(self) -> None:
        import json
        import socket
        import threading

        with tempfile.TemporaryDirectory() as td:
            sock_path = str(Path(td) / "qmp.sock")
            ready = threading.Event()
            received: list[dict] = []

            def _line(conn: socket.socket) -> dict:
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                return json.loads(bytes(buf).split(b"\n", 1)[0])

            def server() -> None:
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(sock_path)
                srv.listen(1)
                srv.settimeout(3)
                ready.set()
                conn, _ = srv.accept()
                try:
                    conn.sendall(
                        b'{"QMP": {"version": {"qemu": {"major": 9}},'
                        b' "capabilities": []}}\n'
                    )
                    received.append(_line(conn))
                    conn.sendall(b'{"return": {}}\n')
                    received.append(_line(conn))
                    conn.sendall(b'{"return": {}}\n')
                finally:
                    conn.close()
                    srv.close()

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(3))
            ok = qmp_send_key(sock_path, ["ctrl", "comma"])
            thread.join(3)
        self.assertTrue(ok)
        self.assertEqual(received[0]["execute"], "qmp_capabilities")
        self.assertEqual(received[1]["execute"], "send-key")
        keys = received[1]["arguments"]["keys"]
        self.assertEqual(
            keys,
            [
                {"type": "qcode", "data": "ctrl"},
                {"type": "qcode", "data": "comma"},
            ],
        )

    def test_missing_socket_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ok = qmp_send_key(Path(td) / "missing.sock", ["ctrl", "comma"])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
