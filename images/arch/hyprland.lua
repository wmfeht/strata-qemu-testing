-- Hyprland lua-first config (0.55+). The 2026-09-01 Arch archive ships
-- hyprland 0.56.x, so this is lua rather than hyprlang `.conf`.
-- Standalone session: do not require a distro shell module.
-- Do not set AQ_NO_KMS_REQUIREMENT.
-- 0.56 env is (name, value); the 0.55 wiki's 3-arg form is rejected.
hl.monitor({ output = "", mode = "preferred", position = "auto", scale = "auto" })
hl.env("XDG_CURRENT_DESKTOP", "Hyprland")
hl.on("hyprland.start", function()
  hl.exec_cmd("dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP")
  hl.exec_cmd("systemctl --user start xdg-desktop-portal-hyprland")
end)
hl.config({
  misc = {
    disable_hyprland_logo = true,
    force_default_wallpaper = 0,
  },
})
