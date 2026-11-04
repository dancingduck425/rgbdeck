"""ASUS Aura USB mainboard controller (0b05:19af and friends): direct LEDs + onboard effects."""
from __future__ import annotations

from typing import List

from rgbdeck.models import (Device, DeviceError, Mode, Zone, MODE_COLORS_MODE_SPECIFIC, MODE_COLORS_PER_LED, MODE_FLAG_HAS_MODE_SPECIFIC_COLOR, MODE_FLAG_HAS_PER_LED_COLOR,
                    MODE_FLAG_MANUAL_SAVE, u32_to_rgb)
from rgbdeck.hw.hidraw import HidDevice, enumerate_hid

VID = 0x0B05
PIDS = {0x19AF, 0x1939, 0x18F3, 0x1867, 0x1872, 0x1889}   # Aura USB mainboard controllers
AURA_MODE_DIRECT = 0xFF
EFFECTS = [("Off", 0), ("Static", 1), ("Breathing", 2), ("Flashing", 3), ("Spectrum Cycle", 4), ("Rainbow", 5),
           ("Spectrum Cycle Breathing", 6), ("Chase Fade", 7), ("Spectrum Cycle Chase Fade", 8), ("Chase", 9),
           ("Spectrum Cycle Chase", 10), ("Spectrum Cycle Wave", 11), ("Chase Rainbow Pulse", 12),
           ("Random Flicker", 13), ("Gentle Transition", 16), ("Wave Propagation", 17),
           ("Wave Propagation Pause", 18), ("Red Pulse", 19)]
COLOR_EFFECTS = {1, 2, 3, 7, 9}


class AuraMainboard:
    def __init__(self, path: str, name: str):
        self.hid = HidDevice(path)
        self.hid.drain()
        self.version = self._query(0x82, expect=0x02)
        table = self._query(0xB0, expect=0x30)
        if table is None:
            self.hid.close()
            raise DeviceError("Aura controller did not return its config table")
        cfg = table[4:64]
        self.num_leds = cfg[0x1B]
        self.num_headers = cfg[0x1D] if cfg[0x1D] <= self.num_leds else 0
        if self.num_leds == 0:
            self.hid.close()
            raise DeviceError("Aura controller reports no mainboard LEDs")
        self._write(bytes([0xEC, 0x52, 0x53, 0x00, 0x01]))      # SetGen1
        self.device = self._build(name, path)

    def _write(self, data: bytes):
        self.hid.write(data + bytes(65 - len(data)))

    def _query(self, cmd: int, expect: int):
        self._write(bytes([0xEC, cmd]))
        for _ in range(5):
            r = self.hid.read(65, 0.5)
            if r is None:
                return None
            if len(r) > 1 and r[1] == expect:
                return r
        return None

    def _build(self, name: str, path: str) -> Device:
        modes = [Mode(0, "Direct", AURA_MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)]
        for mname, val in EFFECTS:
            if val in COLOR_EFFECTS:
                modes.append(Mode(len(modes), mname, val, MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_MANUAL_SAVE,
                                  colors_min=1, colors_max=1, color_mode=MODE_COLORS_MODE_SPECIFIC, colors=[0x00B4FF]))
            else:
                modes.append(Mode(len(modes), mname, val, MODE_FLAG_MANUAL_SAVE))
        n = self.num_leds
        leds = [f"Aura Mainboard, LED {i + 1}" for i in range(n)]
        zones = [Zone(0, "Aura Mainboard", 1, n, n, n, None, 0)]
        ver = self.version[2:18].split(b"\0", 1)[0].decode(errors="replace") if self.version else ""
        return Device(0, "Motherboard", name, "ASUS", "ASUS Aura USB mainboard (direct hidraw)", ver, "",
                      "HID: " + path, 0, modes, zones, leds, [0] * n)

    def set_colors(self, colors: List[int]):
        colors = list(colors)[:self.num_leds]
        per = 0x14
        offset = 0
        done = 0
        cmd = 0x04                                            # direct channel for the mainboard zone
        while done < len(colors):
            chunk = colors[done:done + per]
            done += len(chunk)
            c = cmd | (0x80 if done >= len(colors) else 0)    # apply on the last packet
            pkt = bytearray([0xEC, 0x40, c, offset, len(chunk)])
            for col in chunk:
                pkt += bytes(u32_to_rgb(col))
            self._write(bytes(pkt))
            offset += len(chunk)
        self.device.colors[:len(colors)] = colors

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self._write(bytes([0xEC, 0x35, 0x00, 0x00, 0x01 if save else 0x00, mode.value]))   # effect on channel 0
        if mode.value == AURA_MODE_DIRECT:
            self.set_colors(self.device.colors)
            return
        r, g, b = u32_to_rgb(mode.colors[0]) if mode.colors else (0, 0, 0)
        n = self.num_leds
        mask = ((1 << n) - 1)
        pkt = bytearray([0xEC, 0x36, (mask >> 8) & 0xFF, mask & 0xFF, 0x01 if save else 0x00])
        pkt += bytes([r, g, b]) * n
        self._write(bytes(pkt))
        if save:
            self._write(bytes([0xEC, 0x3F, 0x55]))                                           # commit

    def close(self):
        self.hid.close()


def probe() -> List[AuraMainboard]:
    found = []
    for info in enumerate_hid(VID):
        if info.pid not in PIDS or info.interface != 2:
            continue
        try:
            found.append(AuraMainboard(info.path, info.name or "ASUS Aura Mainboard"))
        except DeviceError:
            continue
    return found
