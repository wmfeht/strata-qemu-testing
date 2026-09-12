"""Port allocator, OpenSSH argv, and shutdown cascade. No guest, no QEMU VM."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from strataqemu.ports import (
    BIND_HOST,
    AddressAlreadyInUse,
    allocate_port,
    allocate_ssh_port,
    allocate_vnc_port,
    bind_localhost,
    is_address_already_in_use,
    retry_on_addr_in_use,
)
from strataqemu.qemu import (
    Machine,
    ShutdownHooks,
    qga_guest_shutdown,
    qmp_system_powerdown,
    run_shutdown,
)
from strataqemu.ssh import (
    scp_command,
    scp_download_command,
    ssh_command,
    ssh_poweroff_command,
)


class PortAllocatorTests(unittest.TestCase):
    def test_rejects_all_interfaces(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            allocate_port(host="0.0.0.0")
        self.assertIn("127.0.0.1", str(ctx.exception))
        with self.assertRaises(ValueError):
            bind_localhost("0.0.0.0", 0)

    def test_two_held_allocations_differ(self) -> None:
        held: list[socket.socket] = []
        try:
            s1, p1 = bind_localhost()
            held.append(s1)
            s2, p2 = bind_localhost()
            held.append(s2)
            self.assertNotEqual(p1, p2)
            self.assertGreater(p1, 0)
            self.assertGreater(p2, 0)
        finally:
            for sock in held:
                sock.close()

    def test_allocate_port_binds_localhost_only(self) -> None:
        port = allocate_port()
        self.assertGreater(port, 0)
        # The allocator closed the socket; we can bind the recorded port on
        # 127.0.0.1. Binding the same port on 0.0.0.0 is a different concern;
        # the API refused 0.0.0.0 as the allocation host.
        sock, got = bind_localhost(BIND_HOST, port)
        try:
            self.assertEqual(got, port)
            self.assertEqual(sock.getsockname()[0], "127.0.0.1")
        finally:
            sock.close()

    def test_ssh_fallback_used_when_ephemeral_denied(self) -> None:
        seen: list[int] = []

        def bind(host: str, port: int):
            self.assertEqual(host, "127.0.0.1")
            seen.append(port)
            if port == 0:
                raise OSError("ephemeral denied")

            class _Fake:
                def getsockname(self):
                    return (host, port)

                def close(self):
                    return None

            return _Fake(), port

        port = allocate_ssh_port(bind=bind)
        self.assertEqual(port, 22022)
        self.assertEqual(seen[0], 0)
        self.assertEqual(seen[1], 22022)

    def test_vnc_fallback_used_when_ephemeral_denied(self) -> None:
        seen: list[int] = []

        def bind(host: str, port: int):
            self.assertEqual(host, "127.0.0.1")
            seen.append(port)
            if port == 0:
                raise OSError("ephemeral denied")

            class _Fake:
                def getsockname(self):
                    return (host, port)

                def close(self):
                    return None

            return _Fake(), port

        port = allocate_vnc_port(bind=bind)
        self.assertEqual(port, 5900)
        self.assertEqual(seen[1], 5900)

    def test_retry_five_times_then_succeeds_or_raises(self) -> None:
        self.assertTrue(is_address_already_in_use("Address already in use"))
        n = {"c": 0}

        def flaky():
            n["c"] += 1
            if n["c"] < 3:
                raise AddressAlreadyInUse("address already in use")
            return "ok"

        self.assertEqual(retry_on_addr_in_use(flaky), "ok")
        self.assertEqual(n["c"], 3)

        n["c"] = 0

        def always():
            n["c"] += 1
            raise AddressAlreadyInUse("address already in use")

        with self.assertRaises(AddressAlreadyInUse):
            retry_on_addr_in_use(always)
        self.assertEqual(n["c"], 5)


class OpenSshArgvTests(unittest.TestCase):
    def test_ssh_default_pty_false_no_t_flag(self) -> None:
        identity = Path("/tmp/id_ed25519")
        argv = ssh_command(
            port=22022,
            identity=identity,
            remote_command="true",
        )
        self.assertEqual(argv[0], "ssh")
        self.assertNotIn("-t", argv)
        self.assertNotIn("-tt", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "22022")
        self.assertEqual(argv[argv.index("-i") + 1], str(identity))
        self.assertIn("tester@127.0.0.1", argv)
        self.assertIn("true", argv)
        argv_default = ssh_command(port=1, identity=identity)
        self.assertNotIn("-tt", argv_default)

    def test_ssh_pty_true_adds_tt(self) -> None:
        argv = ssh_command(
            port=22,
            identity="/tmp/k",
            remote_command="bash",
            pty=True,
        )
        self.assertIn("-tt", argv)
        self.assertEqual(argv[0], "ssh")

    def test_scp_uses_capital_p(self) -> None:
        argv = scp_command(
            port=22022,
            identity="/tmp/k",
            source="/tmp/a",
            destination="/tmp/b",
        )
        self.assertEqual(argv[0], "scp")
        self.assertEqual(argv[argv.index("-P") + 1], "22022")
        self.assertIn("tester@127.0.0.1:/tmp/b", argv)
        self.assertNotIn("paramiko", " ".join(argv).lower())

    def test_scp_download_pulls_remote_to_local(self) -> None:
        argv = scp_download_command(
            port=22022,
            identity="/tmp/k",
            remote_path="/tmp/strata-window.png",
            local_path="/tmp/screendump-session.png",
        )
        self.assertEqual(argv[0], "scp")
        self.assertEqual(argv[argv.index("-P") + 1], "22022")
        self.assertIn("tester@127.0.0.1:/tmp/strata-window.png", argv)
        self.assertIn("/tmp/screendump-session.png", argv)
        self.assertNotIn("paramiko", " ".join(argv).lower())

    def test_poweroff_command_is_systemctl(self) -> None:
        argv = ssh_poweroff_command(port=22022, identity="/tmp/k")
        self.assertIn("sudo systemctl poweroff", argv)
        self.assertNotIn("-tt", argv)

    def test_machine_ssh_defaults_pty_false(self) -> None:
        m = Machine(
            overlay="/tmp/o.qcow2",
            run_dir="/tmp/run",
            ssh_port=22022,
            vnc_port=5900,
            identity="/tmp/k",
        )
        argv = m.ssh("echo hi")
        self.assertNotIn("-t", argv)
        self.assertNotIn("-tt", argv)


class ShutdownCascadeTests(unittest.TestCase):
    def test_qga_success_does_not_kill(self) -> None:
        killed: list[str] = []

        def boom(name: str):
            def _() -> bool:
                raise AssertionError(f"{name} must not run")

            return _

        path = run_shutdown(
            ShutdownHooks(
                guest_shutdown=lambda: True,
                ssh_poweroff=boom("ssh"),
                acpi_power_button=boom("acpi"),
                kill=lambda: killed.append("kill"),
            )
        )
        self.assertEqual(path, "qga")
        self.assertEqual(killed, [])

    def test_qga_fail_ssh_success_does_not_kill(self) -> None:
        killed: list[str] = []
        path = run_shutdown(
            ShutdownHooks(
                guest_shutdown=lambda: False,
                ssh_poweroff=lambda: True,
                acpi_power_button=lambda: (_ for _ in ()).throw(
                    AssertionError("acpi must not run")
                ),
                kill=lambda: killed.append("kill"),
            )
        )
        self.assertEqual(path, "ssh")
        self.assertEqual(killed, [])

    def test_all_fail_then_kill(self) -> None:
        killed: list[str] = []
        path = run_shutdown(
            ShutdownHooks(
                guest_shutdown=lambda: False,
                ssh_poweroff=lambda: False,
                acpi_power_button=lambda: False,
                kill=lambda: killed.append("kill"),
            )
        )
        self.assertEqual(path, "kill")
        self.assertEqual(killed, ["kill"])

    def test_machine_shutdown_uses_injected_hooks(self) -> None:
        killed: list[str] = []
        m = Machine(
            overlay="/tmp/o.qcow2",
            run_dir="/tmp/run",
            ssh_port=1,
            vnc_port=2,
        )
        path = m.shutdown(
            hooks=ShutdownHooks(
                guest_shutdown=lambda: True,
                ssh_poweroff=lambda: False,
                acpi_power_button=lambda: False,
                kill=lambda: killed.append("kill"),
            )
        )
        self.assertEqual(path, "qga")
        self.assertEqual(killed, [])


class QgaAndQmpProtocolTests(unittest.TestCase):
    def test_qga_sends_guest_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sock_path = str(Path(td) / "qga.sock")
            received: list[dict] = []
            ready = threading.Event()

            def server() -> None:
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(sock_path)
                srv.listen(1)
                srv.settimeout(3)
                ready.set()
                conn, _ = srv.accept()
                try:
                    buf = conn.recv(4096)
                    received.append(json.loads(buf.split(b"\n", 1)[0]))
                    conn.sendall(b'{"return": {}}\n')
                finally:
                    conn.close()
                    srv.close()

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(3))
            ok = qga_guest_shutdown(sock_path)
            thread.join(3)
        self.assertTrue(ok)
        self.assertEqual(received, [{"execute": "guest-shutdown"}])

    def test_qmp_sends_system_powerdown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sock_path = str(Path(td) / "qmp.sock")
            received: list[dict] = []
            ready = threading.Event()

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
            ok = qmp_system_powerdown(sock_path)
            thread.join(3)
        self.assertTrue(ok)
        self.assertEqual(received[0]["execute"], "qmp_capabilities")
        self.assertEqual(received[1]["execute"], "system_powerdown")

    def test_qga_missing_socket_returns_false(self) -> None:
        self.assertFalse(qga_guest_shutdown("/tmp/no-such-qga.sock"))

    def test_qga_hangup_after_execute_is_success(self) -> None:
        """Guest often drops the agent before a JSON return."""
        with tempfile.TemporaryDirectory() as td:
            sock_path = str(Path(td) / "qga.sock")
            received: list[dict] = []
            ready = threading.Event()

            def server() -> None:
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(sock_path)
                srv.listen(1)
                srv.settimeout(3)
                ready.set()
                conn, _ = srv.accept()
                try:
                    buf = conn.recv(4096)
                    received.append(json.loads(buf.split(b"\n", 1)[0]))
                finally:
                    conn.close()
                    srv.close()

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(3))
            ok = qga_guest_shutdown(sock_path)
            thread.join(3)
        self.assertTrue(ok)
        self.assertEqual(received, [{"execute": "guest-shutdown"}])


if __name__ == "__main__":
    unittest.main()
