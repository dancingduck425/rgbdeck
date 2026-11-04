# Changelog

## 1.1.0 — 2026-11-04

- New native driver families, ported byte-for-byte from GPL-2.0 driver sources with packet-level unit tests
  (experimental until confirmed on hardware): Razer Chroma (182 device IDs incl. receivers), Logitech mice /
  headsets / Lightspeed & Unifying receivers, Corsair peripherals (legacy + iCUE V2), Corsair Lighting Node /
  Commander PRO, SteelSeries (mice, Apex keyboards, headsets, QcK Prism), HyperX (Alloy keyboards, Pulsefire
  mice, mats, QuadCast).
- Drivers are auto-discovered from `rgbdeck/hw/`; udev rules generated from their USB ID lists (411 IDs).
- Per-driver keepalive heartbeats (boards that fall back to onboard lighting without one).
- LED-count controls for resizable zones (LED strips / fan hubs), remembered per device.
- Backend worker unit tests (coalescing, ordering, error isolation, keepalive, hotplug).
- Removed every runtime trace of OpenRGB.

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
