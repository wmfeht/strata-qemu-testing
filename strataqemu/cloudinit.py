"""NoCloud cidata ISO writer (xorriso volume id ``cidata``)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SSH_KEY_PLACEHOLDER = "{{SSH_AUTHORIZED_KEY}}"
VOLUME_ID = "cidata"
TOKEN_NEEDLES = (
    "ghp_",
    "github_pat_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "gh auth",
    "tskey-",
    "TS_AUTHKEY",
    "tailscale_authkey",
    "TAILSCALE",
)


class CloudInitError(ValueError):
    """Fail-closed seed render / write error."""


def xorriso_argv(dest: Path | str) -> list[str]:
    """``xorriso`` argv; run with cwd containing ``user-data`` and ``meta-data``."""
    return [
        "xorriso",
        "-as",
        "mkisofs",
        "-R",
        "-V",
        VOLUME_ID,
        "-o",
        str(Path(dest)),
        "user-data",
        "meta-data",
    ]


def render_user_data(template: str, pubkey: str) -> str:
    if SSH_KEY_PLACEHOLDER not in template:
        raise CloudInitError(
            f"user-data template missing {SSH_KEY_PLACEHOLDER}"
        )
    rendered = template.replace(SSH_KEY_PLACEHOLDER, pubkey.strip())
    blob = rendered.lower()
    for needle in TOKEN_NEEDLES:
        if needle.lower() in blob:
            raise CloudInitError(
                f"rendered user-data contains forbidden token {needle!r}"
            )
    return rendered


def meta_data_text(*, instance_id: str, hostname: str) -> str:
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"


def write_seed_files(
    work_dir: Path,
    *,
    user_data: str,
    instance_id: str,
    hostname: str,
) -> tuple[Path, Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    user_path = work_dir / "user-data"
    meta_path = work_dir / "meta-data"
    user_path.write_text(user_data, encoding="utf-8")
    meta_path.write_text(
        meta_data_text(instance_id=instance_id, hostname=hostname),
        encoding="utf-8",
    )
    return user_path, meta_path


def write_cidata_iso(
    dest: Path | str,
    *,
    template: str,
    pubkey: str,
    instance_id: str,
    hostname: str,
    xorriso: str | None = None,
) -> Path:
    """Write a NoCloud seed ISO with volume id ``cidata``."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    work = dest_path.parent / "cidata-src"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    user_data = render_user_data(template, pubkey)
    write_seed_files(
        work,
        user_data=user_data,
        instance_id=instance_id,
        hostname=hostname,
    )
    binary = xorriso or shutil.which("xorriso") or "xorriso"
    argv = xorriso_argv(dest_path.resolve())
    argv[0] = binary
    subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        cwd=work,
    )
    return dest_path


def write_cidata_iso_from_recipe(
    dest: Path | str,
    *,
    recipe_dir: Path,
    pubkey: str,
    instance_id: str,
    hostname: str,
) -> Path:
    tmpl = Path(recipe_dir) / "user-data.yaml.tmpl"
    template = tmpl.read_text(encoding="utf-8")
    return write_cidata_iso(
        dest,
        template=template,
        pubkey=pubkey,
        instance_id=instance_id,
        hostname=hostname,
    )
