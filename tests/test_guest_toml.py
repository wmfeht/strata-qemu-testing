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
    ISO_CIDATA_REQUIRED,
    SDDM_WAYLAND_SESSION_CANDIDATES,
    choose_sddm_session,
    covered_recipe_files,
    default_images_root,
    load_guest,
    recipe_digest,
)
from strataqemu.qemu import uses_cloud_init_seed, uses_iso_autoinstall

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


FEDORA = REPO_ROOT / "images" / "fedora-workstation"


class FedoraWorkstationRecipeTests(unittest.TestCase):
    def test_load_real_recipe(self) -> None:
        guest = load_guest("fedora-workstation")
        self.assertEqual(guest.id, "fedora-workstation")
        self.assertNotIn(".", guest.id)
        self.assertEqual(guest.arch, "x86_64")
        self.assertEqual(guest.firmware, "uefi")
        self.assertEqual(guest.source_kind, "cloud-image")
        self.assertIn(
            "download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images",
            guest.source_url,
        )
        self.assertNotIn("/current/", guest.source_url)
        self.assertNotIn("/latest/", guest.source_url)
        self.assertNotIn("44-1.7", guest.source_url)
        self.assertIsNotNone(SHA256_HEX.fullmatch(guest.source_sha256))
        self.assertEqual(guest.source_filename(), "Fedora-Cloud-Base-Generic.qcow2")
        self.assertNotIn("44-1.7", guest.source_filename())
        self.assertEqual(guest.user.name, "tester")
        self.assertIn("wheel", guest.user.groups)
        self.assertTrue(guest.session.autologin)
        self.assertEqual(guest.session.kind, "gnome")
        self.assertEqual(guest.session.display_manager, "gdm")
        self.assertEqual(guest.session.compositor, "mutter")
        self.assertTrue(guest.session.wayland)
        self.assertEqual(guest.packages.manager, "dnf")
        runtime = set(guest.packages.runtime)
        self.assertIn("gnome-screenshot", runtime)
        self.assertIn("gvfs", runtime)
        self.assertNotIn("gvfs-daemons", runtime)
        self.assertIn("xdg-desktop-portal", runtime)
        self.assertIn("xdg-desktop-portal-gnome", runtime)
        self.assertTrue(uses_cloud_init_seed(guest.id))
        self.assertTrue((guest.recipe_dir / "bootstrap.sh").is_file())
        self.assertTrue((guest.recipe_dir / "setup.sh").is_file())
        self.assertTrue((guest.recipe_dir / "user-data.yaml.tmpl").is_file())
        self.assertFalse((guest.recipe_dir / "install.sh").exists())
        self.assertFalse((guest.recipe_dir / "install.expect").exists())

    def test_bootstrap_scrape_is_not_compose_pin(self) -> None:
        text = (FEDORA / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("Fedora-Cloud-Base-Generic", text)
        self.assertIn("CHECKSUM", text)
        self.assertNotIn("44-1.7", text)
        self.assertNotIn("/latest/", text)
        self.assertNotIn("/current/", text)

    def test_directory_url_without_source_filename_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "fedora-workstation"
            shutil.copytree(FEDORA, copy)
            toml = copy / "image.toml"
            lines = [
                line
                for line in toml.read_text(encoding="utf-8").splitlines()
                if not line.startswith("source_filename")
            ]
            toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("source_filename", str(ctx.exception))

    def test_setup_sh_configures_workstation_gnome(self) -> None:
        text = (FEDORA / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("@workstation-product-environment", text)
        self.assertIn("gnome-screenshot", text)
        self.assertIn("gvfs", text)
        self.assertNotIn("gvfs-daemons", text)
        self.assertIn("xdg-desktop-portal-gnome", text)
        self.assertIn("gnome-initial-setup", text)
        self.assertIn("gnome-tour", text)
        self.assertIn("welcome-dialog-last-shown-version", text)
        self.assertIn("gnome-initial-setup-done", text)
        self.assertIn("AutomaticLogin=tester", text)
        self.assertIn("/etc/gdm/custom.conf", text)
        self.assertNotIn("/etc/gdm3/", text)
        self.assertIn("loginctl enable-linger tester", text)
        self.assertIn("qemu-guest-agent", text)
        self.assertIn("firewall-cmd", text)
        self.assertIn("--add-service=ssh", text)
        self.assertIn("rpm -qa", text)
        self.assertIn("nmcli", text)
        self.assertNotRegex(text, r"(^|[;&|]\s*)(bash\s+|sudo\s+.*)?/?install\.sh")
        self.assertNotIn("install.sh", text)

    def test_user_data_template_has_tester_wheel_nopasswd(self) -> None:
        text = (FEDORA / "user-data.yaml.tmpl").read_text(encoding="utf-8")
        self.assertIn("tester", text)
        self.assertIn("foobar", text)
        self.assertIn("NOPASSWD", text)
        self.assertIn("wheel", text)
        self.assertIn("{{SSH_AUTHORIZED_KEY}}", text)
        lowered = text.lower()
        for needle in ("ghp_", "gh auth", "gh_token", "tskey-", "tailscale"):
            self.assertNotIn(needle, lowered)


OMARCHY4 = REPO_ROOT / "images" / "omarchy-4"


class Omarchy4RecipeTests(unittest.TestCase):
    def test_load_real_recipe(self) -> None:
        guest = load_guest("omarchy-4")
        self.assertEqual(guest.id, "omarchy-4")
        self.assertNotIn(".", guest.id)
        self.assertEqual(guest.arch, "x86_64")
        self.assertEqual(guest.firmware, "uefi")
        self.assertEqual(guest.source_kind, "iso-autoinstall")
        self.assertEqual(
            guest.source_url, "https://iso.omarchy.org/omarchy-4.0.3.iso"
        )
        self.assertNotIn("/current/", guest.source_url)
        self.assertNotIn("/latest/", guest.source_url)
        self.assertEqual(
            guest.source_sha256,
            "03d60bc74306dca51f96e1a84b690871d8d606826b260edd0208962da8507d14",
        )
        self.assertIsNotNone(SHA256_HEX.fullmatch(guest.source_sha256))
        self.assertEqual(guest.source_filename(), "omarchy-4.0.3.iso")
        self.assertEqual(guest.user.name, "tester")
        self.assertIn("wheel", guest.user.groups)
        self.assertTrue(guest.session.autologin)
        self.assertEqual(guest.session.kind, "hyprland")
        self.assertEqual(guest.session.display_manager, "sddm")
        self.assertEqual(guest.session.compositor, "hyprland")
        self.assertTrue(guest.session.wayland)
        self.assertEqual(guest.packages.manager, "pacman")
        runtime = set(guest.packages.runtime)
        self.assertIn("grim", runtime)
        self.assertIn("gst-libav", runtime)
        self.assertIn("gst-plugins-good", runtime)
        self.assertIn("gtksourceview5", runtime)
        self.assertIsNotNone(guest.cidata)
        assert guest.cidata is not None
        self.assertEqual(guest.cidata.disk, "/dev/vda")
        self.assertFalse(guest.cidata.encrypt)
        self.assertTrue(uses_iso_autoinstall(guest.id))
        self.assertFalse(uses_cloud_init_seed(guest.id))
        self.assertTrue((guest.recipe_dir / "bootstrap.sh").is_file())
        self.assertTrue((guest.recipe_dir / "setup.sh").is_file())
        self.assertFalse((guest.recipe_dir / "user-data.yaml.tmpl").exists())
        self.assertFalse((guest.recipe_dir / "install.sh").exists())
        cidata = guest.recipe_dir / "cidata"
        for name in ISO_CIDATA_REQUIRED:
            self.assertTrue((cidata / name).is_file(), name)

    def test_missing_cidata_table_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-4"
            shutil.copytree(OMARCHY4, copy)
            toml = copy / "image.toml"
            lines = [
                line
                for line in toml.read_text(encoding="utf-8").splitlines()
                if not line.startswith("[cidata]")
                and not line.startswith("disk =")
                and not line.startswith("encrypt =")
            ]
            toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("[cidata]", str(ctx.exception))

    def test_missing_cidata_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-4"
            shutil.copytree(OMARCHY4, copy)
            (copy / "cidata" / "user_configuration.json").unlink()
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("user_configuration.json", str(ctx.exception))

    def test_digest_covers_cidata_dump(self) -> None:
        files = covered_recipe_files(OMARCHY4)
        names = {p.name for p in files}
        self.assertIn("image.toml", names)
        self.assertIn("bootstrap.sh", names)
        self.assertIn("setup.sh", names)
        self.assertIn("user_configuration.json", names)
        self.assertIn("user_credentials.json.tmpl", names)
        self.assertIn("user_encrypt_installation.txt", names)
        self.assertIn("authorized_keys.tmpl", names)
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-4"
            shutil.copytree(OMARCHY4, copy)
            before = Guest.load(copy).recipe_digest()
            dump = copy / "cidata" / "user_configuration.json"
            dump.write_text(
                dump.read_text(encoding="utf-8").replace(
                    "strata-omarchy-4", "strata-omarchy-4-probe"
                ),
                encoding="utf-8",
            )
            after = Guest.load(copy).recipe_digest()
            self.assertNotEqual(before, after)

    def test_setup_sh_sddm_nopasswd_grim_no_greetd(self) -> None:
        text = (OMARCHY4 / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/sddm.conf.d/99-autologin.conf", text)
        self.assertIn("User=tester", text)
        self.assertIn("Session=", text)
        self.assertIn("Relogin=true", text)
        self.assertIn("omarchy.desktop", text)
        self.assertIn("hyprland-uwsm.desktop", text)
        desktop_idx = text.index("omarchy.desktop")
        uwsm_idx = text.index("hyprland-uwsm.desktop")
        self.assertLess(desktop_idx, uwsm_idx)
        self.assertEqual(
            SDDM_WAYLAND_SESSION_CANDIDATES,
            ("omarchy.desktop", "hyprland-uwsm.desktop"),
        )
        self.assertEqual(
            choose_sddm_session(["hyprland-uwsm.desktop", "omarchy.desktop"]),
            "omarchy",
        )
        self.assertEqual(
            choose_sddm_session(["hyprland-uwsm.desktop"]),
            "hyprland-uwsm",
        )
        self.assertIn("NOPASSWD: ALL", text)
        self.assertIn("Defaults:tester !authenticate", text)
        self.assertIn("grim", text)
        self.assertIn("pacman -Sy --noconfirm", text)
        self.assertIn("gst-libav", text)
        self.assertIn("gst-plugins-good", text)
        self.assertIn("gtksourceview5", text)
        self.assertIn("loginctl enable-linger tester", text)
        self.assertIn("sddm", text)
        self.assertIn("Hyprland", text)
        self.assertNotIn("systemctl enable greetd", text)
        commands = "\n".join(
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("omarchy update", commands)
        self.assertNotIn("pacman -Syu", commands)
        self.assertNotIn("install.sh", commands)
        self.assertNotIn("--with-omarchy-keybinds", text)
        self.assertNotIn(".local/bin/strata", text)

    def test_encrypt_off_and_no_tailscale_in_cidata(self) -> None:
        encrypt = (
            OMARCHY4 / "cidata" / "user_encrypt_installation.txt"
        ).read_text(encoding="utf-8")
        self.assertEqual(encrypt.strip(), "false")
        dump = (OMARCHY4 / "cidata" / "user_configuration.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("/dev/vda", dump)
        self.assertNotIn("disk_encryption", dump)
        lowered = dump.lower()
        for needle in ("tskey-", "tailscale", "ghp_", "gh auth"):
            self.assertNotIn(needle, lowered)
        self.assertFalse((OMARCHY4 / "cidata" / "tailscale_authkey").exists())
        creds = (
            OMARCHY4 / "cidata" / "user_credentials.json.tmpl"
        ).read_text(encoding="utf-8")
        self.assertIn('"username": "tester"', creds)
        self.assertIn("$6$", creds)
        self.assertIn("root_enc_password", creds)

    def test_latest_source_url_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-4"
            shutil.copytree(OMARCHY4, copy)
            toml = copy / "image.toml"
            text = toml.read_text(encoding="utf-8")
            text = text.replace(
                load_guest("omarchy-4").source_url,
                "https://iso.omarchy.org/latest/omarchy.iso",
            )
            toml.write_text(text, encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("dated", str(ctx.exception).lower())


OMARCHY3 = REPO_ROOT / "images" / "omarchy-3"


class Omarchy3RecipeTests(unittest.TestCase):
    def test_load_real_recipe(self) -> None:
        guest = load_guest("omarchy-3")
        self.assertEqual(guest.id, "omarchy-3")
        self.assertNotIn(".", guest.id)
        self.assertEqual(guest.arch, "x86_64")
        self.assertEqual(guest.firmware, "uefi")
        self.assertEqual(guest.source_kind, "iso-autoinstall")
        self.assertEqual(
            guest.source_url, "https://iso.omarchy.org/omarchy-3.8.4.iso"
        )
        self.assertNotIn("/current/", guest.source_url)
        self.assertNotIn("/latest/", guest.source_url)
        self.assertEqual(
            guest.source_sha256,
            "7bc1dc7d98f3d088e57dc06581a494ea441fb15f3edd191360fd1696931bd895",
        )
        self.assertIsNotNone(SHA256_HEX.fullmatch(guest.source_sha256))
        self.assertEqual(guest.source_filename(), "omarchy-3.8.4.iso")
        self.assertEqual(guest.user.name, "tester")
        self.assertIn("wheel", guest.user.groups)
        self.assertTrue(guest.session.autologin)
        self.assertEqual(guest.session.kind, "hyprland")
        self.assertEqual(guest.session.compositor, "hyprland")
        self.assertTrue(guest.session.wayland)
        self.assertEqual(guest.packages.manager, "pacman")
        runtime = set(guest.packages.runtime)
        self.assertIn("grim", runtime)
        self.assertIsNotNone(guest.cidata)
        assert guest.cidata is not None
        self.assertEqual(guest.cidata.disk, "/dev/vda")
        self.assertFalse(guest.cidata.encrypt)
        self.assertTrue(uses_iso_autoinstall(guest.id))
        self.assertFalse(uses_cloud_init_seed(guest.id))
        self.assertTrue((guest.recipe_dir / "bootstrap.sh").is_file())
        self.assertTrue((guest.recipe_dir / "setup.sh").is_file())
        self.assertFalse((guest.recipe_dir / "user-data.yaml.tmpl").exists())
        self.assertFalse((guest.recipe_dir / "install.sh").exists())
        cidata = guest.recipe_dir / "cidata"
        for name in ISO_CIDATA_REQUIRED:
            self.assertTrue((cidata / name).is_file(), name)

    def test_missing_cidata_table_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-3"
            shutil.copytree(OMARCHY3, copy)
            toml = copy / "image.toml"
            lines = [
                line
                for line in toml.read_text(encoding="utf-8").splitlines()
                if not line.startswith("[cidata]")
                and not line.startswith("disk =")
                and not line.startswith("encrypt =")
            ]
            toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("[cidata]", str(ctx.exception))

    def test_missing_cidata_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-3"
            shutil.copytree(OMARCHY3, copy)
            (copy / "cidata" / "user_configuration.json").unlink()
            with self.assertRaises(GuestError) as ctx:
                Guest.load(copy)
            self.assertIn("user_configuration.json", str(ctx.exception))

    def test_digest_covers_cidata_dump(self) -> None:
        files = covered_recipe_files(OMARCHY3)
        names = {p.name for p in files}
        self.assertIn("image.toml", names)
        self.assertIn("bootstrap.sh", names)
        self.assertIn("setup.sh", names)
        self.assertIn("user_configuration.json", names)
        self.assertIn("user_credentials.json.tmpl", names)
        self.assertIn("user_encrypt_installation.txt", names)
        self.assertIn("authorized_keys.tmpl", names)
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-3"
            shutil.copytree(OMARCHY3, copy)
            before = Guest.load(copy).recipe_digest()
            dump = copy / "cidata" / "user_configuration.json"
            dump.write_text(
                dump.read_text(encoding="utf-8").replace(
                    "strata-omarchy-3", "strata-omarchy-3-probe"
                ),
                encoding="utf-8",
            )
            after = Guest.load(copy).recipe_digest()
            self.assertNotEqual(before, after)

    def test_setup_sh_probes_sddm_vs_seamless_never_greetd(self) -> None:
        text = (OMARCHY3 / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("omarchy-seamless-login.service", text)
        self.assertIn("/etc/sddm.conf.d/99-autologin.conf", text)
        self.assertIn("User=tester", text)
        self.assertIn("Session=", text)
        self.assertIn("Relogin=true", text)
        self.assertIn("omarchy.desktop", text)
        self.assertIn("hyprland-uwsm.desktop", text)
        desktop_idx = text.index("omarchy.desktop")
        uwsm_idx = text.index("hyprland-uwsm.desktop")
        self.assertLess(desktop_idx, uwsm_idx)
        self.assertEqual(
            SDDM_WAYLAND_SESSION_CANDIDATES,
            ("omarchy.desktop", "hyprland-uwsm.desktop"),
        )
        self.assertEqual(
            choose_sddm_session(["hyprland-uwsm.desktop", "omarchy.desktop"]),
            "omarchy",
        )
        self.assertIn("NOPASSWD: ALL", text)
        self.assertIn("Defaults:tester !authenticate", text)
        self.assertIn("grim", text)
        self.assertIn("loginctl enable-linger tester", text)
        self.assertIn("omarchy version", text)
        self.assertIn("Hyprland", text)
        self.assertIn(
            "both SDDM and omarchy-seamless-login.service enabled", text
        )
        self.assertNotIn("systemctl enable greetd", text)
        commands = "\n".join(
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("omarchy update", commands)
        self.assertNotIn("pacman -Syu", commands)
        self.assertNotIn("install.sh", commands)
        self.assertNotIn("--with-omarchy-keybinds", text)
        self.assertNotIn(".local/bin/strata", text)

    def test_encrypt_off_and_no_tailscale_in_cidata(self) -> None:
        encrypt = (
            OMARCHY3 / "cidata" / "user_encrypt_installation.txt"
        ).read_text(encoding="utf-8")
        self.assertEqual(encrypt.strip(), "false")
        dump = (OMARCHY3 / "cidata" / "user_configuration.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("/dev/vda", dump)
        self.assertIn('"hostname": "strata-omarchy-3"', dump)
        self.assertNotIn("disk_encryption", dump)
        self.assertNotIn("bootloader_config", dump)
        self.assertNotIn("omarchy_install", dump)
        self.assertIn('"bootloader": "Limine"', dump)
        lowered = dump.lower()
        for needle in ("tskey-", "tailscale", "ghp_", "gh auth"):
            self.assertNotIn(needle, lowered)
        self.assertFalse((OMARCHY3 / "cidata" / "tailscale_authkey").exists())
        creds = (
            OMARCHY3 / "cidata" / "user_credentials.json.tmpl"
        ).read_text(encoding="utf-8")
        self.assertIn('"username": "tester"', creds)
        self.assertIn("$6$", creds)
        self.assertIn("root_enc_password", creds)

    def test_latest_source_url_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "omarchy-3"
            shutil.copytree(OMARCHY3, copy)
            toml = copy / "image.toml"
            text = toml.read_text(encoding="utf-8")
            text = text.replace(
                load_guest("omarchy-3").source_url,
                "https://iso.omarchy.org/latest/omarchy.iso",
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
        self.assertNotIn("download.fedoraproject.org", text)
        self.assertNotIn("Fedora-Cloud-Base-Generic", text)


if __name__ == "__main__":
    unittest.main()
