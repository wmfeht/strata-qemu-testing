"""NoCloud ISO and Omarchy cidata VFAT writers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from strataqemu.guest import ISO_AUTOINSTALL_DISK, ISO_CIDATA_REQUIRED

SSH_KEY_PLACEHOLDER = "{{SSH_AUTHORIZED_KEY}}"
VOLUME_ID = "cidata"
# 4MiB VFAT labeled CIDATA. Attached as a second virtio-blk (not ISO9660).
VFAT_LABEL = "CIDATA"
OMARCHY_CIDATA_BYTES = 4 * 1024 * 1024
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


OMARCHY_CIDATA_PAYLOAD = (
    "user_configuration.json",
    "user_credentials.json",
    "user_encrypt_installation.txt",
    "authorized_keys",
)
FORBIDDEN_OMARCHY_CIDATA = (
    "tailscale_authkey",
    "user-data",
    "meta-data",
)


def omarchy_mkfs_vfat_argv(dest: Path | str) -> list[str]:
    """``mkfs.vfat`` argv for the Omarchy autoinstall seed."""
    return ["mkfs.vfat", "-n", VFAT_LABEL, str(Path(dest))]


def omarchy_mcopy_argv(dest: Path | str) -> list[str]:
    """``mcopy`` argv; run with cwd containing the Omarchy cidata names."""
    return [
        "mcopy",
        "-i",
        str(Path(dest)),
        *OMARCHY_CIDATA_PAYLOAD,
        "::/",
    ]


def _blob_has_forbidden_token(blob: str) -> str | None:
    lowered = blob.lower()
    for needle in TOKEN_NEEDLES:
        if needle.lower() in lowered:
            return needle
    return None


def _validate_omarchy_bootloader_schema(data: dict) -> None:
    """4.x dumps use bootloader_config + omarchy_install; 3.x uses bootloader."""
    bootloader_config = data.get("bootloader_config")
    bootloader = data.get("bootloader")
    omarchy_install = data.get("omarchy_install")
    if isinstance(bootloader_config, dict):
        if not isinstance(omarchy_install, dict):
            raise CloudInitError(
                "user_configuration.json missing omarchy_install"
            )
        if omarchy_install.get("mode") != "full_disk":
            raise CloudInitError("omarchy_install.mode must be full_disk")
        return
    if isinstance(bootloader, str) and bootloader.strip():
        if bootloader != "Limine":
            raise CloudInitError(
                "user_configuration.json bootloader must be Limine"
            )
        if omarchy_install is not None:
            raise CloudInitError(
                "3.x user_configuration.json must not contain omarchy_install"
            )
        return
    raise CloudInitError(
        "user_configuration.json missing bootloader or bootloader_config"
    )


def validate_omarchy_configuration(
    data: dict,
    *,
    disk: str = ISO_AUTOINSTALL_DISK,
) -> None:
    """Fail closed unless this is a complete unencrypted full-disk dump.

    4.x (Quattro) dumps use ``bootloader_config`` plus ``omarchy_install``.
    3.x dumps use a string ``bootloader`` (Limine) and have no
    ``omarchy_install``. Disk target, 1MiB alignment, and no-encryption
    rules are the same for both.
    """
    if not isinstance(data, dict):
        raise CloudInitError("user_configuration.json must be a JSON object")
    if "disk_encryption" in data:
        raise CloudInitError(
            "user_configuration.json must not contain disk_encryption"
        )
    disk_config = data.get("disk_config")
    if not isinstance(disk_config, dict):
        raise CloudInitError("user_configuration.json missing disk_config")
    if "disk_encryption" in disk_config:
        raise CloudInitError(
            "user_configuration.json must not contain disk_encryption"
        )
    if disk_config.get("config_type") != "default_layout":
        raise CloudInitError("disk_config.config_type must be default_layout")
    mods = disk_config.get("device_modifications") or []
    if not isinstance(mods, list) or not mods:
        raise CloudInitError("disk_config.device_modifications is required")
    devices = [
        item.get("device")
        for item in mods
        if isinstance(item, dict)
    ]
    if disk not in devices:
        raise CloudInitError(f"disk_config must target {disk}")
    # archinstall 3.0.9 / 4.4: start and length must be 1MiB-aligned or it
    # raises "Partition is misaligned" while loading the configurator dump.
    mib = 1024 * 1024
    for item in mods:
        if not isinstance(item, dict):
            continue
        for part in item.get("partitions") or []:
            if not isinstance(part, dict) or part.get("status") != "create":
                continue
            for key in ("start", "size"):
                blob = part.get(key)
                if not isinstance(blob, dict) or blob.get("unit") != "B":
                    raise CloudInitError(
                        f"partition {key} must be unit B"
                    )
                value = blob.get("value")
                if not isinstance(value, int) or value % mib:
                    raise CloudInitError(
                        f"partition {key} {value!r} must be 1MiB-aligned"
                    )
    _validate_omarchy_bootloader_schema(data)
    needle = _blob_has_forbidden_token(json.dumps(data))
    if needle is not None:
        raise CloudInitError(
            f"user_configuration.json contains forbidden token {needle!r}"
        )


def validate_omarchy_credentials(data: dict) -> None:
    if not isinstance(data, dict):
        raise CloudInitError("user_credentials.json must be a JSON object")
    if "disk_encryption" in data:
        raise CloudInitError(
            "user_credentials.json must not contain disk_encryption"
        )
    users = data.get("users")
    if not isinstance(users, list) or not users:
        raise CloudInitError("user_credentials.json must list users")
    names = [
        item.get("username")
        for item in users
        if isinstance(item, dict)
    ]
    if "tester" not in names:
        raise CloudInitError("user_credentials.json must include username tester")
    root_hash = data.get("root_enc_password")
    if not isinstance(root_hash, str) or not root_hash.startswith("$6$"):
        raise CloudInitError(
            "user_credentials.json root_enc_password must be an openssl passwd -6 hash"
        )
    needle = _blob_has_forbidden_token(json.dumps(data))
    if needle is not None:
        raise CloudInitError(
            f"user_credentials.json contains forbidden token {needle!r}"
        )


def validate_encrypt_file(text: str) -> None:
    if text.strip() != "false":
        raise CloudInitError(
            "user_encrypt_installation.txt must contain false"
        )


def render_authorized_keys(template: str, pubkey: str) -> str:
    if SSH_KEY_PLACEHOLDER not in template:
        raise CloudInitError(
            f"authorized_keys template missing {SSH_KEY_PLACEHOLDER}"
        )
    rendered = template.replace(SSH_KEY_PLACEHOLDER, pubkey.strip())
    if not rendered.endswith("\n"):
        rendered += "\n"
    needle = _blob_has_forbidden_token(rendered)
    if needle is not None:
        raise CloudInitError(
            f"authorized_keys contains forbidden token {needle!r}"
        )
    return rendered


def write_omarchy_cidata_files(
    work_dir: Path,
    *,
    recipe_dir: Path,
    pubkey: str,
) -> dict[str, Path]:
    """Render Omarchy cidata payload files. No NoCloud user-data/meta-data."""
    src = Path(recipe_dir) / "cidata"
    for name in ISO_CIDATA_REQUIRED:
        if not (src / name).is_file():
            raise CloudInitError(f"missing cidata/{name} in {recipe_dir}")
    for forbidden in FORBIDDEN_OMARCHY_CIDATA:
        if (src / forbidden).exists():
            raise CloudInitError(
                f"omarchy cidata must not include {forbidden}"
            )

    config_text = (src / "user_configuration.json").read_text(encoding="utf-8")
    try:
        config_data = json.loads(config_text)
    except json.JSONDecodeError as exc:
        raise CloudInitError(f"invalid user_configuration.json: {exc}") from exc
    validate_omarchy_configuration(config_data)

    creds_text = (src / "user_credentials.json.tmpl").read_text(encoding="utf-8")
    try:
        creds_data = json.loads(creds_text)
    except json.JSONDecodeError as exc:
        raise CloudInitError(f"invalid user_credentials.json.tmpl: {exc}") from exc
    validate_omarchy_credentials(creds_data)

    encrypt_text = (src / "user_encrypt_installation.txt").read_text(
        encoding="utf-8"
    )
    validate_encrypt_file(encrypt_text)

    keys = render_authorized_keys(
        (src / "authorized_keys.tmpl").read_text(encoding="utf-8"),
        pubkey,
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "user_configuration.json": work_dir / "user_configuration.json",
        "user_credentials.json": work_dir / "user_credentials.json",
        "user_encrypt_installation.txt": work_dir / "user_encrypt_installation.txt",
        "authorized_keys": work_dir / "authorized_keys",
    }
    paths["user_configuration.json"].write_text(config_text, encoding="utf-8")
    paths["user_credentials.json"].write_text(creds_text, encoding="utf-8")
    paths["user_encrypt_installation.txt"].write_text(encrypt_text, encoding="utf-8")
    paths["authorized_keys"].write_text(keys, encoding="utf-8")
    return paths


def write_omarchy_vfat_image(
    dest: Path | str,
    *,
    work_dir: Path,
    mkfs: str | None = None,
    mcopy: str | None = None,
) -> Path:
    """Format ``dest`` as VFAT labeled CIDATA and copy the Omarchy payload."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(b"\0" * OMARCHY_CIDATA_BYTES)
    mkfs_bin = mkfs or shutil.which("mkfs.vfat") or "mkfs.vfat"
    mcopy_bin = mcopy or shutil.which("mcopy") or "mcopy"
    mkfs_argv = omarchy_mkfs_vfat_argv(dest_path.resolve())
    mkfs_argv[0] = mkfs_bin
    mkfs_proc = subprocess.run(
        mkfs_argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if mkfs_proc.returncode != 0:
        raise CloudInitError(
            f"mkfs.vfat failed ({mkfs_proc.returncode}): "
            f"{mkfs_proc.stderr or mkfs_proc.stdout}"
        )
    for name in OMARCHY_CIDATA_PAYLOAD:
        if not (work_dir / name).is_file():
            raise CloudInitError(f"missing {name} in {work_dir}")
    env = os.environ.copy()
    env["MTOOLS_SKIP_CHECK"] = "1"
    mcopy_argv = omarchy_mcopy_argv(dest_path.resolve())
    mcopy_argv[0] = mcopy_bin
    mcopy_proc = subprocess.run(
        mcopy_argv,
        check=False,
        capture_output=True,
        text=True,
        cwd=work_dir,
        env=env,
    )
    if mcopy_proc.returncode != 0:
        raise CloudInitError(
            f"mcopy failed ({mcopy_proc.returncode}): "
            f"{mcopy_proc.stderr or mcopy_proc.stdout}"
        )
    return dest_path


def write_omarchy_cidata_iso(
    dest: Path | str,
    *,
    recipe_dir: Path,
    pubkey: str,
    mkfs: str | None = None,
    mcopy: str | None = None,
) -> Path:
    """Write an Omarchy autoinstall VFAT image labeled ``CIDATA``.

    The live ISO's ``omarchy-cidata-load`` mounts ``/dev/disk/by-label/cidata``.
    Attached as virtio-blk at PCI 0x9 (``/dev/vdb``); the 40G disk is pinned
    at 0x8 so it stays ``/dev/vda``.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    work = dest_path.parent / "cidata-src"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    write_omarchy_cidata_files(work, recipe_dir=recipe_dir, pubkey=pubkey)
    write_omarchy_vfat_image(
        dest_path, work_dir=work, mkfs=mkfs, mcopy=mcopy
    )
    return dest_path
