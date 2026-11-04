# RGB Deck

[![CI](https://github.com/dancingduck425/rgbdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/dancingduck425/rgbdeck/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License GPL-2.0](https://img.shields.io/badge/license-GPL--2.0-green)

Standalone RGB lighting control for Linux. Talks to your devices **directly over `/dev/hidraw`** — no
OpenRGB, no vendor software, no background server.

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

Anything else: enable **Settings → Use OpenRGB for other devices**. RGB Deck then starts an OpenRGB SDK
server with a private config in which the natively-driven families are disabled, so nothing is controlled
twice. (Requires the `openrgb` package.)

## Install

```sh
git clone https://github.com/dancingduck425/rgbdeck.git ~/src/rgbdeck && cd ~/src/rgbdeck
./install.sh            # user install; asks sudo once for the udev rule
# Arch:  makepkg -si     # proper package
# any:   pipx install .  # pulls PySide6 from PyPI
```

Dependencies: Python ≥ 3.10, PySide6. Optional: `python-evdev`, `openrgb`.

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

**Never run OpenRGB's own GUI/server at the same time** as RGB Deck's native drivers — two programs
talking to the same keyboard confuse each other.

## Tests

`tests/` drives the real hardware offscreen (stop the GUI first):

```sh
python3 tests/functional_test.py /tmp/out
python3 tests/effect_test.py /tmp/out
```

## License

GPL-2.0-only. Driver packet formats were ported from OpenRGB (GPL-2.0), so the same license applies here.

## Protocol credits

Packet formats were ported from [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB) (GPL-2.0) and
[korkje/mxw](https://github.com/korkje/mxw) (Glorious wireless, sniffed from Glorious CORE).
