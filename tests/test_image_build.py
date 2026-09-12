"""image-build incremental skip, --force, bootstrap.sh. No live QEMU."""

from __future__ import annotations

import hashlib
import http.server
import io
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from strataqemu import config
from strataqemu.cli import CheckHostResult, main
from strataqemu.guest import load_guest
from strataqemu.image_build import (
    ImageBuildError,
    build_duration_note,
    capture_build_timeout_evidence,
    create_blank_qcow2_argv,
    golden_qcow2,
    golden_vars_fd,
    inventory_basename,
    inventory_provenance_key,
    omarchy_major_for_guest,
    omarchy_version_matches_major,
    recipe_files_to_upload,
    required_free_bytes,
    run_image_build,
    setup_ssh_command,
    ssh_auth_rejected,
    ssh_not_listening,
    wait_iso_autoinstall,
    wait_ssh,
    working_qemu_argv,
)
from strataqemu.qemu import Machine

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "images" / "ubuntu-2404" / "bootstrap.sh"
ARCH_BOOTSTRAP = REPO_ROOT / "images" / "arch" / "bootstrap.sh"
FEDORA_BOOTSTRAP = REPO_ROOT / "images" / "fedora-workstation" / "bootstrap.sh"
OMARCHY4_BOOTSTRAP = REPO_ROOT / "images" / "omarchy-4" / "bootstrap.sh"
OMARCHY3_BOOTSTRAP = REPO_ROOT / "images" / "omarchy-3" / "bootstrap.sh"


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
        self.assertNotIn("typically takes", err.getvalue())
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
        self.assertNotIn("typically takes", err.getvalue())
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

    def test_duration_note_cloud_vs_iso(self) -> None:
        ubuntu = load_guest("ubuntu-2404")
        cloud = build_duration_note(ubuntu)
        self.assertIn("ubuntu-2404", cloud)
        self.assertIn("10-30 minutes", cloud)
        self.assertIn("timeout 60 minutes", cloud)
        self.assertIn("run dir", cloud)
        self.assertNotIn("ISO autoinstall", cloud)
        omarchy = load_guest("omarchy-4")
        iso = build_duration_note(omarchy)
        self.assertIn("omarchy-4", iso)
        self.assertIn("ISO autoinstall", iso)
        self.assertIn("20-60 minutes", iso)
        self.assertIn("timeout 60 minutes", iso)
        omarchy3 = load_guest("omarchy-3")
        iso3 = build_duration_note(omarchy3)
        self.assertIn("omarchy-3", iso3)
        self.assertIn("ISO autoinstall", iso3)
        self.assertIn("20-60 minutes", iso3)
        self.assertIn("timeout 60 minutes", iso3)

    def test_live_build_prints_duration_note_on_stderr(self) -> None:
        guest = load_guest("ubuntu-2404")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            golden = golden_qcow2(guest, cache)
            host = CheckHostResult(
                ok=True, errors=(), ssh_key=Path("k"), ovmf_code=Path("o")
            )
            buf = io.StringIO()
            err = io.StringIO()
            with (
                redirect_stdout(buf),
                redirect_stderr(err),
                patch(
                    "strataqemu.image_build.check_host", return_value=host
                ),
                patch(
                    "strataqemu.image_build.build_live", return_value=golden
                ) as live,
            ):
                code = run_image_build(
                    "ubuntu-2404", force=True, cache_dir=cache
                )
        self.assertEqual(code, 0, err.getvalue())
        live.assert_called_once()
        self.assertIn(build_duration_note(guest), err.getvalue())
        self.assertIn(str(golden.resolve()), buf.getvalue())
        self.assertNotIn("typically takes", buf.getvalue())

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

    def test_fedora_matching_golden_prints_path_and_skips_qemu(self) -> None:
        guest = load_guest("fedora-workstation")
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
                code = run_image_build("fedora-workstation", cache_dir=cache)
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(str(golden.resolve()), buf.getvalue())
            ch.assert_not_called()

    def test_fedora_uploads_are_setup_sh_only(self) -> None:
        guest = load_guest("fedora-workstation")
        self.assertEqual(
            {p.name for p in recipe_files_to_upload(guest)}, {"setup.sh"}
        )

    def test_inventory_basename_fedora_rpm_ubuntu_dpkg(self) -> None:
        fedora = load_guest("fedora-workstation")
        ubuntu = load_guest("ubuntu-2404")
        self.assertEqual(inventory_basename(fedora), "rpm.txt")
        self.assertEqual(inventory_provenance_key(fedora), "rpm")
        self.assertEqual(inventory_basename(ubuntu), "dpkg.txt")
        self.assertEqual(inventory_provenance_key(ubuntu), "dpkg")


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


def _serve_directory(root: Path) -> tuple[str, http.server.ThreadingHTTPServer]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return f"http://{host}:{port}", httpd


def _fedora_listing_fixture(
    root: Path, *, payload: bytes, checksum_hex: str
) -> None:
    image = "Fedora-Cloud-Base-Generic-fixture.qcow2"
    decoy = b"decoy\n"
    (root / image).write_bytes(payload)
    (root / "Fedora-Cloud-Base-UEFI-UKI-fixture.qcow2").write_bytes(decoy)
    decoy_hex = hashlib.sha256(decoy).hexdigest()
    (root / "Fedora-Cloud-images-x86_64-CHECKSUM").write_text(
        f"SHA256 ({image}) = {checksum_hex}\n"
        f"SHA256 (Fedora-Cloud-Base-UEFI-UKI-fixture.qcow2) = {decoy_hex}\n",
        encoding="utf-8",
    )


class FedoraBootstrapShTests(unittest.TestCase):
    def test_scrape_matching_sha256_installs_into_downloads(self) -> None:
        payload = b"tiny-fedora-cloudimg-fixture\n"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            served = tmp / "mirror"
            served.mkdir()
            _fedora_listing_fixture(served, payload=payload, checksum_hex=digest)
            cache = tmp / "cache"
            url, httpd = _serve_directory(served)
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = url + "/"
            env["SOURCE_SHA256"] = digest
            env["SOURCE_FILENAME"] = "Fedora-Cloud-Base-Generic.qcow2"
            try:
                proc = subprocess.run(
                    ["bash", str(FEDORA_BOOTSTRAP)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
            dest = cache / "downloads" / "Fedora-Cloud-Base-Generic.qcow2"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(dest.is_file(), dest)
            self.assertEqual(dest.read_bytes(), payload)
            self.assertIn(str(dest), proc.stdout)

    def test_pin_mismatch_is_nonzero_and_does_not_keep_blob(self) -> None:
        payload = b"tiny-fedora-cloudimg-fixture-bad\n"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            served = tmp / "mirror"
            served.mkdir()
            _fedora_listing_fixture(served, payload=payload, checksum_hex=digest)
            cache = tmp / "cache"
            url, httpd = _serve_directory(served)
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = url + "/"
            env["SOURCE_SHA256"] = "0" * 64
            env["SOURCE_FILENAME"] = "Fedora-Cloud-Base-Generic.qcow2"
            try:
                proc = subprocess.run(
                    ["bash", str(FEDORA_BOOTSTRAP)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
            dest = cache / "downloads" / "Fedora-Cloud-Base-Generic.qcow2"
            partial = cache / "downloads" / "Fedora-Cloud-Base-Generic.qcow2.partial"
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(dest.exists(), dest)
            self.assertFalse(partial.exists(), partial)

    def test_bad_download_checksum_does_not_keep_blob(self) -> None:
        payload = b"tiny-fedora-cloudimg-corrupt\n"
        pin = hashlib.sha256(b"not-the-payload\n").hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            served = tmp / "mirror"
            served.mkdir()
            # Sidecar agrees with the pin; served blob does not.
            _fedora_listing_fixture(served, payload=payload, checksum_hex=pin)
            cache = tmp / "cache"
            url, httpd = _serve_directory(served)
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = url + "/"
            env["SOURCE_SHA256"] = pin
            env["SOURCE_FILENAME"] = "Fedora-Cloud-Base-Generic.qcow2"
            try:
                proc = subprocess.run(
                    ["bash", str(FEDORA_BOOTSTRAP)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
            dest = cache / "downloads" / "Fedora-Cloud-Base-Generic.qcow2"
            partial = cache / "downloads" / "Fedora-Cloud-Base-Generic.qcow2.partial"
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("checksum", proc.stderr.lower())
            self.assertFalse(dest.exists(), dest)
            self.assertFalse(partial.exists(), partial)


class IsoAutoinstallBuildTests(unittest.TestCase):
    def test_build_live_iso_path_is_blank_disk_not_cloud_init(self) -> None:
        import inspect

        from strataqemu.image_build import build_live

        src = inspect.getsource(build_live)
        self.assertIn("create_blank_qcow2", src)
        self.assertIn("write_omarchy_cidata_iso", src)
        self.assertIn("wait_iso_autoinstall", src)
        self.assertIn("golden_vars_fd", src)
        self.assertIn("install_iso", src)
        self.assertIn("omarchy_version", src)
        self.assertIn("setup_ssh_command", src)
        self.assertNotIn("sudo -n bash /tmp/setup.sh", src)

    def test_omarchy4_setup_ssh_uses_password_sudo_not_n(self) -> None:
        guest = load_guest("omarchy-4")
        cmd = setup_ssh_command(guest)
        self.assertIn("sudo -S", cmd)
        self.assertIn("-p ''", cmd)
        self.assertIn("foobar", cmd)
        self.assertIn("bash /tmp/setup.sh", cmd)
        self.assertNotIn("sudo -n", cmd)
        ubuntu = load_guest("ubuntu-2404")
        cloud = setup_ssh_command(ubuntu)
        self.assertIn("sudo -n bash /tmp/setup.sh", cloud)
        self.assertNotIn("sudo -S", cloud)
        arch = load_guest("arch")
        arch_cmd = setup_ssh_command(arch)
        self.assertIn("sudo -n bash /tmp/setup.sh", arch_cmd)
        self.assertNotIn("sudo -S", arch_cmd)

    def test_blank_disk_argv_is_create_not_convert(self) -> None:
        argv = create_blank_qcow2_argv("/tmp/working.qcow2", 40)
        self.assertEqual(argv[0], "qemu-img")
        self.assertEqual(argv[1], "create")
        self.assertIn("-f", argv)
        self.assertIn("qcow2", argv)
        self.assertIn("40G", argv)
        self.assertNotIn("convert", argv)
        self.assertNotIn("-O", argv)

    def test_iso_working_argv_ide_cd_pinned_virtio_cidata_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cidata = tmp / "cidata.iso"
            cidata.write_bytes(b"cidata")
            iso = tmp / "omarchy-4.0.3.iso"
            iso.write_bytes(b"iso")
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
                install_iso=iso,
            )
        self.assertEqual(argv[0], "qemu-system-x86_64")
        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d]
        self.assertEqual(len(drive0), 1)
        self.assertIn("cache=writeback", drive0[0])
        self.assertNotIn("cache=unsafe", drive0[0])
        self.assertNotIn("media=cdrom", drive0[0])
        devices = _all_after(argv, "-device")
        ide = [d for d in devices if d.startswith("ide-cd")]
        self.assertEqual(len(ide), 1)
        self.assertTrue(any("bootindex=2" in d for d in ide))
        self.assertFalse(any(d.startswith("usb-storage") for d in devices))
        self.assertFalse(any(d.startswith("qemu-xhci") for d in devices))
        self.assertFalse(any(d.startswith("scsi-cd") for d in devices))
        cidata_drive = [d for d in drives if "id=cidata0" in d]
        self.assertEqual(len(cidata_drive), 1)
        self.assertNotIn("media=cdrom", cidata_drive[0])
        blk = [d for d in devices if "virtio-blk" in d]
        self.assertEqual(len(blk), 2)
        self.assertIn("drive=drive0", blk[0])
        self.assertIn("addr=0x8", blk[0])
        self.assertIn("drive=cidata0", blk[1])
        self.assertIn("addr=0x9", blk[1])

    def test_timeout_evidence_tries_screenshot_and_qmp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ) as vnc,
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ) as qmp,
            ):
                capture_build_timeout_evidence(machine)
            vnc.assert_called_once()
            self.assertEqual(vnc.call_args.kwargs.get("display"), 5901)
            qmp.assert_called_once()
            self.assertIn("qmp-timeout.png", str(qmp.call_args.args[1]))

    def test_ssh_auth_rejected_detects_permission_denied(self) -> None:
        self.assertTrue(
            ssh_auth_rejected(
                255, "tester@127.0.0.1: Permission denied (publickey,password).\n"
            )
        )
        self.assertTrue(
            ssh_auth_rejected(255, "Connection closed by 127.0.0.1 port 47909\n")
        )
        self.assertTrue(ssh_auth_rejected(255, "Connection reset by peer\n"))
        self.assertFalse(ssh_auth_rejected(0, ""))
        self.assertFalse(ssh_auth_rejected(255, "Connection refused"))
        self.assertTrue(ssh_not_listening("Connection refused"))
        self.assertTrue(ssh_not_listening("Connection timed out during banner exchange"))
        self.assertFalse(ssh_not_listening("Connection closed by 127.0.0.1"))

    def test_wait_iso_autoinstall_permission_denied_retries_until_attempts(self) -> None:
        """Live ISO sshd rejects tester until reboot; do not fail-fast at 30."""
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            msg = (
                "tester@127.0.0.1: Permission denied (publickey,password).\n"
                if calls["n"] % 2
                else "Connection closed by 127.0.0.1 port 47909\n"
            )
            return subprocess.CompletedProcess(argv, 255, "", msg)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ),
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ),
                self.assertRaises(TimeoutError) as ctx,
            ):
                wait_iso_autoinstall(
                    machine,
                    major=4,
                    timeout=3600,
                    run=fake_run,
                    sleep=lambda _s: None,
                    max_attempts=8,
                )
        self.assertIn("8 attempts", str(ctx.exception))
        self.assertNotIn("rejected tester", str(ctx.exception).lower())
        self.assertEqual(calls["n"], 8)

    def test_wait_iso_autoinstall_caps_max_attempts(self) -> None:
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(
                argv, 255, "", "Connection refused\n"
            )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ),
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ),
                self.assertRaises(TimeoutError) as ctx,
            ):
                wait_iso_autoinstall(
                    machine,
                    major=4,
                    timeout=3600,
                    run=fake_run,
                    sleep=lambda _s: None,
                    max_attempts=4,
                )
        self.assertIn("4 attempts", str(ctx.exception))
        self.assertEqual(calls["n"], 4)

    def test_wait_ssh_caps_permission_denied_retries(self) -> None:
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(
                argv,
                255,
                "",
                "Permission denied (publickey,password).\n",
            )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ),
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ),
                self.assertRaises(ImageBuildError) as ctx,
            ):
                wait_ssh(
                    machine,
                    timeout=180,
                    run=fake_run,
                    sleep=lambda _s: None,
                    auth_reject_max=3,
                )
        self.assertIn("rejected tester", str(ctx.exception).lower())
        self.assertEqual(calls["n"], 3)

    def test_wait_ssh_connection_closed_does_not_reset_streak(self) -> None:
        messages = (
            "Permission denied (publickey,password).\n",
            "Connection closed by 127.0.0.1 port 22022\n",
            "Permission denied (publickey,password).\n",
            "Connection closed by 127.0.0.1 port 22022\n",
        )
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(
                argv, 255, "", messages[(calls["n"] - 1) % len(messages)]
            )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ),
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ),
                self.assertRaises(ImageBuildError) as ctx,
            ):
                wait_ssh(
                    machine,
                    timeout=180,
                    run=fake_run,
                    sleep=lambda _s: None,
                    auth_reject_max=4,
                )
        self.assertIn("rejected tester", str(ctx.exception).lower())
        self.assertEqual(calls["n"], 4)

    def test_wait_iso_autoinstall_does_not_info_log_ssh_rejects(self) -> None:
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(
                argv,
                255,
                "",
                "tester@127.0.0.1: Permission denied (publickey,password).\n",
            )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ),
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ),
                self.assertNoLogs("strataqemu", level="INFO"),
                self.assertRaises(TimeoutError),
            ):
                wait_iso_autoinstall(
                    machine,
                    major=4,
                    timeout=3600,
                    run=fake_run,
                    sleep=lambda _s: None,
                    max_attempts=4,
                )
        self.assertEqual(calls["n"], 4)

    def test_wait_ssh_does_not_warning_log_each_reject(self) -> None:
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(
                argv,
                255,
                "",
                "Permission denied (publickey,password).\n",
            )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            identity = tmp / "id"
            identity.write_text("k", encoding="utf-8")
            machine = Machine(
                tmp / "working.qcow2",
                tmp,
                ssh_port=22022,
                vnc_port=5901,
                identity=identity,
            )
            with (
                patch(
                    "strataqemu.image_build.vnc_framebuffer_png",
                    return_value=True,
                ),
                patch(
                    "strataqemu.image_build.qmp_screendump",
                    return_value=False,
                ),
                self.assertNoLogs("strataqemu", level="WARNING"),
                self.assertRaises(ImageBuildError),
            ):
                wait_ssh(
                    machine,
                    timeout=180,
                    run=fake_run,
                    sleep=lambda _s: None,
                    auth_reject_max=3,
                )
        self.assertEqual(calls["n"], 3)

    def test_wait_predicate_accepts_4x_rejects_3x_and_wizard(self) -> None:
        self.assertTrue(omarchy_version_matches_major("4.0.3\n", 4))
        self.assertTrue(omarchy_version_matches_major("Omarchy 4.0.3", 4))
        self.assertTrue(omarchy_version_matches_major("omarchy 4.1.0", 4))
        self.assertFalse(omarchy_version_matches_major("3.8.2\n", 4))
        self.assertFalse(omarchy_version_matches_major("Omarchy 3.8.2", 4))
        self.assertFalse(omarchy_version_matches_major("", 4))
        self.assertFalse(
            omarchy_version_matches_major("waiting for configurator", 4)
        )
        self.assertFalse(omarchy_version_matches_major("wizard hang", 4))
        self.assertTrue(omarchy_version_matches_major("3.8.2", 3))

    def test_wait_predicate_accepts_3x_rejects_4x_and_wizard(self) -> None:
        self.assertEqual(omarchy_major_for_guest("omarchy-3"), 3)
        self.assertEqual(omarchy_major_for_guest(load_guest("omarchy-3")), 3)
        self.assertEqual(omarchy_major_for_guest("omarchy-4"), 4)
        self.assertTrue(omarchy_version_matches_major("3.8.4\n", 3))
        self.assertTrue(omarchy_version_matches_major("Omarchy 3.8.4", 3))
        self.assertTrue(omarchy_version_matches_major("omarchy 3.1.0", 3))
        self.assertFalse(omarchy_version_matches_major("4.0.3\n", 3))
        self.assertFalse(omarchy_version_matches_major("Omarchy 4.0.3", 3))
        self.assertFalse(omarchy_version_matches_major("", 3))
        self.assertFalse(
            omarchy_version_matches_major("waiting for configurator", 3)
        )
        self.assertFalse(omarchy_version_matches_major("wizard hang", 3))

    def test_golden_vars_path_is_omarchy_4_vars_fd(self) -> None:
        guest = load_guest("omarchy-4")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            path = golden_vars_fd(guest, cache)
        self.assertEqual(path.name, "omarchy-4.vars.fd")
        self.assertEqual(path.parent.name, "images")

    def test_golden_vars_path_is_omarchy_3_vars_fd(self) -> None:
        guest = load_guest("omarchy-3")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            path = golden_vars_fd(guest, cache)
        self.assertEqual(path.name, "omarchy-3.vars.fd")
        self.assertEqual(path.parent.name, "images")

    def test_omarchy4_matching_golden_prints_path_and_skips_qemu(self) -> None:
        guest = load_guest("omarchy-4")
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
                code = run_image_build("omarchy-4", cache_dir=cache)
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(str(golden.resolve()), buf.getvalue())
            ch.assert_not_called()

    def test_omarchy4_bootstrap_checksum_mismatch_fail_closed(self) -> None:
        payload = b"omarchy-iso-fixture-bad\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.iso"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = "0" * 64
            env["SOURCE_FILENAME"] = "tiny.iso"
            proc = subprocess.run(
                ["bash", str(OMARCHY4_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.iso"
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("checksum", proc.stderr.lower())
            self.assertFalse(dest.exists(), dest)

    def test_omarchy3_matching_golden_prints_path_and_skips_qemu(self) -> None:
        guest = load_guest("omarchy-3")
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
                code = run_image_build("omarchy-3", cache_dir=cache)
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(str(golden.resolve()), buf.getvalue())
            ch.assert_not_called()

    def test_omarchy3_bootstrap_checksum_mismatch_fail_closed(self) -> None:
        payload = b"omarchy-3-iso-fixture-bad\n"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.iso"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = "0" * 64
            env["SOURCE_FILENAME"] = "tiny.iso"
            proc = subprocess.run(
                ["bash", str(OMARCHY3_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.iso"
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("checksum", proc.stderr.lower())
            self.assertFalse(dest.exists(), dest)

    def test_omarchy3_bootstrap_matching_sha256(self) -> None:
        payload = b"omarchy-3-iso-fixture\n"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.iso"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = digest
            env["SOURCE_FILENAME"] = "tiny.iso"
            proc = subprocess.run(
                ["bash", str(OMARCHY3_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.iso"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(dest.read_bytes(), payload)

    def test_omarchy4_bootstrap_matching_sha256(self) -> None:
        payload = b"omarchy-iso-fixture\n"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "tiny.iso"
            src.write_bytes(payload)
            cache = tmp / "cache"
            env = os.environ.copy()
            env["STRATA_QEMU_CACHE"] = str(cache)
            env["SOURCE_URL"] = src.resolve().as_uri()
            env["SOURCE_SHA256"] = digest
            env["SOURCE_FILENAME"] = "tiny.iso"
            proc = subprocess.run(
                ["bash", str(OMARCHY4_BOOTSTRAP)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            dest = cache / "downloads" / "tiny.iso"
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
