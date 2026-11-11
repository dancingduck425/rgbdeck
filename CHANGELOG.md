# Changelog

## 1.10.1 — 2026-11-11

- **Windows: opening Lights, ticking "Look for Govee lights" or pressing Search could close the whole app.**
  All three start a Bluetooth scan, and Qt's Bluetooth code for Windows ends the process it runs in when
  Windows refuses one of its calls (its own source says so; on Windows 11 it is also known to hang). On
  Windows, Bluetooth now runs in a small **helper process** next to RGB Deck. If the helper dies or stops
  answering, RGB Deck keeps running: Bluetooth lights count as out of reach, the Lights panel says that
  Bluetooth stopped on this PC, and `rgbdeck.log` records the exit code and what was being done. The helper is
  restarted after a pause; after three losses in a row Bluetooth is left alone until the next start. Network
  (LAN) lights are not affected either way.
- Which Windows call fails on the PC this was reported from is not known yet: this release keeps the app
  alive and makes the log say it. Whether Bluetooth lights then work on that PC is a separate question the
  log will answer.
- Linux keeps Bluetooth inside the app as before (`RGBDECK_BLE_HELPER=1` moves it to the helper there too;
  checked that way against a real H618F: scan, connect, queries, writes, clean shutdown).
- A Bluetooth scan whose end is never reported counts as over after 15 s instead of leaving "Searching…"
  up, two scan requests at the same moment can no longer overlap, and a Bluetooth thread that does not stop in
  time is left alone rather than killed.
- The build check (`--selftest`) starts the helper from the packaged program, lets it scan, and kills it in
  the middle of a scan to confirm the app survives.

## 1.10.0 — 2026-11-11

- **Corsair iCUE Commander Duo** (1b1c:0c56), experimental: both ARGB ports with per-LED colors. Corsair fans
  and strips are detected by the hub; for any other ARGB strip set the port's **LED count** in the app (up to
  204) and the hub is told. The hub drops host control after 30–60 s of silence, so the colors are repeated
  every 8 s, and a dropped session is re-entered. Fans and temperature probes are not touched. Written from the
  protocol notes of OpenLinkHub and the corsair-cduo Linux driver and tested against a simulated hub only:
  **not yet confirmed on hardware**. iCUE has to be closed.
- **ASUS Aura mainboards**: the controller is found by its HID usage page instead of a fixed interface number,
  and a board that is found but skipped now says why in the log (before, it was skipped silently).
- `docs/v92-capture/`: how to record the Dark Flash V92 screen's USB traffic, with test images, as the first
  step toward supporting it.

## 1.9.2 — 2026-11-10

- **The update check runs at every start**, right after the devices are found (before: 25 seconds in, and only
  if the last check was more than 20 hours ago). While the app keeps running it still asks once a day.
  "Later" still means tomorrow and "Skip this version" still holds, so starting the app often does not nag.

## 1.9.1 — 2026-11-10

- **Check for updates…** is in the tray menu too.
- When the update window opens while RGB Deck sits in the tray, a tray note says so (the window can open
  behind others).
- The updater is now verified end to end against GitHub: a git checkout and an `install.sh` source folder of
  1.9.0 each found this release, installed it with **Update now** and came back up as 1.9.1.

## 1.9.0 — 2026-11-10

- **Updater.** Once a day (Settings → Updates, on by default) the app asks GitHub for the newest release. When
  there is one, a window shows the notes of every version since yours and offers **Update now**, **Later**
  (ask again tomorrow) and **Skip this version**. Update now replaces this installation and restarts the app:
  the Windows setup or portable exe is downloaded and verified against the SHA-256 GitHub publishes, a git
  checkout is pulled (never over local changes), an `install.sh` source folder gets its package swapped with
  rollback, a pip / pipx environment is upgraded with pip. Package-managed copies are only pointed to the
  download. Downloads are accepted from this project's repository only.
- **Help → Check for updates…** asks right away.
- The Windows paths are covered by tests with stand-in files but have not run on a real Windows machine.

## 1.8.2 — 2026-11-10

Memory: 149 MB → 102 MB resident on the author's Plasma desktop (the app's own share 53 → 35 MB).

- numpy is no longer imported for the music analysis (19 MB for a filter that takes 0.1 ms in plain Python).
- The phone page's HTTP server and WLED's URL library load when they are first used, not at start (10 MB).
- Linux: the desktop's Qt theme plugin is not loaded (about 22 MB on KDE; the tray icon and its menu come
  from Qt's built-in theme). `RGBDECK_NATIVE_THEME=1` brings it back.
- The control token comes from `os.urandom` instead of pulling in the crypto library.
- README: the resource figures are re-measured; the old claim that `--apply` runs without Qt in 20 MB was wrong.

## 1.8.1 — 2026-11-10

Found by driving the real window through every tab, chip, dialog, drag and animation while timing the UI:

- **Stopping an animation left devices dark** that had been showing an onboard effect (a mainboard on
  Breathing): they were moved to direct colors for the animation and never moved back. They get their effect
  back now.
- **Opening the mouse tab hitched** for about 50 ms, and up to a second while the device thread was busy: the
  battery was read on the UI thread, also by the 5-minute low-battery check. It is read in the background now.
- **Lights panel on small screens**: rows were squeezed until their text was cut off; the panel now grows to
  what its rows need.
- Chips of the previous device are hidden at once when switching devices instead of at the next event-loop pass.

## 1.8.0 — 2026-11-10

- **Phone page** (Settings → Phone page, off by default): a small page on port 8765 for a phone on the home
  network — lights off / on, brightness, color, presets, animations. Only private-network clients are answered
  and every request needs the secret in the link; the page can run eight lighting commands and nothing else.
  Verified here by driving the keyboard and the Govee strip through it over the LAN address.

## 1.7.0 — 2026-11-10

**Automations** (File → Automations…), all off until you switch them on:

- **Away**: lights off while the screen is locked or the PC sleeps, the look back afterwards
  (`org.freedesktop.ScreenSaver` and logind on Linux; a desktop check every 2 s on Windows, untested there).
- **Schedule**: at a time of day dim, restore brightness, apply a preset, lights off / on.
- **Notification flash** (Linux): a pulse in a color of your choice, own colors per program, a mute list.
  Windows offers ordinary programs no way to see notifications, so it is not available there.
- **Per-app looks**: a preset while a program is in front, the previous look back when it is not. The focused
  program comes from a small KWin script on KDE Plasma and from the foreground window on Windows (untested);
  other desktops can't supply it.
- Presets applied while brightness is turned down or the lights are off now respect that.
- Verified on this hardware: lock signal → keyboard and Govee strip off and back; a real notification →
  flash on the strip; KWin focus reporting.

## 1.6.0 — 2026-11-10

- **Music animations**: *Music: spectrum* (bars from bass to treble; rows on a keyboard, brightness on a strip)
  and *Music: beat pulse* (brightness follows the bass). The sound is read from the PC's output — `parec` or
  `pw-record` on Linux, the optional `soundcard` package on Windows (untested there) — never from a microphone,
  and only while one of the two runs. `RGBDECK_AUDIO_DEVICE` picks another source. Verified on this hardware
  with a test beat: keyboard spectrum, pulse on the Govee strip over Bluetooth.
- **PC status colors** (Animate → Customize… → per device): CPU temperature, GPU temperature (nvidia-smi or
  amdgpu) or CPU load as a color from blue to red, with or without an animation running on the other devices.
- **Low battery**: a wireless device below 15 % gives one red flash on everything and a tray note.
- Animation frames that did not change are no longer re-sent.

## 1.5.0 — 2026-11-10

- **Tray menu**: lights off / on, presets, animations, brightness.
- **Commands**: `rgbdeck --toggle | --off | --on | --brightness N|+N|-N | --preset NAME | --next-preset |
  --animate KIND|off | --color RRGGBB` act on the running app; they are desktop actions (KDE lists them under
  Shortcuts). Windows: optional global shortcuts Ctrl+Alt+F9…F12 (untested on real Windows).
- **Master brightness and lights off** sit on top of every look (painting, presets, animations, typing effects);
  "off" keeps the look and brings it back, devices on an onboard effect are switched off and restored.
- **Custom animations** (Animate → Customize…): exact speed, your own colors for wave / color cycle / aurora,
  and which devices take part.
- **Presets remember a running animation**, and "apply at login" plays it without the window.
- Internals: every color now passes one output stage (`director.py`), which is also the single list of actions
  for tray, command line and what comes next (automations, phone page).

## 1.4.0 — 2026-11-10

- **Govee lights over Bluetooth.** A light in Bluetooth range that the LAN API does not reach (LAN Control off,
  Bluetooth-only models) is found and driven over Bluetooth LE by itself; the LAN is used whenever it works.
  Verified on a real H618F strip: power, brightness, color, single segments, whites, scenes, and reading back
  what the light shows. The strip takes about ten commands a second and hangs up when flooded, so packets go out
  one at a time, each waiting for the light's answer; a dropped connection is re-established. This also confirms
  on hardware the segment and scene packets the LAN driver sends. Uses Qt's Bluetooth module: no new dependency.
- **Animations** (✨ Animate): rainbow wave, color cycle, breathing, aurora, fire and starlight, rendered by the
  app and played in step on every device that takes per-LED colors. Typing effects render on top; a room light
  that is off stays off; a device you put on an onboard effect is left alone; stopping gives every device its own
  colors back. The choice is remembered and resumes at the next start (the app stays in the tray meanwhile).
- **WLED controllers** (experimental, built against a simulator): found by mDNS or added by address, per-LED
  colors over the UDP realtime protocol, on/off, "WLED's own effect", look written into WLED on exit.
- Lights panel: Bluetooth switch and states, WLED rows, what works is listed first, rows no longer clipped.
- `--apply` hangs up cleanly on network lights; drivers may define `shutdown()`.

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
