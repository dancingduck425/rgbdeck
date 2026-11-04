# RGB Deck

[![CI](https://github.com/dancingduck425/rgbdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/dancingduck425/rgbdeck/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License GPL-2.0](https://img.shields.io/badge/license-GPL--2.0-green)

Standalone RGB lighting control for Linux. Talks to your devices **directly over `/dev/hidraw`** with its
own drivers — no vendor software, no background daemon, no third-party RGB software.

- **Per-key painting** on a drawn keyboard, selection tools, gradients, rainbow, dim.
- **Effects** chips (device onboard effects with speed / brightness / direction / colors).
- **Typing effects**: ripple & key glow rendered on top of your colors.
- **Sync all devices**: one click colors keyboard + mouse + motherboard.
- **Presets**, apply-at-login, tray icon, single instance.

![RGB Deck](docs/screenshot.png)

## Supported hardware

Every driver is native (`rgbdeck/hw/`). The first three were developed and verified on real hardware; the rest
were ported byte-for-byte from GPL-2.0 driver sources with packet-level unit tests, but have **not yet been run
on physical devices** — they are marked *experimental*. If you own one, please try it and open an issue either way.

| Family | Devices | Status |
|---|---|---|
| Logitech HID++ 2.0 keyboards | G512, G513, G810, G815, G910, G915/TKL, PRO… (keys discovered from the device; ANSI/ISO/JIS) | verified |
| Glorious wireless mice | Model O / O- / D / D- Wireless, cable or dongle (all 3 onboard profiles, battery) | verified |
| ASUS Aura USB mainboards | 0b05:19af and relatives (direct color + onboard effects) | verified |
| Razer Chroma | 182 keyboards, Blade laptops, mice, mousepads, keypads, headsets, accessories; paired devices through receivers | experimental |
| Logitech mice & headsets | G203/G303/G403/G502/G703/G900/G903/G Pro families, G633/G635/G733/G933/G935; Lightspeed, Powerplay and Unifying receivers | experimental |
| Corsair peripherals | K55/K65/K68/K70/K95/Strafe, M65/Scimitar/Glaive/Harpoon/Ironclaw/Nightsword/Dark Core, MM800, ST100, and the newer iCUE "V2" keyboards/mice/mousemats | experimental |
| Corsair Lighting Node / Commander | Lighting Node PRO & CORE, Commander PRO LED channels, LS100, LT100, 1000D, SPEC OMEGA (set your LED count per channel in the app) | experimental |
| SteelSeries | Rival 100–710, Rival 3/5, Sensei, Aerox 3/5/9, Apex 3/5/7/9/Pro/TKL/M750, Arctis 5/Nova 3, Siberia 350, QcK Prism | experimental |
| HyperX | Alloy Origins/60/65/Core/Elite/Elite 2/FPS, Pulsefire Surge/Core/FPS Pro/Dart/Raid/Haste, Fury Ultra & Pulsefire mats, QuadCast S/2 S, DuoCast | experimental |

Not supported on purpose: RAM and GPU lighting (driven over I²C/SMBus, where a bad write can brick a module — not
something to ship untested). Everything else: see [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a driver, or
open a device-support issue with your `lsusb` line.

## Install

```sh
git clone https://github.com/dancingduck425/rgbdeck.git ~/src/rgbdeck && cd ~/src/rgbdeck
./install.sh            # user install; asks sudo once for the udev rule
# Arch:  makepkg -si     # proper package
# any:   pipx install .  # pulls PySide6 from PyPI
```

Dependencies: Python ≥ 3.10, PySide6. Optional: `python-evdev`.

Device access: `70-rgbdeck.rules` (generated from every driver's USB ID list by `tools/gen_udev_rules.py`) grants the logged-in user access via `uaccess`. Replug the device
(or reboot) after installing the rule. Typing effects read `/dev/input/event*`, which logind already
grants to the active seat.

## Run

```sh
rgbdeck                      # GUI (second launch just raises the window)
rgbdeck --apply "My look"    # apply a preset headless (used by "apply at login")
rgbdeck --list               # list presets
rgbdeck --verbose            # log to the terminal; log file is ~/.config/rgbdeck/rgbdeck.log
```

Don't run another RGB control program (vendor software etc.) at the same time — two programs talking to
the same keyboard confuse each other.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Found devices but can't access them" | Install the udev rule once, then unplug/replug: `sudo install -m644 rgbdeck/data/70-rgbdeck.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger` |
| Device not listed | Only families in the table above are detected. Close any other program that talks to the device (vendor software, OpenRGB…) — two controllers confuse the hardware. Then **File → Rescan devices**. |
| Glorious mouse ignores colors | Its lighting is stored per onboard profile (1–3) and most tools only write profile 1. RGB Deck writes all three; if it still ignores you, press the profile button once. While on its cable the mouse still shows your colors — a solid color after charging is just the profile it is on. |
| Typing effects don't react | They read `/dev/input/event*`. logind grants that to the active session; otherwise add your user to the `input` group. The hint under the effect chips says which input devices are being listened to. |
| Keyboard flickers / lags while dragging the wheel | Frames are capped per device (Logitech ≈ 20 fps). If another program is also writing to the keyboard, close it. |
| Something else | `rgbdeck --verbose` prints to the terminal; the log is at `~/.config/rgbdeck/rgbdeck.log`. Open an issue with the log and your `lsusb` line. |

## Keyboard shortcuts

`1` / `2` / `3` whole-device / paint / select · `F` fill · `Ctrl+A` select all · `Ctrl+I` invert · `Esc` clear ·
right-click a key to pick its color · `Ctrl+Q` quit.

## Tests

`tests/` drives the real hardware offscreen (stop the GUI first):

```sh
python3 tests/functional_test.py /tmp/out
python3 tests/effect_test.py /tmp/out
```

## License

Copyright (C) 2026 dancingduck425. Released under the GPL-2.0-only (see [LICENSE](LICENSE)).

The experimental drivers are ports of OpenRGB's GPL-2.0-or-later controllers, so they remain under the
GPL; each ported module names the OpenRGB source files it came from.

## Protocol credits

Device protocols were learned from the driver sources of [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB)
(GPL-2.0-or-later) and [korkje/mxw](https://github.com/korkje/mxw) (MIT). RGB Deck does not use or depend on either at runtime.
