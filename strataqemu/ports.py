"""Localhost SSH/VNC port allocation. Never bind 0.0.0.0."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import TypeVar

# Design: bind 127.0.0.1:0; if the OS will not hand out an ephemeral port,
# walk these inclusive ranges. Never listen on all interfaces.
BIND_HOST = "127.0.0.1"
SSH_FALLBACK_START = 22022
SSH_FALLBACK_END = 22999
VNC_FALLBACK_START = 5900
VNC_FALLBACK_END = 5999
SSH_FALLBACK = range(SSH_FALLBACK_START, SSH_FALLBACK_END + 1)
VNC_FALLBACK = range(VNC_FALLBACK_START, VNC_FALLBACK_END + 1)
# Retry a full SSH+VNC allocation this many times if QEMU reports EADDRINUSE.
MAX_PORT_RETRIES = 5

_Bind = Callable[[str, int], tuple[socket.socket, int]]
T = TypeVar("T")


class AddressAlreadyInUse(Exception):
    """QEMU (or bind) failed because a forwarded port was taken."""


def is_address_already_in_use(text: str) -> bool:
    return "address already in use" in text.lower()


def bind_localhost(host: str = BIND_HOST, port: int = 0) -> tuple[socket.socket, int]:
    """Bind ``host:port`` and return ``(socket, chosen_port)``. Caller owns the socket.

    Rejects any host other than 127.0.0.1 so tests/VNC never land on 0.0.0.0.
    """
    if host != BIND_HOST:
        raise ValueError(f"refusing to bind {host!r}; localhost only ({BIND_HOST})")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock, int(sock.getsockname()[1])


def _bind_and_release(
    host: str,
    port: int,
    bind: _Bind,
) -> int:
    sock, chosen = bind(host, port)
    try:
        return chosen
    finally:
        sock.close()


def allocate_port(
    host: str = BIND_HOST,
    *,
    fallback: range | None = None,
    bind: _Bind | None = None,
) -> int:
    """Bind ``host:0``, record the port, close. On ephemeral failure, walk fallback."""
    binder = bind if bind is not None else bind_localhost
    if host != BIND_HOST:
        raise ValueError(f"refusing to bind {host!r}; localhost only ({BIND_HOST})")
    try:
        return _bind_and_release(host, 0, binder)
    except OSError:
        if fallback is None:
            raise
        for candidate in fallback:
            try:
                return _bind_and_release(host, candidate, binder)
            except OSError:
                continue
        raise RuntimeError(
            f"no free port on {host} in fallback "
            f"{fallback.start}–{fallback.stop - 1}"
        ) from None


def allocate_ssh_port(*, bind: _Bind | None = None) -> int:
    return allocate_port(fallback=SSH_FALLBACK, bind=bind)


def allocate_vnc_port(*, bind: _Bind | None = None) -> int:
    """Allocate a QEMU VNC *display* number.

    QEMU ``-vnc 127.0.0.1:DISPLAY`` listens on TCP ``5900+DISPLAY``. An
    ephemeral bind above ``65535-5900`` would overflow, so those are
    rejected in favor of the 5900-5999 fallback range.
    """
    binder = bind if bind is not None else bind_localhost
    try:
        port = _bind_and_release(BIND_HOST, 0, binder)
        if 0 < port and port + 5900 <= 65535:
            return port
    except OSError:
        pass
    last: OSError | None = None
    for candidate in VNC_FALLBACK:
        try:
            return _bind_and_release(BIND_HOST, candidate, binder)
        except OSError as exc:
            last = exc
            continue
    if last is not None:
        raise last
    raise OSError("could not allocate a VNC display")


def retry_on_addr_in_use(
    func: Callable[[], T],
    *,
    attempts: int = MAX_PORT_RETRIES,
) -> T:
    """Call ``func`` up to ``attempts`` times if it raises AddressAlreadyInUse."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last: AddressAlreadyInUse | None = None
    for _ in range(attempts):
        try:
            return func()
        except AddressAlreadyInUse as exc:
            last = exc
    assert last is not None
    raise last
