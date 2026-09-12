"""Guest recipe loader tests. No KVM, no qemu-system-x86_64."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from strataqemu.guest import (
    SHA256_HEX,
    Guest,
    GuestError,
    covered_recipe_files,
    default_images_root,
    load_guest,
    recipe_digest,
)
from strataqemu.qemu import uses_cloud_init_seed

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPE = REPO_ROOT / "images" / "ubuntu-2404"


class Ubuntu2404RecipeTests(unittest.TestCase):
    def test_load_real_recipe(self) -> None:
        guest = load_guest("ubuntu-2404")
        self.assertEqual(guest.id, "ubuntu-2404")
        self.assertNotIn(".", guest.id)
        self.assertEqual(guest.arch, "x86_64")
        self.assertEqual(guest.firmware, "uefi")
        self.assertEqual(guest.source_kind, "cloud-image")
        self.assertIn("noble-server-cloudimg-amd64.img", guest.source_url)
        self.assertNotIn("/current/", guest.source_url)
        self.assertIsNotNone(SHA256_HEX.fullmatch(guest.source_sha256))
        self.assertEqual(guest.user.name, "tester")
        self.assertIn("sudo", guest.user.groups)
        self.assertTrue(guest.session.autologin)
        self.assertEqual(guest.session.display_manager, "gdm")
        self.assertTrue(uses_cloud_init_seed(guest.id))
        self.assertTrue((guest.recipe_dir / "bootstrap.sh").is_file())
        self.assertTrue((guest.recipe_dir / "setup.sh").is_file())
        self.assertTrue((guest.recipe_dir / "user-data.yaml.tmpl").is_file())
        self.assertFalse((guest.recipe_dir / "install.sh").exists())

    def test_missing_source_sha256_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "ubuntu-2404"
            shutil.copytree(RECIPE, copy)
            toml = copy / "image.toml"
            lines = [
                line
                for line in toml.read_text(encoding="utf-8").splitlines()
                if not line.startswith("source_sha256")
            ]
            toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("source_sha256", str(ctx.exception))

    def test_empty_source_sha256_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "ubuntu-2404"
            shutil.copytree(RECIPE, copy)
            toml = copy / "image.toml"
            text = toml.read_text(encoding="utf-8")
            text = text.replace(
                f'source_sha256 = "{Guest.load(copy).source_sha256}"',
                'source_sha256 = ""',
            )
            toml.write_text(text, encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("source_sha256", str(ctx.exception))

    def test_recipe_digest_changes_when_setup_sh_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "ubuntu-2404"
            shutil.copytree(RECIPE, copy)
            before = Guest.load(copy).recipe_digest()
            self.assertIsNotNone(SHA256_HEX.fullmatch(before))
            setup = copy / "setup.sh"
            setup.write_text(
                setup.read_text(encoding="utf-8") + "\n# digest-probe\n",
                encoding="utf-8",
            )
            after = Guest.load(copy).recipe_digest()
            self.assertNotEqual(before, after)
            self.assertIsNotNone(SHA256_HEX.fullmatch(after))

    def test_digest_covers_toml_scripts_and_templates(self) -> None:
        files = covered_recipe_files(RECIPE)
        names = {p.name for p in files}
        self.assertIn("image.toml", names)
        self.assertIn("bootstrap.sh", names)
        self.assertIn("setup.sh", names)
        self.assertIn("user-data.yaml.tmpl", names)
        digest = recipe_digest(RECIPE)
        self.assertEqual(digest, load_guest("ubuntu-2404").recipe_digest())

    def test_golden_digest_includes_source_checksum(self) -> None:
        guest = load_guest("ubuntu-2404")
        self.assertIsNotNone(SHA256_HEX.fullmatch(guest.golden_digest()))
        self.assertNotEqual(guest.golden_digest(), guest.recipe_digest())

    def test_snapshot_is_not_older_than_cloud_image(self) -> None:
        """20260901 snapshot + 20260911 cloudimg: gtk 24.04.28 vs core 24.04.29."""
        guest = load_guest("ubuntu-2404")
        image_day = re.search(r"/noble/(\d{8})/", guest.source_url)
        snap_day = re.search(
            r"/ubuntu/(\d{8})T", guest.packages.snapshot_url or ""
        )
        self.assertIsNotNone(image_day, guest.source_url)
        self.assertIsNotNone(snap_day, guest.packages.snapshot_url)
        assert image_day is not None
        assert snap_day is not None
        self.assertGreaterEqual(
            snap_day.group(1),
            image_day.group(1),
            "APT snapshot must not predate the cloud image date",
        )

    def test_setup_sh_configures_noble_desktop(self) -> None:
        text = (RECIPE / "setup.sh").read_text(encoding="utf-8")
        guest = load_guest("ubuntu-2404")
        self.assertIn("ubuntu-desktop-minimal", text)
        self.assertIn("gnome-screenshot", text)
        self.assertIn("gnome-initial-setup", text)
        self.assertIn("gnome-tour", text)
        self.assertIn("gnome-initial-setup-done", text)
        self.assertIn("AutomaticLogin=tester", text)
        self.assertIn("/etc/gdm3/custom.conf", text)
        self.assertIn("loginctl enable-linger tester", text)
        self.assertIn("qemu-guest-agent", text)
        self.assertIn(guest.packages.snapshot_url or "", text)
        self.assertIn("*.sources", text)
        self.assertIn("Allow-Downgrades", text)
        self.assertNotRegex(text, r"(^|[;&|]\s*)(bash\s+|sudo\s+.*)?/?install\.sh")

    def test_user_data_template_has_tester_and_placeholder(self) -> None:
        text = (RECIPE / "user-data.yaml.tmpl").read_text(encoding="utf-8")
        self.assertIn("tester", text)
        self.assertIn("foobar", text)
        self.assertIn("NOPASSWD", text)
        self.assertIn("{{SSH_AUTHORIZED_KEY}}", text)
        lowered = text.lower()
        for needle in ("ghp_", "gh auth", "gh_token", "tskey-", "tailscale"):
            self.assertNotIn(needle, lowered)

    def test_images_root(self) -> None:
        self.assertEqual(default_images_root(), REPO_ROOT / "images")

    def test_no_recipe_fails_closed(self) -> None:
        with self.assertRaises(GuestError) as ctx:
            load_guest("not-a-guest")
        self.assertIn("no recipe", str(ctx.exception))


ARCH = REPO_ROOT / "images" / "arch"


class ArchRecipeTests(unittest.TestCase):
    def test_load_real_recipe(self) -> None:
        guest = load_guest("arch")
        self.assertEqual(guest.id, "arch")
        self.assertNotIn(".", guest.id)
        self.assertEqual(guest.arch, "x86_64")
        self.assertEqual(guest.firmware, "bios")
        self.assertEqual(guest.source_kind, "cloud-image")
        self.assertRegex(
            guest.source_url,
            r"Arch-Linux-x86_64-cloudimg-\d{8}",
        )
        self.assertNotIn("/current/", guest.source_url)
        self.assertNotIn("/latest/", guest.source_url)
        self.assertIsNotNone(SHA256_HEX.fullmatch(guest.source_sha256))
        self.assertEqual(guest.user.name, "tester")
        self.assertIn("wheel", guest.user.groups)
        self.assertTrue(guest.session.autologin)
        self.assertEqual(guest.session.kind, "hyprland")
        self.assertEqual(guest.session.display_manager, "greetd")
        self.assertEqual(guest.session.compositor, "hyprland")
        self.assertEqual(guest.packages.manager, "pacman")
        runtime = set(guest.packages.runtime)
        for pkg in (
            "hyprland",
            "greetd",
            "xdg-desktop-portal-hyprland",
            "xdg-desktop-portal",
            "jq",
            "grim",
            "ttf-liberation",
        ):
            self.assertIn(pkg, runtime)
        self.assertNotIn("omarchy", runtime)
        self.assertNotIn("gnome", guest.session.kind)
        self.assertNotEqual(guest.session.display_manager, "gdm")
        self.assertTrue(uses_cloud_init_seed(guest.id))
        self.assertTrue((guest.recipe_dir / "bootstrap.sh").is_file())
        self.assertTrue((guest.recipe_dir / "setup.sh").is_file())
        self.assertTrue((guest.recipe_dir / "user-data.yaml.tmpl").is_file())
        self.assertTrue((guest.recipe_dir / "greetd-config.toml").is_file())
        self.assertTrue((guest.recipe_dir / "hyprland.lua").is_file())
        self.assertFalse((guest.recipe_dir / "hyprland.conf").exists())
        self.assertFalse((guest.recipe_dir / "install.sh").exists())

    def test_archive_day_matches_cloudimg_date(self) -> None:
        guest = load_guest("arch")
        image_day = re.search(r"cloudimg-(\d{8})", guest.source_url)
        snap = guest.packages.snapshot_url or ""
        snap_day = re.search(r"/repos/(\d{4})/(\d{2})/(\d{2})/", snap)
        self.assertIsNotNone(image_day, guest.source_url)
        self.assertIsNotNone(snap_day, snap)
        assert image_day is not None
        assert snap_day is not None
        self.assertEqual(
            image_day.group(1),
            "".join(snap_day.group(i) for i in (1, 2, 3)),
        )
        self.assertIn("archive.archlinux.org", snap)
        self.assertNotIn("geo.mirror.pkgbuild.com", snap)

    def test_greetd_names_committed_lua(self) -> None:
        greetd = (ARCH / "greetd-config.toml").read_text(encoding="utf-8")
        lua = (ARCH / "hyprland.lua").read_text(encoding="utf-8")
        self.assertIn(
            "Hyprland --config /home/tester/.config/hypr/hyprland.lua",
            greetd,
        )
        self.assertIn('user = "tester"', greetd)
        code = "\n".join(
            line
            for line in lua.splitlines()
            if line.strip() and not line.lstrip().startswith("--")
        )
        self.assertNotRegex(code, r"require\s*\(?['\"]omarchy['\"]")
        self.assertNotIn("AQ_NO_KMS_REQUIREMENT", code)
        self.assertIn("hl.monitor", lua)
        self.assertIn("hl.env", lua)
        self.assertIn('hl.env("XDG_CURRENT_DESKTOP", "Hyprland")', lua)

    def test_digest_covers_greetd_and_lua(self) -> None:
        files = covered_recipe_files(ARCH)
        names = {p.name for p in files}
        self.assertIn("image.toml", names)
        self.assertIn("bootstrap.sh", names)
        self.assertIn("setup.sh", names)
        self.assertIn("user-data.yaml.tmpl", names)
        self.assertIn("greetd-config.toml", names)
        self.assertIn("hyprland.lua", names)
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "arch"
            shutil.copytree(ARCH, copy)
            before = Guest.load(copy).recipe_digest()
            lua = copy / "hyprland.lua"
            lua.write_text(
                lua.read_text(encoding="utf-8") + "\n-- digest-probe\n",
                encoding="utf-8",
            )
            after_lua = Guest.load(copy).recipe_digest()
            self.assertNotEqual(before, after_lua)
            greetd = copy / "greetd-config.toml"
            greetd.write_text(
                greetd.read_text(encoding="utf-8") + "\n# digest-probe\n",
                encoding="utf-8",
            )
            after_greetd = Guest.load(copy).recipe_digest()
            self.assertNotEqual(after_lua, after_greetd)

    def test_setup_sh_dated_archive_fail_closed(self) -> None:
        text = (ARCH / "setup.sh").read_text(encoding="utf-8")
        guest = load_guest("arch")
        self.assertIn("archive.archlinux.org", text)
        self.assertIn("Retarget", text)
        self.assertNotIn("geo.mirror.pkgbuild.com", text)
        self.assertIn("hyprland", text)
        self.assertIn("greetd", text)
        self.assertIn("grim", text)
        self.assertIn("jq", text)
        self.assertIn("xdg-desktop-portal-hyprland", text)
        self.assertIn("ttf-liberation", text)
        self.assertIn("loginctl enable-linger tester", text)
        self.assertIn("hyprctl configerrors", text)
        self.assertIn("Type=wayland", text)
        self.assertIn(guest.packages.snapshot_url or "", text)
        self.assertNotIn("install.sh", text)
        install_line = "\n".join(
            line for line in text.splitlines() if "pacman -S" in line
        )
        self.assertNotIn("omarchy", install_line)
        self.assertNotIn("strata", install_line)

    def test_user_data_template_has_tester_wheel_nopasswd(self) -> None:
        text = (ARCH / "user-data.yaml.tmpl").read_text(encoding="utf-8")
        self.assertIn("tester", text)
        self.assertIn("foobar", text)
        self.assertIn("NOPASSWD", text)
        self.assertIn("wheel", text)
        self.assertIn("{{SSH_AUTHORIZED_KEY}}", text)
        lowered = text.lower()
        for needle in ("ghp_", "gh auth", "gh_token", "tskey-", "tailscale", "omarchy"):
            self.assertNotIn(needle, lowered)

    def test_latest_source_url_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "arch"
            shutil.copytree(ARCH, copy)
            toml = copy / "image.toml"
            text = toml.read_text(encoding="utf-8")
            text = text.replace(
                load_guest("arch").source_url,
                "https://fastly.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2",
            )
            toml.write_text(text, encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("dated", str(ctx.exception).lower())


class MiseBootstrapBoundaryTests(unittest.TestCase):
    def test_mise_bootstrap_does_not_download_noble_cloudimg(self) -> None:
        text = (REPO_ROOT / "mise.toml").read_text(encoding="utf-8")
        self.assertNotIn("noble-server-cloudimg", text)
        self.assertNotIn("cloud-images.ubuntu.com", text)


if __name__ == "__main__":
    unittest.main()
