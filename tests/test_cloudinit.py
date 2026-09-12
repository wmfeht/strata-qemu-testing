"""Cloud-init NoCloud seed writer tests. No KVM, no qemu-system-x86_64."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strataqemu.cloudinit import (
    CloudInitError,
    VFAT_LABEL,
    VOLUME_ID,
    omarchy_mcopy_argv,
    omarchy_mkfs_vfat_argv,
    render_user_data,
    validate_omarchy_configuration,
    write_cidata_iso,
    write_omarchy_cidata_files,
    write_omarchy_cidata_iso,
    write_seed_files,
    xorriso_argv,
)
from strataqemu.guest import load_guest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    REPO_ROOT / "images" / "ubuntu-2404" / "user-data.yaml.tmpl"
).read_text(encoding="utf-8")
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPubkeyFixture tester@host"


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class XorrisoArgvTests(unittest.TestCase):
    def test_volume_id_cidata(self) -> None:
        argv = xorriso_argv("/tmp/cidata.iso")
        self.assertEqual(argv[0], "xorriso")
        self.assertEqual(_after(argv, "-V"), VOLUME_ID)
        self.assertEqual(VOLUME_ID, "cidata")
        self.assertIn("user-data", argv)
        self.assertIn("meta-data", argv)
        self.assertNotIn("qemu-system-x86_64", argv)


class RenderedUserDataTests(unittest.TestCase):
    def test_tester_foobar_pubkey_and_no_tokens(self) -> None:
        rendered = render_user_data(TEMPLATE, PUBKEY)
        self.assertIn("name: tester", rendered)
        self.assertIn("tester:foobar", rendered)
        self.assertIn("NOPASSWD", rendered)
        self.assertIn(PUBKEY, rendered)
        self.assertNotIn("{{SSH_AUTHORIZED_KEY}}", rendered)
        blob = rendered.lower()
        for needle in (
            "ghp_",
            "github_pat_",
            "gh_token",
            "github_token",
            "gh auth",
            "tskey-",
            "tailscale",
            "tailscale_authkey",
        ):
            self.assertNotIn(needle, blob)

    def test_write_seed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            user_data = render_user_data(TEMPLATE, PUBKEY)
            user_path, meta_path = write_seed_files(
                work,
                user_data=user_data,
                instance_id="ubuntu-2404",
                hostname="ubuntu-2404",
            )
            self.assertEqual(user_path.read_text(encoding="utf-8"), user_data)
            meta = meta_path.read_text(encoding="utf-8")
            self.assertIn("instance-id: ubuntu-2404", meta)
            self.assertIn("local-hostname: ubuntu-2404", meta)


class XorrisoIsoTests(unittest.TestCase):
    def test_writes_iso_with_volume_id_when_xorriso_present(self) -> None:
        xorriso = shutil.which("xorriso")
        if xorriso is None:
            self.skipTest("xorriso not on PATH; argv + rendered files still gate")
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cidata.iso"
            write_cidata_iso(
                dest,
                template=TEMPLATE,
                pubkey=PUBKEY,
                instance_id="ubuntu-2404",
                hostname="ubuntu-2404",
            )
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 0)
            toc = subprocess.run(
                [xorriso, "-indev", str(dest), "-toc"],
                check=False,
                capture_output=True,
                text=True,
            )
            blob = (toc.stdout + toc.stderr).lower()
            self.assertIn("cidata", blob)
            isoinfo = shutil.which("isoinfo")
            if isoinfo is not None:
                info = subprocess.run(
                    [isoinfo, "-d", "-i", str(dest)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("cidata", (info.stdout + info.stderr).lower())

    def test_recipe_template_roundtrip(self) -> None:
        guest = load_guest("ubuntu-2404")
        tmpl = (guest.recipe_dir / "user-data.yaml.tmpl").read_text(encoding="utf-8")
        self.assertEqual(tmpl, TEMPLATE)

    def test_arch_template_renders_tester_wheel(self) -> None:
        guest = load_guest("arch")
        tmpl = (guest.recipe_dir / "user-data.yaml.tmpl").read_text(encoding="utf-8")
        rendered = render_user_data(tmpl, PUBKEY)
        self.assertIn("name: tester", rendered)
        self.assertIn("wheel", rendered)
        self.assertIn("NOPASSWD", rendered)
        self.assertIn(PUBKEY, rendered)
        self.assertNotIn("{{SSH_AUTHORIZED_KEY}}", rendered)
        self.assertNotIn("omarchy", rendered.lower())

    def test_fedora_template_renders_tester_wheel(self) -> None:
        guest = load_guest("fedora-workstation")
        tmpl = (guest.recipe_dir / "user-data.yaml.tmpl").read_text(encoding="utf-8")
        rendered = render_user_data(tmpl, PUBKEY)
        self.assertIn("name: tester", rendered)
        self.assertIn("tester:foobar", rendered)
        self.assertIn("wheel", rendered)
        self.assertIn("NOPASSWD", rendered)
        self.assertIn(PUBKEY, rendered)
        self.assertNotIn("{{SSH_AUTHORIZED_KEY}}", rendered)
        self.assertNotIn("install.sh", rendered)


class OmarchyCidataTests(unittest.TestCase):
    def test_vfat_argv_is_omarchy_names_not_nocloud(self) -> None:
        dest = "/tmp/cidata.img"
        mkfs = omarchy_mkfs_vfat_argv(dest)
        self.assertEqual(mkfs[0], "mkfs.vfat")
        self.assertEqual(_after(mkfs, "-n"), VFAT_LABEL)
        self.assertEqual(VFAT_LABEL, "CIDATA")
        self.assertIn(dest, mkfs)
        mcopy = omarchy_mcopy_argv(dest)
        self.assertEqual(mcopy[0], "mcopy")
        self.assertEqual(_after(mcopy, "-i"), dest)
        self.assertIn("user_configuration.json", mcopy)
        self.assertIn("user_credentials.json", mcopy)
        self.assertIn("user_encrypt_installation.txt", mcopy)
        self.assertIn("authorized_keys", mcopy)
        self.assertIn("::/", mcopy)
        self.assertNotIn("user-data", mcopy)
        self.assertNotIn("meta-data", mcopy)
        self.assertNotIn("qemu-system-x86_64", mkfs + mcopy)
        self.assertNotIn("xorriso", mkfs + mcopy)

    def test_dump_has_vda_no_encryption_no_tailscale(self) -> None:
        guest = load_guest("omarchy-4")
        dump_path = guest.recipe_dir / "cidata" / "user_configuration.json"
        data = json.loads(dump_path.read_text(encoding="utf-8"))
        validate_omarchy_configuration(data)
        self.assertEqual(data["omarchy_install"]["mode"], "full_disk")
        self.assertIn("bootloader_config", data)
        self.assertEqual(
            data["disk_config"]["device_modifications"][0]["device"],
            "/dev/vda",
        )
        mib = 1024 * 1024
        parts = data["disk_config"]["device_modifications"][0]["partitions"]
        for part in parts:
            self.assertEqual(part["start"]["value"] % mib, 0, part["start"])
            self.assertEqual(part["size"]["value"] % mib, 0, part["size"])
        disk_bytes = 40 * 1024 * 1024 * 1024
        boot_start = mib
        boot_size = 2 * 1024 * mib
        main_start = boot_start + boot_size
        main_size = disk_bytes - main_start - mib
        self.assertEqual(parts[0]["start"]["value"], boot_start)
        self.assertEqual(parts[0]["size"]["value"], boot_size)
        self.assertEqual(parts[1]["start"]["value"], main_start)
        self.assertEqual(parts[1]["size"]["value"], main_size)
        self.assertNotIn("disk_encryption", data)
        self.assertNotIn("disk_encryption", data["disk_config"])
        blob = dump_path.read_text(encoding="utf-8").lower()
        for needle in ("tskey-", "tailscale", "user-data", "meta-data"):
            self.assertNotIn(needle, blob)

    def test_unaligned_partition_size_fails_closed(self) -> None:
        guest = load_guest("omarchy-4")
        dump_path = guest.recipe_dir / "cidata" / "user_configuration.json"
        data = json.loads(dump_path.read_text(encoding="utf-8"))
        data["disk_config"]["device_modifications"][0]["partitions"][1]["size"][
            "value"
        ] = 40800104448
        with self.assertRaises(CloudInitError) as ctx:
            validate_omarchy_configuration(data)
        self.assertIn("1MiB-aligned", str(ctx.exception))

    def test_credentials_hash_is_openssl_passwd_6_of_foobar(self) -> None:
        guest = load_guest("omarchy-4")
        creds = json.loads(
            (guest.recipe_dir / "cidata" / "user_credentials.json.tmpl").read_text(
                encoding="utf-8"
            )
        )
        digest = creds["root_enc_password"]
        self.assertEqual(creds["users"][0]["enc_password"], digest)
        self.assertTrue(digest.startswith("$6$"), digest)
        parts = digest.split("$")
        self.assertGreaterEqual(len(parts), 4, digest)
        salt = parts[2]
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("openssl not on PATH; $6$ hash still gated")
        proc = subprocess.run(
            [openssl, "passwd", "-6", "-salt", salt, "foobar"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.stdout.strip(), digest)

    def test_write_files_fill_pubkey_and_keep_tester_hash(self) -> None:
        guest = load_guest("omarchy-4")
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            paths = write_omarchy_cidata_files(
                work, recipe_dir=guest.recipe_dir, pubkey=PUBKEY
            )
            keys = paths["authorized_keys"].read_text(encoding="utf-8")
            self.assertIn(PUBKEY, keys)
            self.assertNotIn("{{SSH_AUTHORIZED_KEY}}", keys)
            creds = paths["user_credentials.json"].read_text(encoding="utf-8")
            self.assertIn('"username": "tester"', creds)
            self.assertIn("$6$", creds)
            self.assertIn("root_enc_password", creds)
            encrypt = paths["user_encrypt_installation.txt"].read_text(
                encoding="utf-8"
            )
            self.assertEqual(encrypt.strip(), "false")
            names = {p.name for p in work.iterdir()}
            self.assertEqual(
                names,
                {
                    "user_configuration.json",
                    "user_credentials.json",
                    "user_encrypt_installation.txt",
                    "authorized_keys",
                },
            )
            self.assertNotIn("user-data", names)
            self.assertNotIn("meta-data", names)

    def test_writes_vfat_labeled_cidata_when_mtools_present(self) -> None:
        mkfs = shutil.which("mkfs.vfat")
        mcopy = shutil.which("mcopy")
        if mkfs is None or mcopy is None:
            self.skipTest("mkfs.vfat/mcopy not on PATH; argv + rendered files still gate")
        guest = load_guest("omarchy-4")
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cidata.img"
            write_omarchy_cidata_iso(
                dest, recipe_dir=guest.recipe_dir, pubkey=PUBKEY
            )
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 0)
            probe = subprocess.run(
                ["blkid", "-o", "udev", str(dest)],
                check=False,
                capture_output=True,
                text=True,
            )
            blob = (probe.stdout + probe.stderr).upper()
            self.assertIn("ID_FS_LABEL=CIDATA", blob)
            self.assertIn("ID_FS_TYPE=VFAT", blob)
            env = os.environ.copy()
            env["MTOOLS_SKIP_CHECK"] = "1"
            listing = subprocess.run(
                ["mdir", "-i", str(dest), "-a"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            listed = (listing.stdout + listing.stderr).lower()
            self.assertIn("user_configuration.json", listed)
            self.assertIn("user_credentials.json", listed)
            self.assertIn("authorized_keys", listed)
            self.assertNotIn("user-data", listed)
            self.assertNotIn("meta-data", listed)


class Omarchy3CidataTests(unittest.TestCase):
    def test_dump_has_vda_no_encryption_3x_schema(self) -> None:
        guest = load_guest("omarchy-3")
        dump_path = guest.recipe_dir / "cidata" / "user_configuration.json"
        data = json.loads(dump_path.read_text(encoding="utf-8"))
        validate_omarchy_configuration(data)
        self.assertEqual(data["hostname"], "strata-omarchy-3")
        self.assertEqual(data["bootloader"], "Limine")
        self.assertNotIn("bootloader_config", data)
        self.assertNotIn("omarchy_install", data)
        self.assertEqual(
            data["disk_config"]["device_modifications"][0]["device"],
            "/dev/vda",
        )
        mib = 1024 * 1024
        parts = data["disk_config"]["device_modifications"][0]["partitions"]
        for part in parts:
            self.assertEqual(part["start"]["value"] % mib, 0, part["start"])
            self.assertEqual(part["size"]["value"] % mib, 0, part["size"])
        disk_bytes = 40 * 1024 * 1024 * 1024
        boot_start = mib
        boot_size = 2 * 1024 * mib
        main_start = boot_start + boot_size
        main_size = disk_bytes - main_start - mib
        self.assertEqual(parts[0]["start"]["value"], boot_start)
        self.assertEqual(parts[0]["size"]["value"], boot_size)
        self.assertEqual(parts[1]["start"]["value"], main_start)
        self.assertEqual(parts[1]["size"]["value"], main_size)
        self.assertNotIn("disk_encryption", data)
        self.assertNotIn("disk_encryption", data["disk_config"])
        blob = dump_path.read_text(encoding="utf-8").lower()
        for needle in ("tskey-", "tailscale", "user-data", "meta-data"):
            self.assertNotIn(needle, blob)

    def test_4x_keys_on_3x_dump_fail_closed(self) -> None:
        guest = load_guest("omarchy-3")
        dump_path = guest.recipe_dir / "cidata" / "user_configuration.json"
        data = json.loads(dump_path.read_text(encoding="utf-8"))
        data["omarchy_install"] = {"mode": "full_disk"}
        with self.assertRaises(CloudInitError) as ctx:
            validate_omarchy_configuration(data)
        self.assertIn("omarchy_install", str(ctx.exception))

    def test_unaligned_partition_size_fails_closed(self) -> None:
        guest = load_guest("omarchy-3")
        dump_path = guest.recipe_dir / "cidata" / "user_configuration.json"
        data = json.loads(dump_path.read_text(encoding="utf-8"))
        data["disk_config"]["device_modifications"][0]["partitions"][1]["size"][
            "value"
        ] = 40800104448
        with self.assertRaises(CloudInitError) as ctx:
            validate_omarchy_configuration(data)
        self.assertIn("1MiB-aligned", str(ctx.exception))

    def test_credentials_hash_is_openssl_passwd_6_of_foobar(self) -> None:
        guest = load_guest("omarchy-3")
        creds = json.loads(
            (guest.recipe_dir / "cidata" / "user_credentials.json.tmpl").read_text(
                encoding="utf-8"
            )
        )
        digest = creds["root_enc_password"]
        self.assertEqual(creds["users"][0]["enc_password"], digest)
        self.assertEqual(creds["users"][0]["username"], "tester")
        self.assertTrue(digest.startswith("$6$"), digest)
        parts = digest.split("$")
        self.assertGreaterEqual(len(parts), 4, digest)
        salt = parts[2]
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("openssl not on PATH; $6$ hash still gated")
        proc = subprocess.run(
            [openssl, "passwd", "-6", "-salt", salt, "foobar"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.stdout.strip(), digest)

    def test_write_files_fill_pubkey_and_keep_tester_hash(self) -> None:
        guest = load_guest("omarchy-3")
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            paths = write_omarchy_cidata_files(
                work, recipe_dir=guest.recipe_dir, pubkey=PUBKEY
            )
            keys = paths["authorized_keys"].read_text(encoding="utf-8")
            self.assertIn(PUBKEY, keys)
            self.assertNotIn("{{SSH_AUTHORIZED_KEY}}", keys)
            creds = paths["user_credentials.json"].read_text(encoding="utf-8")
            self.assertIn('"username": "tester"', creds)
            self.assertIn("$6$", creds)
            self.assertIn("root_enc_password", creds)
            encrypt = paths["user_encrypt_installation.txt"].read_text(
                encoding="utf-8"
            )
            self.assertEqual(encrypt.strip(), "false")
            names = {p.name for p in work.iterdir()}
            self.assertEqual(
                names,
                {
                    "user_configuration.json",
                    "user_credentials.json",
                    "user_encrypt_installation.txt",
                    "authorized_keys",
                },
            )
            self.assertNotIn("user-data", names)
            self.assertNotIn("meta-data", names)

    def test_writes_vfat_labeled_cidata_when_mtools_present(self) -> None:
        mkfs = shutil.which("mkfs.vfat")
        mcopy = shutil.which("mcopy")
        if mkfs is None or mcopy is None:
            self.skipTest("mkfs.vfat/mcopy not on PATH; argv + rendered files still gate")
        guest = load_guest("omarchy-3")
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cidata.img"
            write_omarchy_cidata_iso(
                dest, recipe_dir=guest.recipe_dir, pubkey=PUBKEY
            )
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 0)
            probe = subprocess.run(
                ["blkid", "-o", "udev", str(dest)],
                check=False,
                capture_output=True,
                text=True,
            )
            blob = (probe.stdout + probe.stderr).upper()
            self.assertIn("ID_FS_LABEL=CIDATA", blob)
            self.assertIn("ID_FS_TYPE=VFAT", blob)
            env = os.environ.copy()
            env["MTOOLS_SKIP_CHECK"] = "1"
            listing = subprocess.run(
                ["mdir", "-i", str(dest), "-a"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            listed = (listing.stdout + listing.stderr).lower()
            self.assertIn("user_configuration.json", listed)
            self.assertIn("user_credentials.json", listed)
            self.assertIn("authorized_keys", listed)
            self.assertNotIn("user-data", listed)
            self.assertNotIn("meta-data", listed)


if __name__ == "__main__":
    unittest.main()
