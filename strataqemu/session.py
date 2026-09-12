"""Pick the graphical loginctl session and export Wayland / D-Bus / Hypr env.

Pure functions over ``loginctl`` text and runtime-dir listings so tests inject
samples without SSH or QEMU. After ``ssh tester@guest``, logind has two
sessions; ``$XDG_SESSION_ID`` is the SSH ``Type=tty`` (or unspecified) session.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class SessionError(Exception):
    """Fail-closed session targeting."""


@dataclass(frozen=True)
class ListedSession:
    """One row of ``loginctl --no-legend list-sessions``.

    Columns: SESSION UID USER SEAT TTY
    """

    sid: str
    uid: int
    user: str
    seat: str = ""
    tty: str = ""


@dataclass(frozen=True)
class FsEntry:
    """One name in a runtime dir (or hypr instance dir) listing."""

    name: str
    is_socket: bool = False
    is_dir: bool = False
    mtime: float = 0.0


@dataclass(frozen=True)
class GraphicalSession:
    """Selected wayland seat0 session plus env exports for later SSH commands."""

    sid: str
    uid: int
    env: dict[str, str]


def parse_list_sessions(text: str) -> list[ListedSession]:
    """Parse ``loginctl --no-legend list-sessions`` (SESSION UID USER SEAT TTY)."""
    rows: list[ListedSession] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("session"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        sid = parts[0]
        try:
            uid = int(parts[1])
        except ValueError:
            continue
        user = parts[2] if len(parts) > 2 else ""
        seat = parts[3] if len(parts) > 3 and parts[3] != "-" else ""
        tty = parts[4] if len(parts) > 4 and parts[4] != "-" else ""
        rows.append(
            ListedSession(sid=sid, uid=uid, user=user, seat=seat, tty=tty)
        )
    return rows


def parse_show_session(text: str) -> dict[str, str]:
    """Parse ``loginctl show-session`` property text (``Type=wayland`` lines)."""
    props: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key] = value
    return props


def select_wayland_session(
    list_sessions_text: str,
    show_session_by_sid: Mapping[str, str],
    uid: int,
    *,
    ssh_session_id: str | None = None,
) -> str:
    """Return the active ``Type=wayland`` ``Class=user`` ``Seat=seat0`` sid.

    Never returns ``ssh_session_id`` (the SSH ``$XDG_SESSION_ID``).
    ``Type=x11`` is a hard fail.
    """
    chosen: str | None = None
    for row in parse_list_sessions(list_sessions_text):
        if row.uid != uid:
            continue
        raw = show_session_by_sid.get(row.sid)
        if raw is None:
            continue
        props = parse_show_session(raw)
        typ = props.get("Type", "").strip().lower()
        if typ == "x11":
            raise SessionError(f"Type=x11 is a hard fail (session {row.sid})")
        class_ = props.get("Class", "").strip().lower()
        state = props.get("State", "").strip().lower()
        seat = (props.get("Seat", "") or row.seat).strip()
        if not (
            typ == "wayland"
            and class_ == "user"
            and state == "active"
            and seat == "seat0"
        ):
            continue
        if ssh_session_id is not None and row.sid == ssh_session_id:
            continue
        if chosen is None:
            chosen = row.sid
    if chosen is None:
        raise SessionError("no active wayland seat0 session")
    if ssh_session_id is not None and chosen == ssh_session_id:
        raise SessionError("refusing SSH $XDG_SESSION_ID")
    return chosen


def _as_entries(
    source: Path | Sequence[FsEntry],
    *,
    subdir: str | None = None,
) -> list[FsEntry]:
    if not isinstance(source, Path):
        return list(source)
    root = source / subdir if subdir else source
    if not root.is_dir():
        return []
    entries: list[FsEntry] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            st = path.lstat()
        except OSError:
            continue
        entries.append(
            FsEntry(
                name=path.name,
                is_socket=stat.S_ISSOCK(st.st_mode),
                is_dir=stat.S_ISDIR(st.st_mode),
                mtime=st.st_mtime,
            )
        )
    return entries


def choose_wayland_display(source: Path | Sequence[FsEntry]) -> str:
    """First ``wayland-*`` that is a socket, not a leftover lock. Basename only."""
    for entry in _as_entries(source):
        if not entry.name.startswith("wayland-"):
            continue
        if entry.is_socket:
            return entry.name
    raise SessionError("no wayland socket")


def choose_hyprland_instance(source: Path | Sequence[FsEntry]) -> str | None:
    """Newest hypr instance dir name, or None if ``hypr/`` is absent/empty.

    ``source`` is a runtime dir (looks at ``hypr/``) or a listing of instance
    dirs. Newest wins by mtime (same idea as omarchy-restart-shell).
    """
    if isinstance(source, Path):
        entries = _as_entries(source, subdir="hypr")
    else:
        entries = list(source)
    dirs = [entry for entry in entries if entry.is_dir]
    if not dirs:
        return None
    return max(dirs, key=lambda entry: entry.mtime).name


def session_env(
    uid: int,
    *,
    wayland_display: str,
    hyprland_instance: str | None = None,
) -> dict[str, str]:
    """Guest env exports. ``XDG_RUNTIME_DIR`` is always ``/run/user/<uid>``."""
    runtime = f"/run/user/{uid}"
    env = {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "WAYLAND_DISPLAY": wayland_display,
    }
    if hyprland_instance:
        env["HYPRLAND_INSTANCE_SIGNATURE"] = hyprland_instance
    return env


def target_graphical_session(
    *,
    uid: int,
    list_sessions_text: str,
    show_session_by_sid: Mapping[str, str],
    runtime_dir: Path | None = None,
    runtime_entries: Sequence[FsEntry] | None = None,
    hypr_entries: Sequence[FsEntry] | None = None,
    ssh_session_id: str | None = None,
) -> GraphicalSession:
    """Select the wayland session and build env from a runtime listing or path."""
    sid = select_wayland_session(
        list_sessions_text,
        show_session_by_sid,
        uid,
        ssh_session_id=ssh_session_id,
    )
    if runtime_dir is not None:
        display = choose_wayland_display(runtime_dir)
        hypr = choose_hyprland_instance(runtime_dir)
    else:
        if runtime_entries is None:
            raise SessionError("no wayland socket")
        display = choose_wayland_display(runtime_entries)
        hypr = choose_hyprland_instance(hypr_entries or ())
    return GraphicalSession(
        sid=sid,
        uid=uid,
        env=session_env(
            uid,
            wayland_display=display,
            hyprland_instance=hypr,
        ),
    )
