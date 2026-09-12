"""OpenSSH client argv (subprocess, not paramiko)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

DEFAULT_USER = "tester"
DEFAULT_HOST = "127.0.0.1"
POWEROFF_COMMAND = "sudo systemctl poweroff"


def openssh_opts(identity: Path | str) -> list[str]:
    """Common ``ssh``/``scp`` options for throwaway guests."""
    return [
        "-i",
        str(identity),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
    ]


def ssh_command(
    *,
    port: int,
    identity: Path | str,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
    remote_command: str | Sequence[str] | None = None,
    pty: bool = False,
) -> list[str]:
    """Build an ``ssh`` argv. ``pty=False`` is the default (no ``-t``)."""
    argv: list[str] = ["ssh"]
    if pty:
        # Force a remote TTY even when stdin is not a tty (mise/CI).
        argv.append("-tt")
    argv.extend(openssh_opts(identity))
    argv.extend(["-p", str(port), f"{user}@{host}"])
    if remote_command is None:
        return argv
    if isinstance(remote_command, str):
        argv.append(remote_command)
    else:
        argv.extend(remote_command)
    return argv


def scp_command(
    *,
    port: int,
    identity: Path | str,
    source: str,
    destination: str,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
) -> list[str]:
    """Build an ``scp`` argv. Remote paths are ``user@host:path`` if unadorned."""
    dest = destination
    if ":" not in dest:
        dest = f"{user}@{host}:{dest}"
    return [
        "scp",
        *openssh_opts(identity),
        "-P",
        str(port),
        source,
        dest,
    ]


def ssh_poweroff_command(
    *,
    port: int,
    identity: Path | str,
    host: str = DEFAULT_HOST,
    user: str = DEFAULT_USER,
) -> list[str]:
    return ssh_command(
        port=port,
        identity=identity,
        host=host,
        user=user,
        remote_command=POWEROFF_COMMAND,
        pty=False,
    )
