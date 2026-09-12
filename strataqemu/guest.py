"""Guest recipe loader. Parses ``images/<id>/image.toml``; no QEMU."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomllib

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
IMAGE_NAME_SUFFIXES = (".qcow2", ".img", ".iso", ".raw.xz", ".raw")
DIGEST_BASENAMES = ("image.toml", "bootstrap.sh", "setup.sh")
# Session drop-ins hashed into the golden so greetd/hyprland.lua edits rebuild.
DIGEST_OPTIONAL_BASENAMES = (
    "greetd-config.toml",
    "hyprland.lua",
    "hyprland.conf",
    # 3.8.4 has no omarchy-cidata-load; image-build runs this via qemu-ga.
    "skip-wizard.sh",
)
# ISO autoinstall cidata: both JSON files required or the wizard runs.
ISO_CIDATA_REQUIRED = (
    "user_configuration.json",
    "user_credentials.json.tmpl",
    "user_encrypt_installation.txt",
    "authorized_keys.tmpl",
)
SDDM_WAYLAND_SESSION_CANDIDATES = ("omarchy.desktop", "hyprland-uwsm.desktop")
ISO_AUTOINSTALL_DISK = "/dev/vda"


class GuestError(ValueError):
    """Fail-closed recipe error."""


@dataclass(frozen=True)
class Session:
    kind: str
    display_manager: str
    compositor: str
    autologin: bool
    wayland: bool


@dataclass(frozen=True)
class Packages:
    manager: str
    snapshot_url: str | None
    runtime: tuple[str, ...]


@dataclass(frozen=True)
class User:
    name: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class Cidata:
    disk: str
    encrypt: bool = False


def default_images_root() -> Path:
    return Path(__file__).resolve().parent.parent / "images"


def covered_recipe_files(recipe_dir: Path) -> tuple[Path, ...]:
    """Files hashed into the recipe digest: toml, bootstrap, setup, templates."""
    root = recipe_dir.resolve()
    found: list[Path] = []
    seen: set[Path] = set()
    for name in DIGEST_BASENAMES:
        path = root / name
        if path.is_file():
            found.append(path)
            seen.add(path)
    for name in DIGEST_OPTIONAL_BASENAMES:
        path = root / name
        if path.is_file():
            resolved = path.resolve()
            if resolved in seen:
                continue
            found.append(path)
            seen.add(resolved)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix != ".tmpl" and not path.name.endswith(".tmpl"):
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        found.append(path)
        seen.add(resolved)
    cidata_dir = root / "cidata"
    if cidata_dir.is_dir():
        for path in sorted(cidata_dir.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            found.append(path)
            seen.add(resolved)
    return tuple(found)


def choose_sddm_session(session_files: Sequence[str]) -> str | None:
    """Basename without ``.desktop`` from the frozen probe order."""
    names = {Path(item).name for item in session_files}
    for candidate in SDDM_WAYLAND_SESSION_CANDIDATES:
        if candidate in names:
            return candidate.removesuffix(".desktop")
    return None


def recipe_digest(recipe_dir: Path) -> str:
    """SHA-256 over image.toml + bootstrap.sh + setup.sh + templates + drop-ins."""
    h = hashlib.sha256()
    root = recipe_dir.resolve()
    for path in covered_recipe_files(root):
        rel = path.resolve().relative_to(root).as_posix()
        data = path.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(data)).encode("ascii"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GuestError(f"missing or empty {key!r}")
    return value.strip()


def _require_int(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuestError(f"missing or invalid integer {key!r}")
    return value


def _parse_download_name(data: dict, source_url: str) -> str:
    """Basename written under ``$CACHE/downloads/``.

    File URLs derive it from the path. Directory URLs (Fedora Cloud scrape)
    must set ``source_filename`` in image.toml.
    """
    raw = data.get("source_filename")
    if raw is not None:
        if not isinstance(raw, str) or not raw.strip():
            raise GuestError("source_filename must be a non-empty string")
        name = raw.strip()
        if "/" in name or "\\" in name:
            raise GuestError("source_filename must not contain a path separator")
        return name
    path = source_url.split("?", 1)[0].rstrip("/")
    name = Path(path).name
    lowered = name.lower()
    if any(lowered.endswith(suffix) for suffix in IMAGE_NAME_SUFFIXES):
        return name
    raise GuestError(
        "source_url has no image filename; set source_filename in image.toml"
    )


def _parse_source_sha256(data: dict) -> str:
    if "source_sha256" not in data:
        raise GuestError("source_sha256 is required")
    raw = data["source_sha256"]
    if not isinstance(raw, str) or not raw.strip():
        raise GuestError("source_sha256 is required")
    digest = raw.strip().lower()
    if not SHA256_HEX.fullmatch(digest):
        raise GuestError("source_sha256 must be 64 hex characters")
    return digest


def _parse_id(raw: str) -> str:
    if not raw or "." in raw:
        raise GuestError("guest id must not be empty or contain '.'")
    if "/" in raw or "\\" in raw:
        raise GuestError("guest id must not contain a path separator")
    return raw


@dataclass(frozen=True)
class Guest:
    id: str
    arch: str
    firmware: Literal["uefi", "bios"]
    source_kind: Literal["cloud-image", "iso-autoinstall"]
    source_url: str
    source_sha256: str
    download_name: str
    disk_gb: int
    memory_mib: int
    cpus: int
    build_timeout_s: int
    boot_timeout_s: int
    ovmf_code: str | None
    ovmf_vars_template: str | None
    session: Session
    packages: Packages
    user: User
    cidata: Cidata | None
    recipe_dir: Path

    @classmethod
    def load(cls, recipe_dir: Path) -> Guest:
        recipe_dir = Path(recipe_dir)
        toml_path = recipe_dir / "image.toml"
        if not toml_path.is_file():
            raise GuestError(f"missing image.toml in {recipe_dir}")
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise GuestError(f"invalid image.toml: {exc}") from exc
        if not isinstance(data, dict):
            raise GuestError("image.toml must be a table")

        guest_id = _parse_id(_require_str(data, "id"))
        firmware = _require_str(data, "firmware")
        if firmware not in ("uefi", "bios"):
            raise GuestError("firmware must be 'uefi' or 'bios'")
        source_kind = _require_str(data, "source_kind")
        if source_kind not in ("cloud-image", "iso-autoinstall"):
            raise GuestError(
                "source_kind must be 'cloud-image' or 'iso-autoinstall'"
            )
        source_url = _require_str(data, "source_url")
        if "/current/" in source_url or "/latest/" in source_url:
            raise GuestError(
                "source_url must be a dated tree, not …/current/ or …/latest/"
            )
        source_sha256 = _parse_source_sha256(data)
        download_name = _parse_download_name(data, source_url)

        for name in ("bootstrap.sh", "setup.sh"):
            if not (recipe_dir / name).is_file():
                raise GuestError(f"missing {name} in {recipe_dir}")
        if source_kind == "cloud-image":
            tmpl = recipe_dir / "user-data.yaml.tmpl"
            if not tmpl.is_file():
                raise GuestError(f"missing user-data.yaml.tmpl in {recipe_dir}")
        elif source_kind == "iso-autoinstall":
            if not isinstance(data.get("cidata"), dict):
                raise GuestError("iso-autoinstall requires [cidata] table")
            cidata_dir = recipe_dir / "cidata"
            for name in ISO_CIDATA_REQUIRED:
                if not (cidata_dir / name).is_file():
                    raise GuestError(f"missing cidata/{name} in {recipe_dir}")

        session_raw = data.get("session")
        if not isinstance(session_raw, dict):
            raise GuestError("missing [session] table")
        packages_raw = data.get("packages")
        if not isinstance(packages_raw, dict):
            raise GuestError("missing [packages] table")
        user_raw = data.get("user")
        if not isinstance(user_raw, dict):
            raise GuestError("missing [user] table")

        runtime = packages_raw.get("runtime") or []
        if not isinstance(runtime, list) or not all(
            isinstance(item, str) for item in runtime
        ):
            raise GuestError("packages.runtime must be a list of strings")
        groups = user_raw.get("groups") or []
        if not isinstance(groups, list) or not all(
            isinstance(item, str) for item in groups
        ):
            raise GuestError("user.groups must be a list of strings")

        snapshot = packages_raw.get("snapshot_url")
        if snapshot is not None and not isinstance(snapshot, str):
            raise GuestError("packages.snapshot_url must be a string")

        cidata: Cidata | None = None
        cidata_raw = data.get("cidata")
        if cidata_raw is not None:
            if not isinstance(cidata_raw, dict):
                raise GuestError("[cidata] must be a table")
            cidata = Cidata(
                disk=_require_str(cidata_raw, "disk"),
                encrypt=bool(cidata_raw.get("encrypt", False)),
            )
            if source_kind == "iso-autoinstall":
                if cidata.encrypt:
                    raise GuestError(
                        "iso-autoinstall [cidata] encrypt must be false"
                    )
                if cidata.disk != ISO_AUTOINSTALL_DISK:
                    raise GuestError(
                        "iso-autoinstall [cidata] disk must be /dev/vda"
                    )

        ovmf_code = data.get("ovmf_code")
        ovmf_vars = data.get("ovmf_vars_template")
        if ovmf_code is not None and not isinstance(ovmf_code, str):
            raise GuestError("ovmf_code must be a string")
        if ovmf_vars is not None and not isinstance(ovmf_vars, str):
            raise GuestError("ovmf_vars_template must be a string")

        return cls(
            id=guest_id,
            arch=_require_str(data, "arch"),
            firmware=firmware,  # type: ignore[arg-type]
            source_kind=source_kind,  # type: ignore[arg-type]
            source_url=source_url,
            source_sha256=source_sha256,
            download_name=download_name,
            disk_gb=_require_int(data, "disk_gb"),
            memory_mib=_require_int(data, "memory_mib"),
            cpus=_require_int(data, "cpus"),
            build_timeout_s=_require_int(data, "build_timeout_s"),
            boot_timeout_s=_require_int(data, "boot_timeout_s"),
            ovmf_code=ovmf_code or None,
            ovmf_vars_template=ovmf_vars or None,
            session=Session(
                kind=_require_str(session_raw, "kind"),
                display_manager=_require_str(session_raw, "display_manager"),
                compositor=_require_str(session_raw, "compositor"),
                autologin=bool(session_raw.get("autologin", False)),
                wayland=bool(session_raw.get("wayland", False)),
            ),
            packages=Packages(
                manager=_require_str(packages_raw, "manager"),
                snapshot_url=snapshot.strip() if isinstance(snapshot, str) else None,
                runtime=tuple(runtime),
            ),
            user=User(
                name=_require_str(user_raw, "name"),
                groups=tuple(groups),
            ),
            cidata=cidata,
            recipe_dir=recipe_dir.resolve(),
        )

    def recipe_digest(self) -> str:
        return recipe_digest(self.recipe_dir)

    def golden_digest(self) -> str:
        """Content address: recipe digest plus source checksum."""
        h = hashlib.sha256()
        h.update(self.recipe_digest().encode("ascii"))
        h.update(b":")
        h.update(self.source_sha256.encode("ascii"))
        return h.hexdigest()

    def source_filename(self) -> str:
        return self.download_name


def load_guest(
    guest_id: str,
    *,
    images_root: Path | None = None,
) -> Guest:
    guest_id = _parse_id(guest_id)
    root = images_root if images_root is not None else default_images_root()
    recipe_dir = Path(root) / guest_id
    if not (recipe_dir / "image.toml").is_file():
        raise GuestError(f"no recipe for {guest_id!r} at {recipe_dir}")
    guest = Guest.load(recipe_dir)
    if guest.id != guest_id:
        raise GuestError(
            f"recipe id {guest.id!r} does not match directory {guest_id!r}"
        )
    return guest
