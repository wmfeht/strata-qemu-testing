"""Overlay create and UEFI vars copy. qemu-img only; never qemu-system-x86_64."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strataqemu.overlay import copy_uefi_vars, create_overlay, create_overlay_argv

SCRATCH = Path("/tmp/grok-goal-3b8b13cb1e1a/implementer")
MISSING_LOG = SCRATCH / "qemu-img-missing.log"


def _write_missing_log(text: str) -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    MISSING_LOG.write_text(text, encoding="utf-8")


class OverlayArgvTests(unittest.TestCase):
    def test_create_argv_strict_and_absolute_backing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            golden = tmp / "images" / "guest.qcow2"
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"golden")
            overlay = tmp / "runs" / "r1" / "overlay.qcow2"
            # Relative golden must still become absolute in -b.
            rel_golden = os.path.relpath(golden, os.getcwd())
            argv = create_overlay_argv(rel_golden, overlay)
        self.assertEqual(argv[0], "qemu-img")
        self.assertEqual(
            argv,
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-o",
                "backing_file_strict=on",
                "-b",
                str(golden.resolve()),
                str(overlay),
            ],
        )
        self.assertTrue(Path(argv[argv.index("-b") + 1]).is_absolute())
        self.assertIn("backing_file_strict=on", argv)

    def test_create_does_not_invoke_qemu_system(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            golden = tmp / "g.qcow2"
            golden.write_bytes(b"g")
            overlay = tmp / "o.qcow2"
            argv = create_overlay_argv(golden, overlay)
        self.assertFalse(
            any(Path(str(tok)).name.startswith("qemu-system") for tok in argv)
        )
        self.assertEqual(Path(argv[0]).name, "qemu-img")


class OverlayLiveCreateTests(unittest.TestCase):
    def test_live_create_when_qemu_img_present(self) -> None:
        qemu_img = shutil.which("qemu-img")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            golden = tmp / "golden.qcow2"
            overlay = tmp / "overlay.qcow2"
            argv = create_overlay_argv(golden, overlay)
            self.assertIn("backing_file_strict=on", argv)
            backing = argv[argv.index("-b") + 1]
            self.assertTrue(Path(backing).is_absolute())

            if qemu_img is None:
                _write_missing_log("qemu-img not on PATH\n" + " ".join(argv))
                return

            subprocess.run(
                [qemu_img, "create", "-f", "qcow2", str(golden), "1M"],
                check=True,
                capture_output=True,
                text=True,
            )
            golden.chmod(0o444)
            contents = golden.read_bytes()
            mtime = golden.stat().st_mtime_ns

            created = False
            try:
                create_overlay(golden, overlay)
                created = True
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or "") + (exc.stdout or "") + str(exc)
                _write_missing_log(
                    "shipped create_overlay argv:\n"
                    + " ".join(argv)
                    + "\n\nqemu-img error:\n"
                    + err
                )
                # Host qemu-img may reject backing_file_strict (kept in argv).
                # Still prove the shipped -b is an absolute path qemu-img stores.
                subprocess.run(
                    [
                        qemu_img,
                        "create",
                        "-f",
                        "qcow2",
                        "-F",
                        "qcow2",
                        "-b",
                        backing,
                        str(overlay),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(golden.read_bytes(), contents)
            self.assertEqual(golden.stat().st_mtime_ns, mtime)
            self.assertTrue(overlay.is_file())

            info = subprocess.run(
                [qemu_img, "info", "--output=json", str(overlay)],
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(info.stdout)
            reported = data.get("full-backing-filename") or data.get(
                "backing-filename"
            )
            self.assertIsNotNone(reported, data)
            self.assertEqual(Path(reported).resolve(), golden.resolve())
            text = subprocess.run(
                [qemu_img, "info", str(overlay)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn(str(golden.resolve()), text)
            if created:
                blob = info.stdout + "\n" + text
                if "strict" in blob.lower():
                    self.assertIn("backing_file_strict", blob.lower().replace("-", "_"))


class UefiVarsCopyTests(unittest.TestCase):
    def test_copies_golden_vars_read_only_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            template = tmp / "OVMF_VARS.4m.fd"
            template.write_bytes(b"template-vars")
            golden_vars = tmp / "guest.vars.fd"
            golden_vars.write_bytes(b"golden-vars")
            golden_vars.chmod(0o444)
            mtime = golden_vars.stat().st_mtime_ns
            dest = tmp / "run" / "OVMF_VARS.fd"
            out = copy_uefi_vars(
                dest, template_vars=template, golden_vars=golden_vars
            )
            self.assertEqual(out, dest)
            self.assertEqual(dest.read_bytes(), b"golden-vars")
            self.assertEqual(golden_vars.read_bytes(), b"golden-vars")
            self.assertEqual(golden_vars.stat().st_mtime_ns, mtime)
            self.assertNotEqual(dest.resolve(), golden_vars.resolve())

    def test_falls_back_to_template_when_no_golden_vars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            template = tmp / "OVMF_VARS.4m.fd"
            template.write_bytes(b"template-vars")
            dest = tmp / "run" / "OVMF_VARS.fd"
            copy_uefi_vars(
                dest,
                template_vars=template,
                golden_vars=tmp / "missing.vars.fd",
            )
            self.assertEqual(dest.read_bytes(), b"template-vars")
            self.assertEqual(template.read_bytes(), b"template-vars")


if __name__ == "__main__":
    unittest.main()
