# Contributing

Thanks for helping make RGB Deck work on more hardware.

## Adding a device

Native drivers live in `rgbdeck/hw/`. A driver module exposes `probe() -> list[driver]`, and each driver has:

```python
driver.device            # rgbdeck.models.Device (modes, zones, led_names, colors)
driver.set_colors(colors: list[int])          # per-LED, u32 = r | g<<8 | b<<16
driver.set_mode(mode: Mode, save: bool)       # onboard effect
driver.read_colors() -> list[int]             # optional, used by the tests
driver.battery() -> (percent, on_cable)       # optional
driver.close()
```

Drivers are discovered automatically (any module under `rgbdeck/hw/` with a `probe()`). Add a module-level
`USB_IDS = [(vid, pid), ...]` and run `python3 tools/gen_udev_rules.py` to regenerate the udev rules.
Optional extras the backend understands: `keepalive()` + `keepalive_interval` (seconds) for boards that need a
heartbeat, `battery()`, `read_colors()`, and `resize_zone(zone_index, count)` for zones with `leds_min != leds_max`.
Add a hardware-free `tests/unit_<name>.py` that asserts exact packet bytes through a fake `HidDevice` — CI runs every
`tests/unit*.py`.

Good protocol sources: USB captures of the vendor software (Wireshark + USBPcap) and existing open-source
driver code for the device (GPL-2.0 sources are license-compatible with this project).

## Rules of thumb

- Hardware I/O happens on the backend worker thread — never sleep or block in UI code.
- Devices that drop pipelined writes exist (Logitech 0x8080 does): send, wait for the ACK, then send the next.
- Rate-limit anything that goes over a wireless link.
- Keep LED names in the standard style (`Key: A`, `Key: Left Shift`, …): `keyboard_layout.py` and the typing
  effects key off those names.

## Running the checks

```sh
python3 tests/unit_test.py                 # no hardware needed (this is what CI runs)
python3 tests/functional_test.py /tmp/out  # drives your real devices — stop the GUI first
python3 tests/effect_test.py /tmp/out
ruff check rgbdeck tests
```

## Reporting a device that doesn't work

Open an issue with: `lsusb` output for the device, what the device shows vs. what you set, and the last
lines of `~/.config/rgbdeck/rgbdeck.log` (run `rgbdeck --verbose` to get more detail).

## Windows

Drivers are platform-neutral: they only use `HidDevice` / `enumerate_hid` from `rgbdeck.hw.hidraw`, which on
Windows resolves to `rgbdeck.hw.hidapi_backend` (hidapi, with collections regrouped per USB interface). Keep
interface selection based on `info.interface` and descriptor sniffing (`b"\x06\x43\xff"` for usage page
0xFF43 etc.) and it will work on both. The Windows build (`RGBDeck.exe` + installer) is produced by the
`windows` CI job; `python -m rgbdeck --selftest` is what it checks.
