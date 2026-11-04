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

## Supported natively

| Family | Notes |
|---|---|
| Logitech HID++ 2.0 RGB keyboards (G512, G513, G810, G815, G910, G915/TKL, PRO…) | keys discovered from the device; ANSI/ISO/JIS aware |
| Glorious Model O / O- / D / D- Wireless (cable or dongle) | lighting written to all 3 onboard profiles; battery readout |
| ASUS Aura USB mainboards (0b05:19af and relatives) | direct color + onboard effects |

Other devices: every driver is a small module in `rgbdeck/hw/` — see [CONTRIBUTING.md](CONTRIBUTING.md)
for how to add one. Open a device-support issue with your `lsusb` line if you'd like yours added.

## Install

```sh
git clone https://github.com/dancingduck425/rgbdeck.git ~/src/rgbdeck && cd ~/src/rgbdeck
./install.sh            # user install; asks sudo once for the udev rule
# Arch:  makepkg -si     # proper package
# any:   pipx install .  # pulls PySide6 from PyPI
```

Dependencies: Python ≥ 3.10, PySide6. Optional: `python-evdev`.

Device access: `70-rgbdeck.rules` grants the logged-in user access via `uaccess`. Replug the device
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

## Tests

`tests/` drives the real hardware offscreen (stop the GUI first):

```sh
python3 tests/functional_test.py /tmp/out
python3 tests/effect_test.py /tmp/out
```

## License

GPL-2.0-only (the device protocols were learned from GPL-2.0 driver sources, so the same license applies here).

## Protocol credits

Device protocols were learned from the driver sources of [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB)
(GPL-2.0) and [korkje/mxw](https://github.com/korkje/mxw). RGB Deck does not use or depend on either at runtime.
