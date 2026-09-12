"""QEMU argv builder tests. No KVM, no qemu-system-x86_64 process."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strataqemu.artifacts import RunArtifacts
from strataqemu.qemu import (
    ISO_AUTOINSTALL_GUESTS,
    build_qemu_argv,
    uses_cloud_init_seed,
    uses_iso_autoinstall,
    vnc_tcp_port,
    write_png_rgb,
)


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _all_after(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag]


def _base_kwargs(tmp: Path, **over):
    run = tmp / "run"
    kwargs = {
        "overlay": run / "overlay.qcow2",
        "run_dir": run,
        "ssh_port": 22022,
        "vnc_port": 5901,
        "cpus": 4,
        "memory_mib": 8192,
    }
    kwargs.update(over)
    return kwargs


class DefaultGlArgvTests(unittest.TestCase):
    def test_frozen_test_image_build_stack(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            kwargs = _base_kwargs(tmp)
            with patch("subprocess.run") as run:
                argv = build_qemu_argv(**kwargs)
            run.assert_not_called()

        self.assertEqual(argv[0], "qemu-system-x86_64")
        self.assertEqual(_after(argv, "-machine"), "q35,accel=kvm,usb=off")
        self.assertEqual(_after(argv, "-cpu"), "host")
        self.assertEqual(_after(argv, "-smp"), "4")
        self.assertEqual(_after(argv, "-m"), "8192")
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-gpu-gl-pci", argv)
        self.assertNotIn("virtio-vga-gl", argv)
        self.assertEqual(_after(argv, "-display"), "egl-headless,gl=on")
        vnc = _after(argv, "-vnc")
        self.assertEqual(vnc, "127.0.0.1:5901")
        self.assertNotIn("0.0.0.0", vnc)
        self.assertFalse(any("0.0.0.0" in tok for tok in argv))

        drives = _all_after(argv, "-drive")
        drive0 = [d for d in drives if "id=drive0" in d]
        self.assertEqual(len(drive0), 1)
        self.assertIn("cache=unsafe", drive0[0])
        self.assertIn("discard=unmap", drive0[0])
        self.assertIn("if=none", drive0[0])
        self.assertNotIn("media=cdrom", drive0[0])
        overlay_abs = str(Path(kwargs["overlay"]).resolve())
        self.assertIn(f"file={overlay_abs}", drive0[0])

        devices = _all_after(argv, "-device")
        blk = [d for d in devices if "virtio-blk-pci" in d]
        self.assertEqual(len(blk), 1)
        self.assertIn("drive=drive0", blk[0])
        self.assertIn("bootindex=1", blk[0])
        self.assertNotIn("media=cdrom", blk[0])

        netdev = _after(argv, "-netdev")
        self.assertEqual(netdev, "user,id=net0,hostfwd=tcp:127.0.0.1:22022-:22")
        self.assertIn("virtio-net-pci,netdev=net0", devices)

        arts = RunArtifacts(Path(kwargs["run_dir"]))
        self.assertEqual(
            _after(argv, "-serial"),
            f"file:{arts.serial_log.resolve()}",
        )
        self.assertEqual(
            _after(argv, "-qmp"),
            f"unix:{arts.qmp_sock.resolve()},server,wait=off",
        )
        chardev = _after(argv, "-chardev")
        self.assertIn(f"path={arts.qga_sock.resolve()}", chardev)
        self.assertIn("server=on", chardev)
        self.assertIn("wait=off", chardev)
        self.assertIn("id=qga", chardev)
        self.assertIn("virtio-serial-pci", devices)
        serialport = [
            d
            for d in devices
            if d.startswith("virtserialport")
        ]
        self.assertEqual(len(serialport), 1)
        self.assertIn("chardev=qga", serialport[0])
        self.assertIn("name=org.qemu.guest_agent.0", serialport[0])
        self.assertIn("-usb", argv)
        self.assertIn("usb-tablet", devices)
        self.assertEqual(argv.count("-vnc"), 1)


class CloudInitSeedTests(unittest.TestCase):
    def test_scsi_cd_cidata_no_bootindex_no_ide(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cidata = tmp / "cidata.iso"
            argv = build_qemu_argv(
                **_base_kwargs(tmp, cidata_iso=cidata)
            )

        devices = _all_after(argv, "-device")
        drives = _all_after(argv, "-drive")
        scsi = [d for d in devices if "virtio-scsi-pci" in d]
        self.assertEqual(len(scsi), 1)
        self.assertEqual(scsi[0], "virtio-scsi-pci,id=scsi0")
        self.assertEqual(sum(1 for t in argv if "id=scsi0" in t), 1)

        cidata_drive = [d for d in drives if "id=cidata0" in d]
        self.assertEqual(len(cidata_drive), 1)
        self.assertIn("readonly=on", cidata_drive[0])
        self.assertIn("if=none", cidata_drive[0])
        self.assertIn("format=raw", cidata_drive[0])
        self.assertIn(str(cidata.resolve()), cidata_drive[0])
        self.assertNotIn("bootindex", cidata_drive[0])
        self.assertNotIn("media=cdrom", cidata_drive[0])

        scsi_cd = [d for d in devices if d.startswith("scsi-cd")]
        self.assertEqual(len(scsi_cd), 1)
        self.assertIn("drive=cidata0", scsi_cd[0])
        self.assertIn("bus=scsi0.0", scsi_cd[0])
        self.assertNotIn("bootindex", scsi_cd[0])

        self.assertFalse(any(d.startswith("ide-cd") for d in devices))
        drive0 = [d for d in drives if "id=drive0" in d][0]
        self.assertNotIn("media=cdrom", drive0)
        blk = [d for d in devices if "virtio-blk" in d][0]
        self.assertNotIn("media=cdrom", blk)
        self.assertIn("bootindex=1", blk)

    def test_cloud_guests_use_seed_not_iso(self) -> None:
        for guest in ("arch", "ubuntu-2404", "fedora-workstation"):
            with self.subTest(guest=guest):
                self.assertTrue(uses_cloud_init_seed(guest))
                self.assertFalse(uses_iso_autoinstall(guest))


class IsoAutoinstallTests(unittest.TestCase):
    def test_both_omarchy_majors_get_ide_cd_and_scsi_cidata(self) -> None:
        self.assertEqual(
            ISO_AUTOINSTALL_GUESTS,
            frozenset({"omarchy-4", "omarchy-3"}),
        )
        for guest in ("omarchy-4", "omarchy-3"):
            with self.subTest(guest=guest):
                self.assertTrue(uses_iso_autoinstall(guest), guest)
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    iso = tmp / f"{guest}.iso"
                    cidata = tmp / "cidata.iso"
                    argv = build_qemu_argv(
                        **_base_kwargs(
                            tmp,
                            install_iso=iso,
                            cidata_iso=cidata,
                        )
                    )
                devices = _all_after(argv, "-device")
                drives = _all_after(argv, "-drive")
                ide = [d for d in devices if d.startswith("ide-cd")]
                self.assertEqual(len(ide), 1, guest)
                self.assertIn("drive=cdrom0", ide[0])
                self.assertIn("bootindex=2", ide[0])
                cdrom = [d for d in drives if "id=cdrom0" in d]
                self.assertEqual(len(cdrom), 1, guest)
                self.assertIn("media=cdrom", cdrom[0])
                self.assertIn(str(iso.resolve()), cdrom[0])
                scsi = [d for d in devices if "virtio-scsi-pci" in d]
                self.assertEqual(len(scsi), 1, guest)
                self.assertEqual(scsi[0], "virtio-scsi-pci,id=scsi0")
                self.assertEqual(
                    sum(1 for t in argv if "id=scsi0" in t),
                    1,
                    argv,
                )
                cidata_drive = [d for d in drives if "id=cidata0" in d]
                self.assertEqual(len(cidata_drive), 1, guest)
                self.assertIn("readonly=on", cidata_drive[0])
                self.assertNotIn("bootindex", cidata_drive[0])
                scsi_cd = [d for d in devices if d.startswith("scsi-cd")]
                self.assertEqual(len(scsi_cd), 1, guest)
                self.assertIn("bus=scsi0.0", scsi_cd[0])
                drive0 = [d for d in drives if "id=drive0" in d][0]
                self.assertNotIn("media=cdrom", drive0)
                blk = [d for d in devices if "virtio-blk" in d][0]
                self.assertNotIn("media=cdrom", blk)

    def test_iso_without_cidata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with self.assertRaises(ValueError) as ctx:
                build_qemu_argv(
                    **_base_kwargs(tmp, install_iso=tmp / "install.iso")
                )
        self.assertIn("cidata", str(ctx.exception).lower())
        self.assertIn("omarchy-3", str(ctx.exception))


class UefiPflashTests(unittest.TestCase):
    def test_readonly_code_and_writable_copied_vars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            code = tmp / "OVMF_CODE.4m.fd"
            vars_fd = tmp / "run" / "OVMF_VARS.fd"
            argv = build_qemu_argv(
                **_base_kwargs(tmp, ovmf_code=code, ovmf_vars=vars_fd)
            )
        drives = _all_after(argv, "-drive")
        pflash = [d for d in drives if "if=pflash" in d]
        self.assertEqual(len(pflash), 2)
        self.assertIn("readonly=on", pflash[0])
        self.assertIn("format=raw", pflash[0])
        self.assertIn(str(code.resolve()), pflash[0])
        self.assertNotIn("readonly=on", pflash[1])
        self.assertIn("format=raw", pflash[1])
        self.assertIn(str(vars_fd.resolve()), pflash[1])


class GraphicalArgvTests(unittest.TestCase):
    def test_gtk_gl_without_vnc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = build_qemu_argv(
                **_base_kwargs(tmp, graphical=True, vnc_port=5901)
            )
        self.assertEqual(_after(argv, "-vga"), "none")
        self.assertIn("virtio-vga-gl", argv)
        self.assertNotIn("virtio-gpu-gl-pci", argv)
        self.assertEqual(_after(argv, "-display"), "gtk,gl=on")
        self.assertNotIn("-vnc", argv)
        self.assertEqual(argv.count("-vnc"), 0)
        self.assertFalse(any("0.0.0.0" in tok for tok in argv))
        self.assertIn("org.qemu.guest_agent.0", " ".join(argv))

    def test_sdl_fallback_without_vnc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = build_qemu_argv(
                **_base_kwargs(
                    tmp, graphical=True, graphical_ui="sdl", vnc_port=None
                )
            )
        self.assertEqual(_after(argv, "-display"), "sdl,gl=on")
        self.assertIn("virtio-vga-gl", argv)
        self.assertNotIn("-vnc", argv)


class VncFramebufferHelperTests(unittest.TestCase):
    def test_display_maps_to_5900_plus_and_rejects_overflow(self) -> None:
        self.assertEqual(vnc_tcp_port(1), 5901)
        self.assertEqual(vnc_tcp_port(33195), 39095)
        self.assertIsNone(vnc_tcp_port(60587))

    def test_write_png_rgb_magic_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "tiny.png"
            rgb = bytes(b for i in range(32 * 16) for b in (i % 256, 40, 80))
            write_png_rgb(dest, 32, 16, rgb)
            data = dest.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreaterEqual(len(data), 256)


if __name__ == "__main__":
    unittest.main()
