"""Cloud-init NoCloud seed writer tests. No KVM, no qemu-system-x86_64."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from strataqemu.cloudinit import (
    VOLUME_ID,
    render_user_data,
    write_cidata_iso,
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


if __name__ == "__main__":
    unittest.main()
