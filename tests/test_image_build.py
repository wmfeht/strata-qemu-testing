"""image-build incremental skip, --force, bootstrap.sh. No live QEMU."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu import config
from strataqemu.cli import CheckHostResult, main
from strataqemu.guest import load_guest
from strataqemu.image_build import (
    golden_qcow2,
    recipe_files_to_upload,
    required_free_bytes,
    run_image_build,
    working_qemu_argv,
)
from strataqemu.qemu import uses_cloud_init_seed

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "images" / "ubuntu-2404" / "bootstrap.sh"
ARCH_BOOTSTRAP = REPO_ROOT / "images" / "arch" / "bootstrap.sh"


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _all_after(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag]


def _refuse_qemu_system(cmd, *args, **kwargs):
    name = Path(str(cmd[0])).name if cmd else ""
    if name.startswith("qemu-system"):
        raise AssertionError(f"spawned qemu-system: {cmd}")
    raise AssertionError(f"unexpected subprocess: {cmd}")


class IncrementalImageBuildTests(unittest.TestCase):
    def test_matching_golden_prints_path_and_skips_qemu(self) -> None:
        guest = load_guest("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            golden = golden_qcow2(guest, cache)
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"existing-golden")
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            buf = io.StringIO()
            err = io.StringIO()
            try:
                with (
                    redirect_stdout(buf),
                    redirect_stderr(err),
                    patch("strataqemu.image_build.check_host") as ch,
                    patch("subprocess.Popen", side_effect=_refuse_qemu_system),
                    patch("subprocess.run", side_effect=_refuse_qemu_system),
                ):
                    code = main(["image-build", "ubuntu-2404"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn(str(golden.resolve()), buf.getvalue())
        self.assertNotIn("not implemented", buf.getvalue() + err.getvalue())
        ch.assert_not_called()

    def test_force_does_not_take_incremental_skip(self) -> None:
        guest = load_guest("ubuntu-2404")
        fail = CheckHostResult(ok=False, errors=("injected-host-failure",))
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            golden = golden_qcow2(guest, cache)
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"existing-golden")
            old = os.environ.get(config.CACHE_ENV)
            os.environ[config.CACHE_ENV] = str(cache)
            buf = io.StringIO()
            err = io.StringIO()
            try:
                with (
                    redirect_stdout(buf),
                    redirect_stderr(err),
                    patch(
                        "strataqemu.image_build.check_host", return_value=fail
                    ) as ch,
                    patch("subprocess.Popen", side_effect=_refuse_qemu_system) as popen,
                    patch("subprocess.run", side_effect=_refuse_qemu_system) as run,
                ):
                    code = main(["image-build", "ubuntu-2404", "--force"])
            finally:
                if old is None:
                    os.environ.pop(config.CACHE_ENV, None)
                else:
                    os.environ[config.CACHE_ENV] = old
        self.assertEqual(code, 1)
        self.assertIn("injected-host-failure", err.getvalue())
        self.assertNotEqual(buf.getvalue().strip(), str(golden.resolve()))
        self.assertNotIn("not implemented", buf.getvalue() + err.getvalue())
        ch.assert_called()
        popen.assert_not_called()
        run.assert_not_called()

    def test_run_image_build_incremental_twice_is_stable(self) -> None:
        guest = load_guest("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            golden = golden_qcow2(guest, cache)
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"existing-golden")
            paths = []
            for _ in range(2):
                buf = io.StringIO()
                with (
                    redirect_stdout(buf),
                    patch("subprocess.Popen", side_effect=_refuse_qemu_system),
                    patch("subprocess.run", side_effect=_refuse_qemu_system),
                ):
                    code = run_image_build("ubuntu-2404", cache_dir=cache)
                self.assertEqual(code, 0)
                paths.append(buf.getvalue().strip())
            self.assertEqual(paths[0], paths[1])
            self.assertEqual(paths[0], str(golden.resolve()))

    def test_arch_matching_golden_prints_path_and_skips_qemu(self) -> None:
        guest = load_guest("arch")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            golden = golden_qcow2(guest, cache)
            golden.parent.mkdir(parents=True)
            golden.write_bytes(b"existing-golden")
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch("strataqemu.image_build.check_host") as ch,
                patch("subprocess.Popen", side_effect=_refuse_qemu_system),
                patch("subprocess.run", side_effect=_refuse_qemu_system),
            ):
                code = run_image_build("arch", cache_dir=cache)
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(str(golden.resolve()), buf.getvalue())
            ch.assert_not_called()

    def test_arch_uploads_include_greetd_and_lua(self) -> None:
        guest = load_guest("arch")
        names = {p.name for p in recipe_files_to_upload(guest)}
        self.assertEqual(
            names, {"setup.sh", "greetd-config.toml", "hyprland.lua"}
        )
        ubuntu = load_guest("ubuntu-2404")
        self.assertEqual(
            {p.name for p in recipe_files_to_upload(ubuntu)}, {"setup.sh"}
        )


class WorkingDiskArgvTests(unittest.TestCase):
    def test_writeback_not_unsafe_and_scsi_cidata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cidata = tmp / "cidata.iso"
            cidata.write_bytes(b"iso")
            argv = working_qemu_argv(
                overlay=tmp / "working.qcow2",
                run_dir=tmp / "run",
                ssh_port=22022,
                vnc_port=5901,
                cpus=4,
                memory_mib=8192,
                ovmf_code=tmp / "OVMF_CODE.4m.fd",
                ovmf_vars=tmp / "OVMF_VARS.fd",
                cidata_iso=cidata,
            )
        self.assertEqual(argv[0], "qemu-system-x86_64")
        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d]
        self.assertEqual(len(drive0), 1)
        self.assertIn("cache=writeback", drive0[0])
        self.assertNotIn("cache=unsafe", drive0[0])
        devices = _all_after(argv, "-device")
        self.assertTrue(any(d.startswith("scsi-cd") for d in devices))
        self.assertIn("virtio-gpu-gl-pci", argv)
        self.assertEqual(_after(argv, "-display"), "egl-headless,gl=on")
        self.assertTrue(uses_cloud_init_seed("ubuntu-2404"))


class BootstrapShTests(unittest.TestCase):
    def test_matching_sha256_installs_into_downloads(self) -> None:
        payload = b"tiny-cloudimg-fixture\n"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.bin"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = digest
            env["SOURCE_FILENAME"] = "tiny.bin"
            proc = subprocess.run(
                ["bash", str(BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.bin"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(dest.is_file(), dest)
            self.assertEqual(dest.read_bytes(), payload)
            self.assertIn(str(dest), proc.stdout)

    def test_mismatched_sha256_is_nonzero_and_does_not_keep_blob(self) -> None:
        payload = b"tiny-cloudimg-fixture-bad\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.bin"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = "0" * 64
            env["SOURCE_FILENAME"] = "tiny.bin"
            proc = subprocess.run(
                ["bash", str(BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.bin"
            partial = cache / "downloads" / "tiny.bin.partial"
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("checksum", proc.stderr.lower())
            self.assertFalse(dest.exists(), dest)
            self.assertFalse(partial.exists(), partial)

    def test_arch_bootstrap_checksum_mismatch_fail_closed(self) -> None:
        payload = b"arch-cloudimg-fixture-bad\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.bin"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = "0" * 64
            env["SOURCE_FILENAME"] = "tiny.bin"
            proc = subprocess.run(
                ["bash", str(ARCH_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.bin"
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("checksum", proc.stderr.lower())
            self.assertFalse(dest.exists(), dest)

    def test_arch_bootstrap_matching_sha256(self) -> None:
        payload = b"arch-cloudimg-fixture\n"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.bin"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = digest
            env["SOURCE_FILENAME"] = "tiny.bin"
            proc = subprocess.run(
                ["bash", str(ARCH_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.bin"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(dest.read_bytes(), payload)


class FreeSpaceFormulaTests(unittest.TestCase):
    def test_two_times_disk_plus_source_plus_ten(self) -> None:
        gib = 1024**3
        self.assertEqual(
            required_free_bytes(40, 600 * 1024 * 1024),
            (2 * 40 + 10) * gib + 600 * 1024 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
