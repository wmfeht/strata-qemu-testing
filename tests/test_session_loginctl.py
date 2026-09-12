"""Session targeting against mixed tty+wayland loginctl fixtures.

No KVM, no QEMU, no SSH. Drives the shipped functions; does not re-implement
the selector.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from strataqemu.session import (
    SessionError,
    choose_hyprland_instance,
    choose_wayland_display,
    select_wayland_session,
    target_graphical_session,
)

# Representative `loginctl --no-legend list-sessions` (SESSION UID USER SEAT TTY).
# c1 is the seat0 graphical session; c2 is the SSH login.
MIXED_LIST = """\
c1 1000 tester seat0 tty2
c2 1000 tester - pts/0
"""

WAYLAND_SHOW = """\
Id=c1
User=1000
Name=tester
Seat=seat0
TTY=tty2
Remote=no
Type=wayland
Class=user
Active=yes
State=active
"""

TTY_SHOW = """\
Id=c2
User=1000
Name=tester
Seat=
TTY=pts/0
Remote=yes
Type=tty
Class=user
Active=yes
State=active
"""

UNSPECIFIED_SHOW = """\
Id=c2
User=1000
Name=tester
Type=unspecified
Class=user
State=active
Seat=
"""

X11_SHOW = """\
Id=c1
User=1000
Name=tester
Seat=seat0
TTY=tty2
Type=x11
Class=user
State=active
"""

UID = 1000
SSH_SID = "c2"
WAYLAND_SID = "c1"


def _unix_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if path.exists():
        path.unlink()
    sock.bind(str(path))
    return sock


class SelectWaylandSessionTests(unittest.TestCase):
    def test_mixed_tty_and_wayland_selects_wayland_not_ssh(self) -> None:
        sid = select_wayland_session(
            MIXED_LIST,
            {WAYLAND_SID: WAYLAND_SHOW, SSH_SID: TTY_SHOW},
            UID,
            ssh_session_id=SSH_SID,
        )
        self.assertEqual(sid, WAYLAND_SID)
        self.assertNotEqual(sid, SSH_SID)

    def test_unspecified_ssh_session_is_not_selected(self) -> None:
        sid = select_wayland_session(
            MIXED_LIST,
            {WAYLAND_SID: WAYLAND_SHOW, SSH_SID: UNSPECIFIED_SHOW},
            UID,
            ssh_session_id=SSH_SID,
        )
        self.assertEqual(sid, WAYLAND_SID)
        self.assertNotEqual(sid, SSH_SID)

    def test_x11_is_hard_fail(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            select_wayland_session(
                MIXED_LIST,
                {WAYLAND_SID: X11_SHOW, SSH_SID: TTY_SHOW},
                UID,
                ssh_session_id=SSH_SID,
            )
        self.assertIn("x11", str(ctx.exception).lower())

    def test_x11_fails_even_when_wayland_is_also_listed(self) -> None:
        list_text = """\
c1 1000 tester seat0 tty2
c2 1000 tester - pts/0
c3 1000 tester seat0 tty3
"""
        shows = {
            "c1": X11_SHOW.replace("Id=c1", "Id=c1"),
            "c2": TTY_SHOW,
            "c3": WAYLAND_SHOW.replace("Id=c1", "Id=c3").replace("tty2", "tty3"),
        }
        with self.assertRaises(SessionError) as ctx:
            select_wayland_session(list_text, shows, UID, ssh_session_id="c2")
        self.assertIn("x11", str(ctx.exception).lower())

    def test_missing_wayland_fails_closed(self) -> None:
        with self.assertRaises(SessionError) as ctx:
            select_wayland_session(
                MIXED_LIST,
                {SSH_SID: TTY_SHOW},
                UID,
                ssh_session_id=SSH_SID,
            )
        self.assertIn("wayland", str(ctx.exception).lower())


class WaylandDisplayTests(unittest.TestCase):
    def test_socket_basename_not_leftover_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "wayland-0.lock").write_bytes(b"lock")
            sock = _unix_socket(runtime / "wayland-0")
            try:
                name = choose_wayland_display(runtime)
            finally:
                sock.close()
        self.assertEqual(name, "wayland-0")
        self.assertFalse(name.endswith(".lock"))

    def test_lock_only_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "wayland-0.lock").write_bytes(b"lock")
            with self.assertRaises(SessionError) as ctx:
                choose_wayland_display(runtime)
        self.assertIn("wayland socket", str(ctx.exception).lower())


class HyprInstanceTests(unittest.TestCase):
    def test_newest_instance_dir_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            hypr = runtime / "hypr"
            older = hypr / "old_sig_aaaa"
            newer = hypr / "new_sig_bbbb"
            older.mkdir(parents=True)
            newer.mkdir()
            os.utime(older, (10, 10))
            os.utime(newer, (99, 99))
            sig = choose_hyprland_instance(runtime)
        self.assertEqual(sig, "new_sig_bbbb")
        self.assertNotEqual(sig, "old_sig_aaaa")

    def test_absent_hypr_dir_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(choose_hyprland_instance(Path(td)))


class TargetGraphicalSessionTests(unittest.TestCase):
    def test_exports_runtime_dbus_wayland_and_newest_hypr(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "wayland-0.lock").write_bytes(b"lock")
            sock = _unix_socket(runtime / "wayland-0")
            hypr = runtime / "hypr"
            older = hypr / "old_sig_aaaa"
            newer = hypr / "new_sig_bbbb"
            older.mkdir(parents=True)
            newer.mkdir()
            os.utime(older, (10, 10))
            os.utime(newer, (99, 99))
            try:
                session = target_graphical_session(
                    uid=UID,
                    list_sessions_text=MIXED_LIST,
                    show_session_by_sid={
                        WAYLAND_SID: WAYLAND_SHOW,
                        SSH_SID: TTY_SHOW,
                    },
                    runtime_dir=runtime,
                    ssh_session_id=SSH_SID,
                )
            finally:
                sock.close()

        self.assertEqual(session.sid, WAYLAND_SID)
        self.assertNotEqual(session.sid, SSH_SID)
        env = session.env
        self.assertEqual(env["XDG_RUNTIME_DIR"], f"/run/user/{UID}")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"],
            f"unix:path=/run/user/{UID}/bus",
        )
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertNotEqual(env["WAYLAND_DISPLAY"], "wayland-0.lock")
        self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], "new_sig_bbbb")


if __name__ == "__main__":
    unittest.main()
