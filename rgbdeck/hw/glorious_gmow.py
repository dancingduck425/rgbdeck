"""Glorious Model O / D Wireless (Sinowealth) mouse driver: feature reports on the vendor interface."""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from rgbdeck.models import (
    Device,
    DeviceError,
    Mode,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_NONE,
    MODE_FLAG_AUTOMATIC_SAVE,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_SPEED,
    u32_to_rgb,
)
from rgbdeck.hw.hidraw import HidDevice, enumerate_hid

VID = 0x258A
PIDS = {
    0x2011: ("Glorious Model O Wireless", True),
    0x2022: ("Glorious Model O Wireless", False),
    0x2012: ("Glorious Model D Wireless", True),
    0x2023: ("Glorious Model D Wireless", False),
}
PKT = 65

M_OFF, M_RAINBOW, M_CYCLE, M_CBREATH, M_STATIC, M_BREATH, M_TAIL, M_RAVE, M_WAVE = range(9)
SPEED1 = (0x14, 0x0B, 0x01)  # min, mid, max  (lower byte = faster)
SPEED2 = (0xC8, 0x6E, 0x0A)
BRIGHT = (0x00, 0x7F, 0xFF)


class GloriousMouse:
    def __init__(self, path: str, pid: int, name: str, cable: bool):
        self.hid = HidDevice(path)
        self.cable = cable
        self.version = self._firmware_version()
        if not self.version:
            self.hid.close()
            raise DeviceError("not the vendor interface")
        self.device = self._build(name, path)

    def _firmware_version(self) -> str:
        buf = bytearray(PKT)
        if self.cable:
            buf[3] = 0x02
        buf[4] = 0x03
        buf[6] = 0x81
        try:
            self.hid.send_feature(bytes(buf))
            time.sleep(0.05)
            r = self.hid.get_feature(0, PKT)
        except DeviceError:
            return ""
        if not any(r[7:11]):
            return ""
        return ".".join(str(x) for x in r[7:11])

    def _build(self, name: str, path: str) -> Device:
        A = MODE_FLAG_AUTOMATIC_SAVE
        modes = [
            Mode(0, "Off", M_OFF, A),
            Mode(
                1,
                "Rainbow Wave",
                M_RAINBOW,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | A,
                SPEED1[0],
                SPEED1[2],
                BRIGHT[0],
                BRIGHT[2],
                0,
                0,
                SPEED1[1],
                BRIGHT[1],
                2,
                MODE_COLORS_NONE,
                [],
            ),
            Mode(
                2,
                "Spectrum Cycle",
                M_CYCLE,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | A,
                SPEED1[0],
                SPEED1[2],
                BRIGHT[0],
                BRIGHT[2],
                0,
                0,
                SPEED1[1],
                BRIGHT[1],
                0,
                MODE_COLORS_NONE,
                [],
            ),
            Mode(
                3,
                "Custom Breathing",
                M_CBREATH,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_HAS_SPEED | A,
                SPEED1[0],
                SPEED1[2],
                BRIGHT[0],
                BRIGHT[2],
                2,
                7,
                SPEED1[1],
                BRIGHT[1],
                0,
                MODE_COLORS_MODE_SPECIFIC,
                [0x00B4FF, 0xFF00FF],
            ),
            Mode(
                4,
                "Static",
                M_STATIC,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS | A,
                0,
                0,
                BRIGHT[0],
                BRIGHT[2],
                1,
                1,
                0,
                BRIGHT[1],
                0,
                MODE_COLORS_MODE_SPECIFIC,
                [0x00B4FF],
            ),
            Mode(
                5,
                "Breathing",
                M_BREATH,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS | A,
                SPEED1[0],
                SPEED1[2],
                BRIGHT[0],
                BRIGHT[2],
                1,
                1,
                SPEED1[1],
                BRIGHT[1],
                0,
                MODE_COLORS_MODE_SPECIFIC,
                [0x00B4FF],
            ),
            Mode(
                6,
                "Tail",
                M_TAIL,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | A,
                SPEED1[0],
                SPEED1[2],
                BRIGHT[0],
                BRIGHT[2],
                0,
                0,
                SPEED1[1],
                BRIGHT[1],
                0,
                MODE_COLORS_NONE,
                [],
            ),
            Mode(
                7,
                "Rave",
                M_RAVE,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS | A,
                SPEED2[0],
                SPEED2[2],
                BRIGHT[0],
                BRIGHT[2],
                1,
                2,
                SPEED2[1],
                BRIGHT[1],
                0,
                MODE_COLORS_MODE_SPECIFIC,
                [0x00B4FF, 0xFF00FF],
            ),
            Mode(
                8,
                "Wave",
                M_WAVE,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | A,
                SPEED2[0],
                SPEED2[2],
                BRIGHT[0],
                BRIGHT[2],
                0,
                0,
                SPEED2[1],
                BRIGHT[1],
                0,
                MODE_COLORS_NONE,
                [],
            ),
        ]
        return Device(
            0,
            "Mouse",
            name,
            "Glorious",
            "Sinowealth wireless mouse (direct hidraw)" + ("" if self.cable else " via dongle"),
            self.version,
            "",
            "HID: " + path,
            4,
            modes,
            [],
            [],
            [],
        )

    def set_colors(self, colors: List[int]):
        pass  # no per-LED control on this mouse

    def battery(self) -> Optional[Tuple[int, bool]]:
        """(percent, charging) from the mouse, or None. Layout from korkje/mxw (sniffed from Glorious CORE)."""
        q = bytearray(PKT)
        q[3], q[4], q[6] = 0x02, 0x02, 0x83
        try:
            self.hid.send_feature(bytes(q))
            time.sleep(0.05)
            r = self.hid.get_feature(0, PKT)
        except DeviceError:
            return None
        if r[6] != 0x83:
            return None
        return max(1, r[8]), self.cable

    def set_mode(self, mode: Mode, save: bool = False):
        # Lighting is stored PER ONBOARD PROFILE (byte 7 = profile 1..3) and the mouse shows its
        # active profile, which we cannot read. Writing all three makes the change visible no matter
        # which one is active (other tools only wrote profile 1, which is why a mouse
        # left on profile 2 or 3 never seemed to react). The active profile is never touched.
        self.device.active_mode = mode.index
        for profile in (1, 2, 3):
            self._send_effect(mode, profile)
        wired = bytearray(PKT)
        wired[3], wired[4], wired[5], wired[6], wired[7] = 0x02, 0x02, 0x02, 0x02, 0x01
        wired[8] = mode.brightness & 0xFF
        less = bytearray(wired)
        less[7] = 0x00
        for pkt in (wired, less):
            time.sleep(0.04)
            self.hid.send_feature(bytes(pkt))

    def _send_effect(self, mode: Mode, profile: int):
        mode_buf = bytearray(PKT)
        mode_buf[3], mode_buf[5], mode_buf[7], mode_buf[8] = 0x02, 0x02, profile, 0xFF
        mode_buf[9] = mode.value
        cols = [
            tuple(max(1, c) if i == 0 else c for i, c in enumerate(u32_to_rgb(x))) if x == 0 else u32_to_rgb(x)
            for x in mode.colors
        ]
        v, speed = mode.value, mode.speed & 0xFF
        if v in (M_RAINBOW, M_TAIL, M_WAVE):
            mode_buf[4] = 0x05
            mode_buf[0x0B] = speed
        elif v == M_CYCLE:
            mode_buf[4] = 0x05
            mode_buf[0x0B] = speed
            mode_buf[0x0C] = 0xFF
        elif v in (M_CBREATH, M_RAVE):
            mode_buf[4] = len(cols) * 3 + 5
            mode_buf[0x0B] = speed
            for i, (r, g, b) in enumerate(cols):
                mode_buf[0x0C + 3 * i : 0x0C + 3 * i + 3] = (r, g, b)
        elif v == M_STATIC:  # layout as Glorious CORE sends it (mxw "Solid")
            mode_buf[4] = 0x08
            r, g, b = cols[0] if cols else (1, 0, 0)
            mode_buf[0x0C:0x0F] = (r, g, b)
        elif v == M_BREATH:
            mode_buf[4] = 0x08
            mode_buf[0x0B] = speed
            r, g, b = cols[0] if cols else (1, 0, 0)
            mode_buf[0x0C:0x0F] = (r, g, b)
        elif v == M_OFF:
            mode_buf[4] = 0x05
        time.sleep(0.04)
        self.hid.send_feature(bytes(mode_buf))

    def close(self):
        self.hid.close()


def probe() -> List[GloriousMouse]:
    found = []
    infos = [i for i in enumerate_hid(VID) if i.pid in PIDS]
    # The lighting config lives on the vendor-defined collection (usage page 0xFFFF, the tiny
    # descriptor). Other interfaces also answer the firmware query, so prefer by descriptor.
    infos.sort(key=lambda i: (b"\x06\xff\xff" not in i.descriptor, len(i.descriptor)))
    for info in infos:
        name, cable = PIDS[info.pid]
        try:
            found.append(GloriousMouse(info.path, info.pid, info.name or name, cable))
            break
        except DeviceError:
            continue
    return found
