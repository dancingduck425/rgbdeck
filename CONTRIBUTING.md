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

Register the module in `rgbdeck/backend.py` (`NATIVE_DRIVERS`), add its VID/PIDs to `rgbdeck/data/70-rgbdeck.rules`,
and add the OpenRGB detector name for that family to `NATIVE_DETECTORS` in `rgbdeck/hw/openrgb_bridge.py`
so the optional bridge never drives it too.

Good protocol sources: the OpenRGB controller for the device (GPL-2.0, same license as this project),
USB captures of the vendor software (Wireshark + USBPcap), and existing reverse-engineering projects.

## Rules of thumb

- Hardware I/O happens on the backend worker thread — never sleep or block in UI code.
- Devices that drop pipelined writes exist (Logitech 0x8080 does): send, wait for the ACK, then send the next.
- Rate-limit anything that goes over a wireless link.
- Keep LED names in the OpenRGB style (`Key: A`, `Key: Left Shift`, …): `keyboard_layout.py` and the typing
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
