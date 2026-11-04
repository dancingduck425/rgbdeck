# Changelog

## 1.0.0 — 2026-11-04

First public release.

- Standalone: drives devices directly over `/dev/hidraw` with its own drivers; no vendor or third-party software needed.
- Native drivers: Logitech HID++ 2.0 RGB keyboards (keys discovered from the device, ANSI/ISO/JIS),
  Glorious Model O/O-/D/D- Wireless (all three onboard profiles, battery readout),
  ASUS Aura USB mainboards.
- Per-key painting on a drawn keyboard, selection tools, gradients, rainbow, dim, Sync all devices.
- Onboard effects with speed / brightness / direction / colors; "keep on device".
- Typing effects (ripple, key glow) rendered on top of your colors.
- Presets, apply at login, tray icon, single instance, settings dialog, log file.
- Installer script, Arch PKGBUILD, pyproject for pipx.
