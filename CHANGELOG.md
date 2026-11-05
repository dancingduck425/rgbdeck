# Changelog

## 1.3.2 — 2026-11-04

- **Search my network** in the Lights panel (runs by itself when the panel opens and nothing is connected):
  every address of the local network is asked directly, so lights are found where the router does not pass
  multicast on, and nobody has to know an address. Govee hardware is also recognised by its MAC prefix, so a
  light that has LAN Control switched off is listed as exactly that and connects by itself once it is on.
  Only private networks are searched, at most 1024 addresses, never VPN or container interfaces.

## 1.3.1 — 2026-11-04

- **Lights panel**: a 💡 Lights button next to the device cards (also File → Network lights…) lists every Govee
  light with its state, explains how to connect one, and checks an address you type. It tells apart a light
  that answers, one that is on the network but refuses local control (LAN Control still off in the Govee Home
  app), one that is unreachable, and one that takes commands without answering (firewall).
- An address that refuses commands is kept on the list and picked up as soon as LAN Control is switched on,
  instead of showing as a device card that does nothing.

## 1.3.0 — 2026-11-04

- **Govee Wi-Fi lights** through Govee's LAN API (local UDP; no cloud, no account). Lights with "LAN Control"
  switched on in the Govee Home app are found automatically, remembered, and can also be added by address.
  Color, brightness, warm / neutral / cool white, on/off for every LAN-capable model; per-segment painting and
  the light's own scenes for the 272 models in the built-in table. Updates are rate-limited per light and the
  final state is sent twice, since UDP gives no delivery guarantee. Developed against a simulated light —
  **not yet confirmed on real hardware**, hence marked experimental.
- **Room lights pulse with typing effects**: while ripple / glow runs on the keyboard, network lights flash
  toward the color of each key press and fall back to their own color (Settings; lights that are off stay off).
- The backend understands devices that are not USB (`fingerprint()` / `probed()`), so further network lights
  are one driver each.
- Dialogs, menus and tooltips are dark on every desktop theme. On a light theme (Windows' default) Settings and
  Help were light text on a white background.
- Typing-effect controls are only offered for devices that have keys.

Linux users with a firewall: allow incoming UDP 4002 from your LAN, or add the light by address.

## 1.2.3 — 2026-11-04

Windows audit (still untested on real Windows hardware — please report what you see):

- A stale `control.json` (crash, power loss, or simply the previous exit) could make the next start exit
  silently if some other program now owned that port: only RGB Deck's own replies count as "already running",
  and the file is removed on exit.
- Input reports longer than 64 bytes were clipped (ASUS Aura, Corsair, HyperX, SteelSeries replies).
- Keyboard / mouse system collections are opened too (Windows allows that without read/write access), so
  drivers that send feature reports there (Razer, some Corsair / SteelSeries) can reach them; report-id-less
  reports try every collection instead of only the first.
- hidapi 0.15 is required: 0.14 has no `get_report_descriptor`, which left report-id routing to trial and error.
- Typing effects: F13–F24 and JIS keys mapped to the wrong LEDs (scan codes above F12 don't match Linux codes).
- "Open log" used `xdg-open` on Windows; a hotplug poll error no longer triggers an endless rescan loop.

## 1.2.2 — 2026-11-04

- **Windows: the 1.2.0 / 1.2.1 builds could not talk to any device.** The hidapi module is loaded dynamically,
  so PyInstaller never bundled it and every scan failed; `RGBDeck.exe` now ships it and `--selftest` fails
  the build if the HID backend is unusable.
- Windows: collections of one USB interface were not regrouped (each also carries its own instance-id suffix,
  `col02` ↔ `&0&0001`), which would have left Logitech keyboards undetected.

## 1.2.1 — 2026-11-04

- **Keep my look when I close RGB Deck** (Settings, on by default): on exit, a single color becomes a saved
  Static effect in the device's memory, a running onboard effect is re-sent with the save flag, and a
  multi-color key layout is written to the "Last look" preset and re-applied at login (the keyboard cannot
  store per-key colors itself). Verified on the G513: the stored static color survives a USB reset.
- README documents measured CPU / memory use.

## 1.2.0 — 2026-11-04

- **Windows support.** The same drivers run on top of hidapi: Windows exposes every HID collection as its own
  device, so the backend regroups them per USB interface, routes each report ID to the collection that owns it
  (the Logitech 0x11 / 0x12 split) and merges their input reports. No driver or udev rule is needed; the vendor
  software must be closed. Typing effects use a low-level keyboard hook. Login entries use the Run registry key,
  settings live in `%APPDATA%\rgbdeck`, single instance via a loopback port with a per-user token.
- Windows CI job: unit tests, a PyInstaller `RGBDeck.exe`, an Inno Setup installer, both attached to releases.
- `--selftest` flag (used by CI to check a bundled build), `.ico` app icon.

## 1.1.1 — 2026-11-04

- udev rule now covers the verified Logitech keyboard, Glorious mouse and ASUS Aura IDs too (they were missing,
  so those devices only worked where another package's rule granted access). 433 IDs.
- Installer writes the rule with mode 644 regardless of the caller's umask (udev ignores unreadable rules).
- SPDX license and copyright headers on every source file; ported drivers credit their OpenRGB origin.
- Removed local filesystem paths from two driver docstrings.

## 1.1.0 — 2026-11-04

- New native driver families, ported byte-for-byte from GPL-2.0 driver sources with packet-level unit tests
  (experimental until confirmed on hardware): Razer Chroma (182 device IDs incl. receivers), Logitech mice /
  headsets / Lightspeed & Unifying receivers, Corsair peripherals (legacy + iCUE V2), Corsair Lighting Node /
  Commander PRO, SteelSeries (mice, Apex keyboards, headsets, QcK Prism), HyperX (Alloy keyboards, Pulsefire
  mice, mats, QuadCast).
- Drivers are auto-discovered from `rgbdeck/hw/`; udev rules generated from their USB ID lists (433 IDs).
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
