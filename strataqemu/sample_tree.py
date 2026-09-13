"""Host-side sample file tree for a live Strata session.

Generated at runtime (stdlib only) and uploaded as a tarball. The guest
extracts it to ``$HOME/fixtures``.
"""

from __future__ import annotations

import io
import struct
import tarfile
import zipfile
import zlib
from pathlib import Path

GUEST_FIXTURES_REL = "fixtures"
GUEST_FIXTURES_TAR_REMOTE = "/tmp/strata-fixtures.tar.gz"


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def png_1x1(rgb: tuple[int, int, int] = (0xC0, 0x40, 0x20)) -> bytes:
    """Minimal valid 1×1 RGB PNG."""
    r, g, b = rgb
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes((r, g, b))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def sample_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hello.txt", "hello from zip\n")
        zf.writestr("nested/inside.txt", "nested zip member\n")
    return buf.getvalue()


def write_sample_tree(root: Path) -> Path:
    """Populate ``root`` with documents, a PNG, an archive, links, and dirs."""
    root.mkdir(parents=True, exist_ok=True)

    documents = root / "documents"
    documents.mkdir()
    (documents / "notes.txt").write_text("Meeting notes\n", encoding="utf-8")
    (documents / "report.md").write_text("# Report\n\nSample markdown.\n", encoding="utf-8")
    (documents / "spreadsheet.csv").write_text(
        "name,value\nalpha,1\nbeta,2\n", encoding="utf-8"
    )

    pictures = root / "pictures"
    pictures.mkdir()
    (pictures / "pixel.png").write_bytes(png_1x1())
    (pictures / "caption.txt").write_text("a one-pixel PNG\n", encoding="utf-8")

    archive = root / "archive"
    archive.mkdir()
    (archive / "sample.zip").write_bytes(sample_zip_bytes())

    nested = root / "nested" / "deep"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("buried\n", encoding="utf-8")

    (root / "empty").mkdir()

    (root / "readme.md").write_text(
        "# Fixtures\n\nSample files for a live Strata session.\n",
        encoding="utf-8",
    )
    (root / "todo.txt").write_text("- open this directory in Strata\n", encoding="utf-8")
    (root / ".hidden.txt").write_text("hidden\n", encoding="utf-8")
    (root / "file with spaces.txt").write_text("spaces in the name\n", encoding="utf-8")
    (root / "long-file.txt").write_text(
        "".join(f"line {i:04d}\n" for i in range(1, 101)),
        encoding="utf-8",
    )
    script = root / "script.sh"
    script.write_text("#!/bin/sh\necho fixtures\n", encoding="utf-8")
    script.chmod(0o755)

    link = root / "link-to-readme.md"
    if not link.exists():
        link.symlink_to("readme.md")
    broken = root / "broken-link"
    if not broken.exists() and not broken.is_symlink():
        broken.symlink_to("missing-target")

    return root


def pack_sample_tree(root: Path, archive: Path) -> Path:
    """Write a gzip tar of ``root`` with members relative to ``root``."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(root.rglob("*"), key=lambda p: str(p.relative_to(root))):
            tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
    return archive


def build_fixtures_archive(dest: Path) -> Path:
    """Generate the sample tree next to ``dest`` and pack it."""
    dest = Path(dest)
    tree = dest.parent / "sample-tree"
    write_sample_tree(tree)
    return pack_sample_tree(tree, dest)
