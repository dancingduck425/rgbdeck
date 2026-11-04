# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
# Packet formats derived from OpenRGB (https://gitlab.com/CalcProgrammer1/OpenRGB),
# Copyright (C) the OpenRGB contributors, GPL-2.0-or-later.
"""
SteelSeries mice, keyboards, headsets and mousemats — direct /dev/hidraw drivers.

EXPERIMENTAL: ported from OpenRGB sources, not yet verified on hardware.

Protocols were read from OpenRGB (GPL-2.0-or-later), Controllers/SteelSeriesController/*; every packet
builder below cites the C++ file it was ported from. PIDs, USB interfaces and usage pages follow
SteelSeriesControllerDetect.cpp exactly. RGB Deck never talks to OpenRGB at runtime.

Supported (OpenRGB family -> class here):
  Mice
    Rival 100/105/106/110, Rival 300 (+editions), Rival 600, Rival 650, Rival 700/710 ....... RivalMouse
    Rival 3 (old + new firmware) ................................................................ Rival3Mouse
    Rival 3 Wireless, Aerox 3/5/9 Wireless (2.4 GHz and wired modes, special editions) ....... AeroxWirelessMouse
    Aerox 3 Wired ............................................................................... Aerox3Mouse
    Aerox 5 Wired ............................................................................... Aerox5Mouse
    Rival 5 ..................................................................................... Rival5Mouse
    Sensei TEN (+Neon Rider), Sensei 310, Rival 310 (+editions) ................................ SenseiMouse
  Keyboards
    Apex 5 / 7 / 7 TKL / 9 TKL / 9 Mini / Pro / Pro TKL / Pro TKL 2023 (wired+wireless) /
    Pro TKL Gen 3 (wired+wireless) / Apex Pro 3 ................................................ ApexKeyboard (per-key)
    Apex M750 ................................................................................... ApexMKeyboard (per-key)
    Apex 3 (10-zone) ............................................................................ ApexTZoneKeyboard
    Apex 3 TKL (8-zone) ......................................................................... Apex8ZoneKeyboard
    Apex (OG) / Apex Fnatic / Apex 350 .......................................................... OldApexKeyboard
  Headsets
    Siberia 350 ................................................................................. SiberiaHeadset
    Arctis 5 (both PIDs) ........................................................................ Arctis5Headset
    Arctis Nova 3 ............................................................................... ArctisNova3Headset
  Mousemats
    QcK Prism Cloth Medium / XL / 3XL / 4XL / 5XL and special editions .......................... QckPrismMat

Color values are RGB Deck u32: r | g << 8 | b << 16.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from rgbdeck.hw.hidraw import HidDevice, HidInfo, enumerate_hid
from rgbdeck.models import (
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_NONE,
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_HAS_SPEED,
    MODE_FLAG_MANUAL_SAVE,
    NO_LED,
    Device,
    DeviceError,
    Mode,
    Zone,
    rgb_to_u32,
    u32_to_rgb,
)

log = logging.getLogger("rgbdeck.steelseries")

VID = 0x1038
ZONE_SINGLE, ZONE_LINEAR, ZONE_MATRIX = 0, 1, 2

# steelseries_type (SteelSeriesDevices.h) — only the values the drivers branch on
RIVAL_100, RIVAL_300, RIVAL_650, RIVAL_600, RIVAL_700 = 0x00, 0x01, 0x02, 0x08, 0x0B
AEROX_3_WIRELESS, AEROX_3_WIRELESS_WIRED = 0x0E, 0x0F
AEROX_5_WIRELESS, AEROX_5_WIRELESS_WIRED = 0x10, 0x11
AEROX_5_DESTINY_WIRELESS, AEROX_5_DESTINY_WIRELESS_WIRED = 0x12, 0x13
AEROX_5_DIABLO_WIRELESS, AEROX_5_DIABLO_WIRELESS_WIRED = 0x14, 0x15
AEROX_9_WIRELESS, AEROX_9_WIRELESS_WIRED = 0x16, 0x17
AEROX_3_GEN2_WIRELESS = 0x18
RIVAL_3_WIRELESS = 0x20

# SteelSeriesMouseController.h effect values (Rival 3 / Aerox / Rival 5 family)
MOUSE_EFFECT_SPECTRUM_CYCLE = 0x00
MOUSE_EFFECT_BREATHING_MIN = 0x03  # value 0x03 - speed(0..2) -> 0x03/0x02/0x01 (min/mid/max)
MOUSE_EFFECT_DIRECT = 0x04
MOUSE_EFFECT_RAINBOW_BREATHING = 0x05
MOUSE_EFFECT_REACTIVE = 0x07
MOUSE_EFFECT_RAINBOW = 0x08
MOUSE_BRIGHTNESS_MAX = 0x64


def _pad(data: Sequence[int], size: int) -> bytes:
    buf = bytearray(size)
    buf[: len(data)] = bytes(data)
    return bytes(buf)


def _prefixed(data: Sequence[int], size: int) -> bytes:
    """OpenRGB's send_usb_msg(): prepend report id 0x00 then the payload padded to `size` bytes."""
    return bytes([0x00]) + _pad(data, size)


def _single_zones(names: Sequence[str]) -> List[Zone]:
    return [Zone(i, n, ZONE_SINGLE, 1, 1, 1, None, i) for i, n in enumerate(names)]


class _SteelSeriesDriver:
    """Common plumbing: owns the HidDevice and the Device model."""

    type_name = "Mouse"
    description = "SteelSeries device (direct hidraw)"

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        self.hid = hid
        self.pid = pid
        self.name = name
        self.path = path
        self.device: Device  # set by subclasses

    def _make_device(
        self, modes: List[Mode], zones: List[Zone], led_names: List[str], version: str = "", serial: str = ""
    ) -> Device:
        return Device(
            0,
            self.type_name,
            self.name,
            "SteelSeries",
            self.description,
            version,
            serial,
            "HID: " + self.path,
            0,
            modes,
            zones,
            list(led_names),
            [0] * len(led_names),
        )

    def _read(self, size: int = 64, timeout: float = 0.25) -> Optional[bytes]:
        try:
            return self.hid.read(size, timeout)
        except DeviceError:
            return None

    def set_colors(self, colors: List[int]):
        raise NotImplementedError

    def set_mode(self, mode: Mode, save: bool = False):
        raise NotImplementedError

    def close(self):
        self.hid.close()


# =====================================================================================================
# Mice
# =====================================================================================================


class RivalMouse(_SteelSeriesDriver):
    """Rival 100/105/106/110, 300, 600, 650, 700/710 — SteelSeriesRivalController.cpp.

    OpenRGB opens interface 0 and uses hid_write with a leading 0x00 report id (send_usb_msg), except
    the Rival 600 (raw 7-byte write, report id 0x1C) and the Rival 700 (579-byte feature report)."""

    description = "SteelSeries Rival Device"
    RIVAL_650_LEDS = [
        ("Left 1", 0x12),
        ("Left 2", 0x14),
        ("Left 3", 0x16),
        ("Right 1", 0x13),
        ("Right 2", 0x15),
        ("Right 3", 0x17),
    ]
    RIVAL_600_LEDS = [
        ("Left top", 0x02),
        ("Left mid", 0x04),
        ("Left bottom", 0x06),
        ("Right top", 0x03),
        ("Right mid", 0x05),
        ("Right bottom", 0x07),
    ]
    EFFECT_DIRECT, EFFECT_PULSATE_MIN, EFFECT_PULSATE_MID, EFFECT_PULSATE_MAX = 0x01, 0x02, 0x03, 0x04

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str, proto: int):
        super().__init__(hid, pid, name, path)
        self.proto = proto
        version = self._firmware_version()
        # RGBController_SteelSeriesRival.cpp: LED ids are the per-model zone values
        leds: List[Tuple[str, int]] = [("Logo", 0)]
        zones = [Zone(0, "Logo", ZONE_SINGLE, 1, 1, 1, None, 0)]
        if proto in (RIVAL_300, RIVAL_700):
            leds.append(("Scroll Wheel", 1))
            zones.append(Zone(1, "Scroll Wheel", ZONE_SINGLE, 1, 1, 1, None, 1))
        elif proto == RIVAL_650:
            leds = [("Logo", 0x11), ("Scroll Wheel", 0x10)] + self.RIVAL_650_LEDS
            zones = [
                Zone(0, "Logo", ZONE_SINGLE, 1, 1, 1, None, 0),
                Zone(1, "Scroll Wheel", ZONE_SINGLE, 1, 1, 1, None, 1),
                Zone(2, "Mouse", ZONE_LINEAR, 6, 6, 6, None, 2),
            ]
        elif proto == RIVAL_600:
            leds = [("Logo", 0x01), ("Scroll Wheel", 0x00)] + self.RIVAL_600_LEDS
            zones = [
                Zone(0, "Logo", ZONE_SINGLE, 1, 1, 1, None, 0),
                Zone(1, "Scroll Wheel", ZONE_SINGLE, 1, 1, 1, None, 1),
                Zone(2, "Mouse", ZONE_LINEAR, 6, 6, 6, None, 2),
            ]
        self.led_values = [v for _n, v in leds]
        modes = [
            Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_MANUAL_SAVE, color_mode=MODE_COLORS_PER_LED),
            Mode(
                1,
                "Pulsate",
                1,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_MANUAL_SAVE,
                speed_min=self.EFFECT_PULSATE_MIN,
                speed_max=self.EFFECT_PULSATE_MAX,
                speed=self.EFFECT_PULSATE_MID,
                color_mode=MODE_COLORS_PER_LED,
            ),
        ]
        self.device = self._make_device(modes, zones, [n for n, _v in leds], version)

    def _send(self, payload: Sequence[int], size: int):
        self.hid.write(_prefixed(payload, size))

    def _firmware_version(self) -> str:
        # SteelSeriesRivalController.cpp GetFirmwareVersion: only Rival 300 / 700 answer
        if self.proto not in (RIVAL_300, RIVAL_700):
            return ""
        try:
            self.hid.write(bytes([0x00, 0x10, 0x00]))
        except DeviceError:
            return ""
        r = self._read(64, 0.5)
        if not r or len(r) < 2:
            return ""
        return str(int.from_bytes(r[:2], "little"))

    def _set_light_effect(self, zone_id: int, effect: int):
        buf = bytearray(9)
        if self.proto == RIVAL_100:
            buf[0], buf[1] = 0x07, 0x00
        elif self.proto == RIVAL_300:
            buf[0], buf[1] = 0x07, zone_id + 1
        elif self.proto == RIVAL_700:
            return
        # RIVAL_650 falls through OpenRGB's switch with bytes 0/1 left at zero — reproduced as-is
        buf[2] = effect
        self._send(buf, 9)

    def _set_light_effect_all(self, effect: int):
        if self.proto == RIVAL_100:
            self._set_light_effect(0, effect)
        elif self.proto == RIVAL_300:
            self._set_light_effect(0, effect)
            self._set_light_effect(1, effect)
        elif self.proto == RIVAL_650:
            for i in range(0x10, 0x18):
                self._set_light_effect(i, effect)
        # RIVAL_600 / RIVAL_700: OpenRGB sends nothing

    def _set_color(self, zone_id: int, r: int, g: int, b: int):
        if self.proto == RIVAL_650:
            self._set_rival650_color(zone_id, r, g, b)
            return
        if self.proto == RIVAL_600:
            # SetRival600Color: raw hid_write, no 0x00 prefix -> report id 0x1C
            self.hid.write(bytes([0x1C, 0x27, 0x00, (1 << zone_id) & 0xFF, r, g, b]))
            return
        if self.proto == RIVAL_700:
            # SetRival700Color: 578-byte payload behind a 0x00 report id, sent as a feature report
            buf = bytearray(579)
            buf[0x01] = 0x05
            buf[0x03] = zone_id
            buf[0x04], buf[0x05], buf[0x06] = r, g, b
            buf[0x0C] = zone_id
            buf[0x0D] = 0x01
            self.hid.send_feature(bytes(buf))
            return
        buf = bytearray(9)
        if self.proto == RIVAL_100:
            buf[0], buf[1] = 0x05, 0x00
        elif self.proto == RIVAL_300:
            buf[0], buf[1] = 0x08, zone_id + 1
        buf[2], buf[3], buf[4] = r, g, b
        self._send(buf, 9)

    def _set_rival650_color(self, zone_id: int, r: int, g: int, b: int):
        # SteelSeriesRivalController.cpp SetRival650Color: four 60-byte payloads
        p1 = bytearray(60)
        p1[0x00], p1[0x04], p1[0x06], p1[0x07] = 0x03, 0x30, 0x10, 0x27
        p1[0x16], p1[0x1E] = 0x01, 0x04
        p1[0x1F], p1[0x20], p1[0x21], p1[0x22] = r, g, b, 0xFF
        p1[0x27], p1[0x29], p1[0x2C], p1[0x2D] = 0xFF, 0x54, 0xFF, 0x54
        p1[0x2E], p1[0x2F], p1[0x30], p1[0x31] = r, g, b, 0x56
        self._send(p1, 60)
        p2 = bytearray(60)
        p2[0x00], p2[0x02], p2[0x04] = 0x03, 0x30, 0x2C
        self._send(p2, 60)
        p3 = bytearray(60)
        p3[0x00], p3[0x02], p3[0x03], p3[0x08] = 0x05, zone_id, 0xFF, 0x5C
        self._send(p3, 60)
        p4 = bytearray(60)
        p4[0x00], p4[0x02], p4[0x04] = 0x1C, 0x55, 0x46
        self._send(p4, 60)

    def _save(self):
        # SaveMode: 0x09 followed by eight zero bytes
        self._send([0x09], 9)

    def set_colors(self, colors: List[int]):
        for value, col in zip(self.led_values, colors):
            self._set_color(value, *u32_to_rgb(col))
        self.device.colors[: len(colors)] = list(colors)[: len(self.led_values)]

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == 0:
            self._set_light_effect_all(self.EFFECT_DIRECT)
        else:
            self._set_light_effect_all(mode.speed)
        self.set_colors(self.device.colors)
        if save:
            self._save()


class _MouseControllerBase(_SteelSeriesDriver):
    """RGBController_SteelSeriesRival3.cpp mode table shared by Rival 3 / Aerox families."""

    description = "SteelSeries Mouse Device"

    def _rival3_modes(self, available: Sequence[int]) -> List[Mode]:
        table = {
            MOUSE_EFFECT_DIRECT: (
                "Direct",
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_MANUAL_SAVE,
                dict(
                    brightness_min=0,
                    brightness_max=MOUSE_BRIGHTNESS_MAX,
                    brightness=MOUSE_BRIGHTNESS_MAX,
                    color_mode=MODE_COLORS_PER_LED,
                ),
            ),
            MOUSE_EFFECT_BREATHING_MIN: (
                "Breathing",
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_SPEED | MODE_FLAG_MANUAL_SAVE,
                dict(speed_min=0, speed_max=2, speed=1, color_mode=MODE_COLORS_PER_LED),
            ),
            MOUSE_EFFECT_SPECTRUM_CYCLE: ("Spectrum Cycle", MODE_FLAG_MANUAL_SAVE, {}),
            MOUSE_EFFECT_RAINBOW_BREATHING: ("Rainbow Breathing", MODE_FLAG_MANUAL_SAVE, {}),
        }
        modes = []
        for value in available:
            if value in table:
                name, flags, extra = table[value]
                modes.append(Mode(len(modes), name, value, flags, **extra))
        return modes

    # subclasses: _set_color(zone_value, r, g, b, brightness), _set_light_effect_all(effect), _save()
    def _set_color(self, zone_id: int, r: int, g: int, b: int, brightness: int):
        raise NotImplementedError

    def _set_light_effect_all(self, effect: int):
        pass

    def _save(self):
        pass

    def set_colors(self, colors: List[int]):
        mode = self.device.current_mode
        for value, col in zip(self.led_values, colors):
            self._set_color(value, *u32_to_rgb(col), mode.brightness)
        self.device.colors[: len(colors)] = list(colors)[: len(self.led_values)]
        self._set_light_effect_all(mode.value - mode.speed)  # DeviceUpdateLEDs() ends with DeviceUpdateMode()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.color_mode == MODE_COLORS_PER_LED:
            self.set_colors(self.device.colors)
        else:
            self._set_light_effect_all(mode.value - mode.speed)
        if save:
            self._save()


class Rival3Mouse(_MouseControllerBase):
    """Rival 3 wired (0x1824 old firmware, 0x184C) — SteelSeriesRival3Controller.cpp, interface 3."""

    LEDS = [("Front", 0x01), ("Middle", 0x02), ("Rear", 0x03), ("Logo", 0x04)]
    MODES = (
        MOUSE_EFFECT_DIRECT,
        MOUSE_EFFECT_BREATHING_MIN,
        MOUSE_EFFECT_SPECTRUM_CYCLE,
        MOUSE_EFFECT_RAINBOW_BREATHING,
    )

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self.led_values = [v for _n, v in self.LEDS]
        version = self._firmware_version()
        self.device = self._make_device(
            self._rival3_modes(self.MODES),
            _single_zones([n for n, _v in self.LEDS]),
            [n for n, _v in self.LEDS],
            version,
        )

    def _firmware_version(self) -> str:
        try:
            self.hid.write(bytes([0x00, 0x10, 0x00]))
        except DeviceError:
            return ""
        r = self._read(64, 0.5)
        if not r or len(r) < 2:
            return ""
        return str(int.from_bytes(r[:2], "little"))

    def _set_color(self, zone_id: int, r: int, g: int, b: int, brightness: int):
        self.hid.write(bytes([0x00, 0x05, 0x00, zone_id, r, g, b, brightness & 0xFF]))

    def _set_light_effect_all(self, effect: int):
        self.hid.write(bytes([0x00, 0x06, 0x00, effect & 0xFF]))

    def _save(self):
        # SteelSeriesMouseController.cpp Save(): 10-byte write {0x00, 0x09, 0...}
        self.hid.write(_pad([0x00, 0x09], 10))


class Aerox3Mouse(_MouseControllerBase):
    """Aerox 3 Wired (0x1836) — SteelSeriesAerox3Controller.cpp, interface 3 / usage page 0xFFC0."""

    PKT = 65
    LEDS = [("Front", 0), ("Middle", 1), ("Rear", 2)]

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self.led_values = [v for _n, v in self.LEDS]
        self.current_brightness: Optional[int] = None
        self.hid.send_feature(_pad([0x00, 0x2D], self.PKT))  # SendInit()
        version = self._firmware_version()
        self.device = self._make_device(
            self._rival3_modes((MOUSE_EFFECT_DIRECT,)),
            _single_zones([n for n, _v in self.LEDS]),
            [n for n, _v in self.LEDS],
            version,
        )

    def _firmware_version(self) -> str:
        return _aerox_firmware_version(self, self.PKT)

    def _set_color(self, zone_id: int, r: int, g: int, b: int, brightness: int):
        buf = bytearray(self.PKT)
        buf[0x01] = 0x21
        buf[0x02] = (1 << zone_id) & 0xFF
        off = 3 + zone_id * 3
        buf[off], buf[off + 1], buf[off + 2] = r, g, b
        self.hid.write(bytes(buf))
        if brightness != self.current_brightness:
            self.hid.write(_pad([0x00, 0x23, brightness & 0xFF], self.PKT))
            self.current_brightness = brightness

    def _save(self):
        self.hid.write(_pad([0x00, 0x11, 0x00], self.PKT))


class Aerox5Mouse(_MouseControllerBase):
    """Aerox 5 Wired (0x1850) — SteelSeriesAerox5Controller.cpp, interface 3 / usage page 0xFFC0."""

    PKT = 65
    LEDS = [("Front", 0), ("Middle", 1), ("Rear", 2), ("Reactive", 3)]

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self.led_values = [v for _n, v in self.LEDS]
        self.current_brightness: Optional[int] = None
        self.hid.send_feature(_pad([0x00, 0x2D], self.PKT))  # SendInit()
        version = _aerox_firmware_version(self, self.PKT)
        self.device = self._make_device(
            self._rival3_modes((MOUSE_EFFECT_DIRECT,)),
            _single_zones([n for n, _v in self.LEDS]),
            [n for n, _v in self.LEDS],
            version,
        )

    def _set_color(self, zone_id: int, r: int, g: int, b: int, brightness: int):
        buf = bytearray(self.PKT)
        buf[0x01], buf[0x02] = 0x21, 0x01
        if zone_id == 0:
            off = 0x03
        elif zone_id == 1:
            buf[0x02], off = 0x02, 0x06
        elif zone_id == 2:
            buf[0x02], off = 0x04, 0x09
        elif zone_id == 3:
            buf[0x01], off = 0x26, 0x04  # reactive LED has its own command
        else:
            return
        buf[off], buf[off + 1], buf[off + 2] = r, g, b
        self.hid.write(bytes(buf))
        time.sleep(0.002)
        if brightness != self.current_brightness:
            # OpenRGB declares a 3-byte buffer but writes 65 bytes; we send the intended zero-padded packet
            self.hid.write(_pad([0x00, 0x23, brightness & 0xFF], self.PKT))
            time.sleep(0.002)
            self.current_brightness = brightness

    def _save(self):
        self.hid.write(_pad([0x00, 0x11, 0x00], self.PKT))


def _aerox_firmware_version(drv: _SteelSeriesDriver, pkt: int) -> str:
    """Aerox*/AeroxWireless GetFirmwareVersion(): feature 0x90, then read until a 0x90 reply (16 ASCII bytes)."""
    try:
        drv.hid.send_feature(_pad([0x00, 0x90, 0x00], pkt))
    except DeviceError:
        return ""
    for _ in range(8):
        r = drv._read(pkt, 0.25)
        if not r:
            return ""
        if r[0] == 0x90:
            return r[1:17].split(b"\0", 1)[0].decode("ascii", errors="replace")
    return ""


class AeroxWirelessMouse(_MouseControllerBase):
    """Aerox 3/5/9 Wireless (2.4 GHz and wired mode) and Rival 3 Wireless — SteelSeriesAeroxWirelessController.cpp.

    64-byte output reports; in 2.4 GHz mode the command byte is OR'ed with 0x40 and every write is followed
    by a read-back. Brightness is sent in 10 steps (brightness // 10)."""

    PKT = 64
    WIRELESS_FLAG = 0x40
    WIRELESS = {
        AEROX_3_WIRELESS,
        AEROX_3_GEN2_WIRELESS,
        AEROX_5_WIRELESS,
        AEROX_5_DESTINY_WIRELESS,
        AEROX_5_DIABLO_WIRELESS,
        AEROX_9_WIRELESS,
    }
    AEROX_3_LEDS = [("Front", 0), ("Middle", 1), ("Rear", 2)]
    AEROX_9_LEDS = [("Front", 0), ("Middle", 1), ("Rear", 2), ("Reactive", 3)]
    RIVAL_3W_LEDS = [("Scroll Wheel", 1)]

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str, proto: int):
        super().__init__(hid, pid, name, path)
        self.proto = proto
        self.current_brightness: Optional[int] = None
        if proto in (AEROX_9_WIRELESS, AEROX_9_WIRELESS_WIRED):
            leds, modes = self.AEROX_9_LEDS, (MOUSE_EFFECT_DIRECT, MOUSE_EFFECT_RAINBOW_BREATHING)
        elif proto == RIVAL_3_WIRELESS:
            leds, modes = self.RIVAL_3W_LEDS, (MOUSE_EFFECT_DIRECT,)
        else:
            leds, modes = self.AEROX_3_LEDS, (MOUSE_EFFECT_DIRECT, MOUSE_EFFECT_RAINBOW_BREATHING)
        self.led_values = [v for _n, v in leds]
        self.hid.send_feature(_pad([0x00, 0x2D], self.PKT))  # SendInit()
        version = _aerox_firmware_version(self, self.PKT)
        self.device = self._make_device(
            self._rival3_modes(modes), _single_zones([n for n, _v in leds]), [n for n, _v in leds], version
        )

    @property
    def is_wireless(self) -> bool:
        return self.proto in self.WIRELESS

    def _write_buffer(self, buf: bytearray):
        if self.is_wireless:
            buf[1] |= self.WIRELESS_FLAG
        self.hid.write(bytes(buf))
        if self.is_wireless:
            self._read(self.PKT, 0.25)  # read-back required in wireless mode

    def _set_color(self, zone_id: int, r: int, g: int, b: int, brightness: int):
        buf = bytearray(self.PKT)
        if self.proto == RIVAL_3_WIRELESS:
            buf[1], buf[2], buf[3], buf[4], buf[5] = 0x13, zone_id, r, g, b
        else:
            buf[1], buf[2] = 0x21, 0x01
            if zone_id == 3 and self.proto in (AEROX_9_WIRELESS, AEROX_9_WIRELESS_WIRED):
                buf[0x01], buf[0x03] = 0x26, 0x00
            else:
                buf[0x03] = zone_id
            buf[0x04], buf[0x05], buf[0x06] = r, g, b
        self._write_buffer(buf)
        steps = (brightness & 0xFF) // 10
        if steps != self.current_brightness:
            b2 = bytearray(self.PKT)
            b2[1], b2[2] = 0x23, steps
            self._write_buffer(b2)
            self.current_brightness = steps

    def _set_light_effect_all(self, effect: int):
        if self.proto == RIVAL_3_WIRELESS:
            return
        if effect == MOUSE_EFFECT_RAINBOW_BREATHING:
            buf = bytearray(self.PKT)
            buf[1], buf[2] = 0x22, 0xFF
            self._write_buffer(buf)

    def _save(self):
        if self.proto == RIVAL_3_WIRELESS:
            return
        buf = bytearray(self.PKT)
        buf[1], buf[2] = 0x11, 0x00
        self._write_buffer(buf)


class Rival5Mouse(_SteelSeriesDriver):
    """Rival 5 (0x183C) — SteelSeriesRival5Controller.cpp, interface 0 / usage page 0xFFC0, 64-byte feature reports."""

    description = "SteelSeries Mouse Device"
    PKT = 64
    LEDS = [
        "Scroll Wheel",
        "Body Left 1",
        "Body Right 1",
        "Body Left 2",
        "Body Right 2",
        "Body Left 3",
        "Body Right 3",
        "Body Left 4",
        "Body Right 4",
        "Logo",
    ]

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self.current_brightness = 0xFF
        modes = [
            Mode(
                0,
                "Direct",
                MOUSE_EFFECT_DIRECT,
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS,
                brightness_min=0,
                brightness_max=MOUSE_BRIGHTNESS_MAX,
                brightness=MOUSE_BRIGHTNESS_MAX,
                color_mode=MODE_COLORS_PER_LED,
            ),
            Mode(
                1,
                "Reactive",
                MOUSE_EFFECT_REACTIVE,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[rgb_to_u32(255, 255, 255)],
            ),
            Mode(
                2,
                "Rainbow",
                MOUSE_EFFECT_RAINBOW,
                MODE_FLAG_HAS_BRIGHTNESS,
                brightness_min=0,
                brightness_max=MOUSE_BRIGHTNESS_MAX,
                brightness=MOUSE_BRIGHTNESS_MAX,
            ),
        ]
        n = len(self.LEDS)
        self.device = self._make_device(modes, [Zone(0, "Whole Mouse", ZONE_LINEAR, n, n, n, None, 0)], self.LEDS)

    def _feature(self, payload: Sequence[int]):
        self.hid.send_feature(_pad(payload, self.PKT))

    def _set_brightness(self, brightness: int):
        if brightness == self.current_brightness:
            return
        self.current_brightness = brightness
        if brightness >= 88:
            v = 0x64
        elif brightness >= 63:
            v = 0x32
        elif brightness >= 38:
            v = 0x19
        elif brightness >= 13:
            v = 0x0C
        else:
            v = 0x00
        self._feature([0x00, 0x23, v])

    def _update_leds(self, colors: List[int]):
        buf = bytearray(self.PKT)
        buf[1], buf[2], buf[3] = 0x21, 0xFF, 0x03
        for i, col in enumerate(colors[:10]):
            buf[4 + i * 3 : 7 + i * 3] = bytes(u32_to_rgb(col))
        self.hid.send_feature(bytes(buf))

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[:10]
        self._set_brightness(self.device.current_mode.brightness & 0xFF)
        self._update_leds(self.device.colors)

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == MOUSE_EFFECT_REACTIVE:
            r, g, b = u32_to_rgb(mode.colors[0]) if mode.colors else (255, 255, 255)
            self._feature([0x00, 0x26, 0x01, 0x00, r, g, b])
        elif mode.value == MOUSE_EFFECT_RAINBOW:
            self._feature([0x00, 0x22, 0xFF, 0x03])
        else:
            self._feature([0x00, 0x26])  # leaving Reactive must disable it
            self.set_colors(self.device.colors)
        # Rival 5 has no save command in OpenRGB


class SenseiMouse(_SteelSeriesDriver):
    """Sensei TEN / Sensei 310 / Rival 310 — SteelSeriesSenseiController.cpp, interface 0.

    65-byte payloads behind a 0x00 report id (66-byte writes)."""

    description = "SteelSeries Sensei Device"
    MODE_DIRECT, MODE_BREATHING, MODE_RAINBOW = 0, 1, 2
    BREATHING_DUR = {2: (0x27, 0x10), 3: (0x13, 0x88), 4: (0x09, 0xC4)}  # 10 s / 5 s / 2.5 s cycle
    RAINBOW_DUR = {5: (0x4E, 0x20), 6: (0x27, 0x10), 7: (0x13, 0x88)}  # 20 s / 10 s / 5 s cycle

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        modes = [
            Mode(0, "Direct", self.MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            Mode(
                1,
                "Breathing",
                self.MODE_BREATHING,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_PER_LED_COLOR,
                speed_min=2,
                speed_max=4,
                speed=3,
                color_mode=MODE_COLORS_PER_LED,
            ),
            Mode(
                2,
                "Rainbow",
                self.MODE_RAINBOW,
                MODE_FLAG_HAS_SPEED,
                speed_min=5,
                speed_max=7,
                speed=6,
                color_mode=MODE_COLORS_NONE,
            ),
        ]
        names = ["Logo", "Scroll Wheel"]
        self.device = self._make_device(modes, _single_zones(names), names)

    def _send(self, payload: bytearray):
        self.hid.write(_prefixed(payload, 65))

    def _set_color(self, zone_id: int, r: int, g: int, b: int):
        buf = bytearray(65)
        buf[0x00], buf[0x02] = 0x5B, zone_id
        buf[0x13] = 0x01  # static color flag
        buf[0x1B] = 0x01  # number of colors
        buf[0x1C:0x1F] = bytes((r, g, b))
        buf[0x1F:0x22] = bytes((r, g, b))
        self._send(buf)

    def _set_light_effect(self, zone_id: int, mode_value: int, speed: int, r: int, g: int, b: int):
        buf = bytearray(65)
        if mode_value == self.MODE_BREATHING:
            dur1, dur2 = self.BREATHING_DUR.get(speed, (0x27, 0x10))
            buf[0x00], buf[0x02], buf[0x04], buf[0x03] = 0x5B, zone_id, dur1, dur2
            buf[0x1B] = 0x03
            buf[0x1C:0x1F] = bytes((r, g, b))
            buf[0x1F:0x22] = bytes((r, g, b))
            buf[0x26] = 0x7F
            buf[0x27:0x2A] = bytes((r, g, b))
            buf[0x2A] = 0x7F
        elif mode_value == self.MODE_RAINBOW:
            dur1, dur2 = self.RAINBOW_DUR.get(speed, (0x27, 0x10))
            buf[0x00], buf[0x02], buf[0x04], buf[0x03] = 0x5B, zone_id, dur1, dur2
            buf[0x1B] = 0x07
            buf[0x1C], buf[0x1D], buf[0x1E] = 0xFF, g, b  # OpenRGB overwrites red with 0xFF, keeps g/b
            buf[0x1F] = 0xFF
            buf[0x22], buf[0x23], buf[0x24] = 0x14, 0xFF, 0xFF
            buf[0x26], buf[0x28], buf[0x2A] = 0x2B, 0xFF, 0x2B
            buf[0x2C], buf[0x2D], buf[0x2E] = 0xFF, 0xFF, 0x28
            buf[0x31], buf[0x32], buf[0x33] = 0xFF, 0x2B, 0xFF
            buf[0x35], buf[0x36], buf[0x37] = 0xFF, 0x2B, 0xFF
            buf[0x3A] = 0x14
        self._send(buf)

    def _update_zone(self, zone: int):
        mode = self.device.current_mode
        r, g, b = u32_to_rgb(self.device.colors[zone])
        if mode.value == self.MODE_DIRECT:
            self._set_color(zone, r, g, b)
        else:
            self._set_light_effect(zone, mode.value, mode.speed, r, g, b)

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[:2]
        for zone in range(2):
            self._update_zone(zone)

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)
        if save:
            self._send(bytearray([0x59]))  # SteelSeriesSenseiController::Save (unused by OpenRGB UI)


# =====================================================================================================
# Keyboards
# =====================================================================================================

# SteelSeriesApexController.cpp `keys[]`: per-key ids (HID keyboard usages plus 0xF0 Fn / 0xFB media)
APEX_KEYS: List[int] = [
    0x04,
    0x05,
    0x06,
    0x07,
    0x08,
    0x09,
    0x0A,
    0x0B,
    0x0C,
    0x0D,
    0x0E,
    0x0F,
    0x10,
    0x11,
    0x12,
    0x13,
    0x14,
    0x15,
    0x16,
    0x17,
    0x18,
    0x19,
    0x1A,
    0x1B,
    0x1C,
    0x1D,
    0x1E,
    0x1F,
    0x20,
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x29,
    0x2A,
    0x2B,
    0x2C,
    0x2D,
    0x2E,
    0x2F,
    0x30,
    0x32,
    0x33,
    0x34,
    0x35,
    0x36,
    0x37,
    0x38,
    0x39,
    0x3A,
    0x3B,
    0x3C,
    0x3D,
    0x3E,
    0x3F,
    0x40,
    0x41,
    0x42,
    0x43,
    0x44,
    0x45,
    0x46,
    0x47,
    0x48,
    0x49,
    0x4A,
    0x4B,
    0x4C,
    0x4D,
    0x4E,
    0x4F,
    0x50,
    0x51,
    0x52,
    0x64,
    0xE0,
    0xE1,
    0xE2,
    0xE3,
    0xE4,
    0xE5,
    0xE6,
    0xE7,
    0xF0,
    0x31,
    0x87,
    0x88,
    0x89,
    0x8A,
    0x8B,
    0x53,
    0x54,
    0x55,
    0x56,
    0x57,
    0x58,
    0x59,
    0x5A,
    0x5B,
    0x5C,
    0x5D,
    0x5E,
    0x5F,
    0x60,
    0x61,
    0x62,
    0x63,
    0xFB,
]

# SteelSeriesApexRegions.h `led_names[]`, same order as APEX_KEYS. OpenRGB spellings translated to the
# RGB Deck canonical set (logitech_hidpp.USAGE_NAMES): "#" -> "# (ISO)", JIS keys get the "(JIS)" suffix.
APEX_LED_NAMES: List[str] = (
    [f"Key: {c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [f"Key: {d}" for d in "1234567890"]
    + [
        "Key: Enter",
        "Key: Escape",
        "Key: Backspace",
        "Key: Tab",
        "Key: Space",
        "Key: -",
        "Key: =",
        "Key: [",
        "Key: ]",
        "Key: # (ISO)",
        "Key: ;",
        "Key: '",
        "Key: `",
        "Key: ,",
        "Key: .",
        "Key: /",
        "Key: Caps Lock",
    ]
    + [f"Key: F{i}" for i in range(1, 13)]
    + [
        "Key: Print Screen",
        "Key: Scroll Lock",
        "Key: Pause/Break",
        "Key: Insert",
        "Key: Home",
        "Key: Page Up",
        "Key: Delete",
        "Key: End",
        "Key: Page Down",
        "Key: Right Arrow",
        "Key: Left Arrow",
        "Key: Down Arrow",
        "Key: Up Arrow",
        "Key: \\ (ISO)",
        "Key: Left Control",
        "Key: Left Shift",
        "Key: Left Alt",
        "Key: Left Windows",
        "Key: Right Control",
        "Key: Right Shift",
        "Key: Right Alt",
        "Key: Right Windows",
        "Key: Right Fn",
        "Key: \\ (ANSI)",
        "Key: Ro (JIS)",
        "Key: Katakana/Hiragana (JIS)",
        "Key: Yen (JIS)",
        "Key: Henkan (JIS)",
        "Key: Muhenkan (JIS)",
        "Key: Num Lock",
        "Key: Number Pad /",
        "Key: Number Pad *",
        "Key: Number Pad -",
        "Key: Number Pad +",
        "Key: Number Pad Enter",
    ]
    + [f"Key: Number Pad {i}" for i in range(1, 10)]
    + ["Key: Number Pad 0", "Key: Number Pad .", "Key: Media Play/Pause"]
)
assert len(APEX_KEYS) == len(APEX_LED_NAMES) == 112

NA = NO_LED
APEX_MATRIX_HEIGHT, APEX_MATRIX_WIDTH = 6, 22
# SteelSeriesApexRegions.h MATRIX_MAP_ANSI (values index APEX_LED_NAMES)
APEX_MATRIX_ANSI: List[List[int]] = [
    [37, NA, 53, 54, 55, 56, NA, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, NA, NA, NA, NA],
    [48, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 41, 42, 38, NA, 68, 69, 70, 94, 95, 96, 97],
    [39, NA, 16, 22, 4, 17, 19, 24, 20, 8, 14, 15, 43, 44, 88, 71, 72, 73, 106, 107, 108, 98],
    [52, NA, 0, 18, 3, 5, 6, 7, 9, 10, 11, 46, 47, 36, NA, NA, NA, NA, 103, 104, 105, NA],
    [80, NA, 25, 23, 2, 21, 1, 13, 12, 49, 50, 51, 84, NA, NA, NA, 77, NA, 100, 101, 102, 99],
    [79, 82, 81, NA, NA, NA, NA, 40, NA, NA, NA, 85, 86, 87, 83, 75, 76, 74, 109, NA, 110, NA],
]

# region patches: (row, column, led index or NA) — SteelSeriesApexRegions.h
APEX_JP_REGION = [
    (1, 13, 91),
    (1, 14, 38),
    (2, 14, 36),
    (3, 13, 45),
    (4, 12, 89),
    (4, 13, 84),
    (5, 3, 93),
    (5, 10, 92),
    (5, 11, 90),
    (5, 12, 85),
]
APEX_ISO_REGION = [(2, 14, 36), (3, 13, 45), (4, 1, 78)]
APEX_TKL_REGION = [
    (0, 15, NA),
    (0, 16, NA),
    (0, 17, 111),
    (1, 18, NA),
    (1, 19, NA),
    (1, 20, NA),
    (1, 21, NA),
    (2, 18, NA),
    (2, 19, NA),
    (2, 20, NA),
    (2, 21, NA),
    (3, 18, NA),
    (3, 19, NA),
    (3, 20, NA),
    (4, 18, NA),
    (4, 19, NA),
    (4, 20, NA),
    (4, 21, NA),
    (5, 18, NA),
    (5, 20, NA),
]
APEX_MINI_REGION = [
    (0, 0, NA),
    (0, 2, NA),
    (0, 3, NA),
    (0, 4, NA),
    (0, 5, NA),
    (0, 7, NA),
    (0, 8, NA),
    (0, 9, NA),
    (0, 10, NA),
    (0, 11, NA),
    (0, 12, NA),
    (0, 13, NA),
    (0, 14, NA),
    (0, 15, NA),
    (0, 16, NA),
    (0, 17, NA),
    (1, 0, 37),
    (1, 15, NA),
    (1, 16, NA),
    (1, 17, NA),
    (1, 18, NA),
    (1, 19, NA),
    (1, 20, NA),
    (1, 21, NA),
    (2, 15, NA),
    (2, 16, NA),
    (2, 17, NA),
    (2, 18, NA),
    (2, 19, NA),
    (2, 20, NA),
    (2, 21, NA),
    (3, 18, NA),
    (3, 19, NA),
    (3, 20, NA),
    (4, 16, NA),
    (4, 18, NA),
    (4, 19, NA),
    (4, 20, NA),
    (4, 21, NA),
    (5, 15, NA),
    (5, 16, NA),
    (5, 17, NA),
    (5, 18, NA),
    (5, 20, NA),
]
# keyname lookups (cosmetic relabels for regional keycaps). OpenRGB's JP/Nordic cap labels are kept
# verbatim; the keyboard geometry only needs the scancode-derived base names, so these only affect
# display. ISO Enter keeps the canonical "Enter" name.
APEX_JP_KEYNAMES = {
    42: "Key: ^",
    43: "Key: @",
    44: "Key: [",
    45: "Key: ]",
    47: "Key: :",
    48: "Key: E/J",
    84: "Key: Right Shift",
    36: "Key: Enter",
}
APEX_UK_KEYNAMES = {36: "Key: Enter"}
APEX_NOR_KEYNAMES = {
    36: "Key: Enter",
    41: "Key: + ?",
    42: "Key: ´ `",
    43: "Key: Å",
    44: "Key: ¨ ^",
    45: "Key: ' *",
    46: "Key: Ä Ø",
    47: "Key: Ö Æ",
    48: "Key: § ½",
    51: "Key: - _",
    78: "Key: < >",
}

_Patch = Tuple[list, list, dict]
# SKU (first 5 chars of the serial) -> (base patch, region patch, keyname lookup). OpenRGB lists "64856"
# twice; std::map keeps the first entry (Gen 2 / 2023 US TKL), reproduced here.
APEX_SKU_PATCHES: Dict[str, _Patch] = {
    # Apex Pro Gen 3
    "64660": ([], [], {}),
    "64740": (APEX_TKL_REGION, [], {}),
    "64742": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64743": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64745": (APEX_TKL_REGION, APEX_JP_REGION, APEX_JP_KEYNAMES),
    "64871": (APEX_TKL_REGION, [], {}),
    "64913": (APEX_MINI_REGION, [], {}),
    # Apex Pro Gen 2 / 2023
    "64856": (APEX_TKL_REGION, [], {}),
    "64865": (APEX_TKL_REGION, [], {}),
    "64820": (APEX_MINI_REGION, [], {}),
    "64842": (APEX_MINI_REGION, [], {}),
    # Apex Pro
    "64739": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64738": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64737": (APEX_TKL_REGION, APEX_JP_REGION, APEX_JP_KEYNAMES),
    "64734": (APEX_TKL_REGION, [], {}),
    "64631": ([], APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64634": ([], APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64629": ([], APEX_JP_REGION, APEX_JP_KEYNAMES),
    # Apex 9
    "64847": (APEX_TKL_REGION, [], {}),
    "64848": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64849": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64837": (APEX_MINI_REGION, [], {}),
    # Apex 7
    "64646": (APEX_TKL_REGION, [], {}),
    "64758": (APEX_TKL_REGION, [], {}),
    "64747": (APEX_TKL_REGION, [], {}),
    "64652": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64760": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64749": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64651": (APEX_TKL_REGION, APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64649": (APEX_TKL_REGION, APEX_JP_REGION, APEX_JP_KEYNAMES),
    "64756": (APEX_TKL_REGION, APEX_JP_REGION, APEX_JP_KEYNAMES),
    "64635": ([], APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64778": ([], APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64788": ([], APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64641": ([], APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64775": ([], APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64787": ([], APEX_ISO_REGION, APEX_NOR_KEYNAMES),
    "64639": ([], APEX_JP_REGION, APEX_JP_KEYNAMES),
    "64772": ([], APEX_JP_REGION, APEX_JP_KEYNAMES),
    # Apex 5
    "64534": ([], APEX_ISO_REGION, APEX_UK_KEYNAMES),
    "64537": ([], APEX_JP_REGION, APEX_JP_KEYNAMES),
    "64533": ([], APEX_ISO_REGION, APEX_NOR_KEYNAMES),
}


def apex_matrix_for_sku(sku: str) -> List[List[int]]:
    """SteelSeriesApexRegions.h SetSkuRegion(): ANSI base map with the SKU's base + region patches applied."""
    matrix = [row[:] for row in APEX_MATRIX_ANSI]
    patch = APEX_SKU_PATCHES.get(sku)
    if patch:
        for row, col, value in patch[0] + patch[1]:
            matrix[row][col] = value
    return matrix


def apex_led_names_for_sku(sku: str) -> List[str]:
    """SteelSeriesApexRegions.h SetSkuLedNames()."""
    names = list(APEX_LED_NAMES)
    patch = APEX_SKU_PATCHES.get(sku)
    if patch:
        for idx, name in patch[2].items():
            names[idx] = name
    return names


def _extract_version(s: str) -> str:
    parts = s.split(".")
    if len(parts) >= 3:
        return parts[0] + "." + parts[1] + "." + ".".join(parts[2:])
    return ""


# SteelSeriesApexBaseController.h
APEX_PACKET_IN = 64
APEX_PACKET_OUT = 65
APEX_PACKET_LENGTH = 643
APEX_9_PACKET_LENGTH = 513
APEX_PACKET_ID_FIRMWARE = 0x90
APEX_GEN1_PACKET_ID_DIRECT = 0x3A
APEX_GEN1_PACKET_ID_ONBOARD = 0x3B
APEX_GEN1_PACKET_ID_SERIAL = 0xFF
APEX_2023_PACKET_ID_DIRECT = 0x40
APEX_2023_PACKET_ID_DIRECT_WIRELESS = 0x61
APEX_2023_PACKET_ID_INIT = 0x4B
APEX_2023_PACKET_ID_ONBOARD = 0x41
APEX_2023_PACKET_ID_REGION = 0xF5
APEX_PACKET_ID_SET_BRIGHTNESS = 0x23
APEX_PACKET_ID_GET_BRIGHTNESS = 0xA3
APEX_BRIGHTNESS_MIN, APEX_BRIGHTNESS_MAX = 0, 10
APEX_MODE_DIRECT, APEX_MODE_ONBOARD = 0, 1
APEX_GEN1, APEX_GEN2, APEX_GEN3 = 0, 1, 2

PID_APEX_3, PID_APEX_3_TKL, PID_APEX_5, PID_APEX_7, PID_APEX_7_TKL = 0x161A, 0x1622, 0x161C, 0x1612, 0x1618
PID_APEX_9_TKL, PID_APEX_9_MINI, PID_APEX_PRO, PID_APEX_PRO_TKL = 0x1634, 0x1620, 0x1610, 0x1614
PID_APEX_PRO_TKL_2023, PID_APEX_PRO_TKL_2023_WL_1, PID_APEX_PRO_TKL_2023_WL_2 = 0x1628, 0x1630, 0x1632
PID_APEX_PRO_TKL_GEN3, PID_APEX_PRO_TKL_GEN3_WL_1, PID_APEX_PRO_TKL_GEN3_WL_2 = 0x1642, 0x1644, 0x1646
PID_APEX_M750, PID_APEX_OG, PID_APEX_350, PID_APEX_PRO3 = 0x0616, 0x1202, 0x1206, 0x1640

APEX_PROTOCOL_MAP = {
    PID_APEX_9_TKL: APEX_GEN2,
    PID_APEX_9_MINI: APEX_GEN2,
    PID_APEX_PRO_TKL_2023: APEX_GEN2,
    PID_APEX_PRO_TKL_2023_WL_1: APEX_GEN3,
    PID_APEX_PRO_TKL_2023_WL_2: APEX_GEN3,
    PID_APEX_PRO_TKL_GEN3: APEX_GEN3,
    PID_APEX_PRO_TKL_GEN3_WL_1: APEX_GEN3,
    PID_APEX_PRO_TKL_GEN3_WL_2: APEX_GEN3,
}
APEX_DIRECT_LENGTH_MAP = {PID_APEX_9_TKL: APEX_9_PACKET_LENGTH, PID_APEX_9_MINI: APEX_9_PACKET_LENGTH}
APEX_WIRELESS_PIDS = {
    PID_APEX_PRO_TKL_GEN3_WL_1,
    PID_APEX_PRO_TKL_GEN3_WL_2,
    PID_APEX_PRO_TKL_2023_WL_1,
    PID_APEX_PRO_TKL_2023_WL_2,
}
APEX_GEN3_REGION_SKUS = {0x1: "64740", 0x3: "64741", 0x4: "64743", 0x6: "64744", 0xA: "64742", 0xD: "64745"}


class ApexKeyboard(_SteelSeriesDriver):
    """Apex 5/7/9/Pro per-key keyboards — SteelSeriesApexController.cpp (+ SteelSeriesApexBaseController.cpp).

    Control packets are 65-byte output reports (0x00 report id). The per-key frame is ONE feature report of
    643 bytes (513 on Apex 9): 0x00, packet id, key count, then (key id, r, g, b) x 112 — no chunking."""

    type_name = "Keyboard"
    description = "SteelSeries Apex RGB Device"

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self.quirk = APEX_PROTOCOL_MAP.get(pid, APEX_GEN1)
        self.direct_len = APEX_DIRECT_LENGTH_MAP.get(pid, APEX_PACKET_LENGTH)
        self.reset_cmd = APEX_GEN1_PACKET_ID_ONBOARD
        self.active = APEX_MODE_DIRECT
        self._send_initialization()
        serial = self._get_serial()
        version = self._get_version()
        self.brightness = APEX_BRIGHTNESS_MAX
        self.brightness_supported = self._read_brightness()
        sku = serial[:5]
        onboard_flags = 0
        onboard = Mode(1, "Onboard", APEX_MODE_ONBOARD, onboard_flags, color_mode=MODE_COLORS_NONE)
        if self.brightness_supported:
            onboard.flags |= MODE_FLAG_HAS_BRIGHTNESS
            onboard.brightness_min, onboard.brightness_max, onboard.brightness = (
                APEX_BRIGHTNESS_MIN,
                APEX_BRIGHTNESS_MAX,
                self.brightness,
            )
        modes = [
            Mode(0, "Direct", APEX_MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            onboard,
        ]
        names = apex_led_names_for_sku(sku)
        n = len(names)
        zones = [Zone(0, "Keyboard", ZONE_MATRIX, n, n, n, apex_matrix_for_sku(sku), 0)]
        self.device = self._make_device(modes, zones, names, version, serial)

    # ---- control packets ---------------------------------------------------------------------
    def _out(self, payload: Sequence[int]):
        self.hid.write(_pad(payload, APEX_PACKET_OUT))

    def _query(self, payload: Sequence[int], timeout: float = 0.1) -> Optional[bytes]:
        try:
            self._out(payload)
        except DeviceError:
            return None
        return self._read(APEX_PACKET_IN, timeout)

    def _get_version(self) -> str:
        out = ""
        r = self._query([0x00, APEX_PACKET_ID_FIRMWARE])
        if r:
            s = r.replace(b"\0", b"")
            if r[0] == APEX_PACKET_ID_FIRMWARE:
                s = s[1:]
            out = "KBD: " + _extract_version(s.decode("ascii", errors="replace"))
        if self.quirk == APEX_GEN1:
            # Gen 1 boards have a second (LED) controller firmware; OpenRGB asks for it unconditionally
            r2 = self._query([0x00, APEX_PACKET_ID_FIRMWARE, 0x01])
            if r2 and out:
                out += " / LED: " + _extract_version(r2.replace(b"\0", b"").decode("ascii", errors="replace"))
        return out

    def _get_serial(self) -> str:
        if self.quirk == APEX_GEN1:
            r = self._query([0x00, APEX_GEN1_PACKET_ID_SERIAL])
            if r:
                return r[:19].replace(b"\0", b"").decode("ascii", errors="replace")
            return ""
        if self.pid == PID_APEX_PRO_TKL_GEN3:
            r = self._query([0x00, APEX_2023_PACKET_ID_REGION])
            if r and len(r) > 3 and r[0] == APEX_2023_PACKET_ID_REGION and r[2] in APEX_GEN3_REGION_SKUS:
                return APEX_GEN3_REGION_SKUS[r[2]]
        if self.pid == PID_APEX_9_TKL:
            return "64847"
        if self.pid == PID_APEX_9_MINI:
            return "64837"
        return "64865"

    def _send_initialization(self):
        if self.pid in (PID_APEX_PRO_TKL, PID_APEX_9_TKL, PID_APEX_9_MINI):
            # firmware >= 1.19.7 switched these boards to the 2023/Gen3 protocol
            ver = self._get_version()[5:]
            try:
                major, minor, patch = (int(x) for x in ver.split(".")[:3])
            except ValueError:
                major = minor = patch = -1
            if major > 1 or (major == 1 and (minor > 19 or (minor == 19 and patch >= 7))):
                self.quirk = APEX_GEN3
        if self.quirk >= APEX_GEN3:
            self.reset_cmd = APEX_2023_PACKET_ID_ONBOARD
            self.hid.send_feature(_pad([0x00, APEX_2023_PACKET_ID_INIT], APEX_PACKET_OUT))
            log.debug("[%s] using Apex 2023/Gen3 protocol", self.name)
        else:
            self.reset_cmd = APEX_GEN1_PACKET_ID_ONBOARD
            log.debug("[%s] using Apex legacy protocol", self.name)

    def _send_deinitialization(self):
        self._out([0x00, self.reset_cmd])

    def _read_brightness(self) -> bool:
        r = self._query([0x00, APEX_PACKET_ID_GET_BRIGHTNESS], 0.1)
        if not r or len(r) < 3 or r[0] != APEX_PACKET_ID_GET_BRIGHTNESS or r[1] != 0x00 or r[2] > APEX_BRIGHTNESS_MAX:
            return False
        self.brightness = r[2]
        return True

    def _set_brightness(self, value: int):
        if not self.brightness_supported:
            return
        value = min(value, APEX_BRIGHTNESS_MAX)
        self._out([0x00, APEX_PACKET_ID_SET_BRIGHTNESS, value])
        self.brightness = value

    # ---- lighting -----------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        n = min(len(APEX_KEYS), len(colors), (self.direct_len - 3) // 4)
        if self.quirk >= APEX_GEN2:
            packet_id = (
                APEX_2023_PACKET_ID_DIRECT_WIRELESS if self.pid in APEX_WIRELESS_PIDS else APEX_2023_PACKET_ID_DIRECT
            )
        else:
            packet_id = APEX_GEN1_PACKET_ID_DIRECT
        buf = bytearray(self.direct_len)
        buf[0x01] = packet_id
        buf[0x02] = n
        for i in range(n):
            r, g, b = u32_to_rgb(colors[i])
            buf[3 + i * 4 : 7 + i * 4] = bytes((APEX_KEYS[i], r, g, b))
        self.hid.send_feature(bytes(buf))
        self.device.colors[: len(colors)] = list(colors)[: len(APEX_KEYS)]

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.flags & MODE_FLAG_HAS_BRIGHTNESS:
            self._set_brightness(mode.brightness)
        if self.active != mode.value:
            if self.active == APEX_MODE_ONBOARD and self.quirk != APEX_GEN1:
                self._send_initialization()
            if mode.value == APEX_MODE_ONBOARD:
                self._send_deinitialization()
        self.active = mode.value
        if mode.value == APEX_MODE_DIRECT:
            self.set_colors(self.device.colors)
        # no flash save on these boards; onboard effects are whatever SteelSeries GG stored

    def close(self):
        try:
            self._send_deinitialization()  # destructor hands the LEDs back to the firmware
        except DeviceError:
            pass
        self.hid.close()


# SteelSeriesApexMController.cpp keys_m[]: physical slot -> APEX_LED_NAMES index (0xFF = unpopulated slot)
_M = 0xFF
APEX_M750_SLOTS: List[int] = [
    79,
    82,
    81,
    _M,
    40,
    _M,
    _M,
    _M,
    _M,
    85,
    86,
    87,
    83,
    _M,
    _M,
    75,
    76,
    74,
    _M,
    104,
    _M,
    105,
    80,
    25,
    23,
    2,
    21,
    1,
    13,
    12,
    49,
    50,
    51,
    _M,
    84,
    _M,
    _M,
    _M,
    77,
    _M,
    95,
    96,
    97,
    94,
    52,
    0,
    18,
    3,
    5,
    6,
    7,
    9,
    10,
    11,
    46,
    47,
    _M,
    36,
    _M,
    _M,
    _M,
    _M,
    98,
    99,
    100,
    _M,
    39,
    16,
    22,
    4,
    17,
    19,
    24,
    20,
    8,
    14,
    15,
    43,
    44,
    _M,
    88,
    71,
    72,
    73,
    101,
    102,
    103,
    93,
    48,
    26,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    41,
    42,
    _M,
    38,
    68,
    69,
    70,
    89,
    90,
    91,
    92,
    37,
    53,
    54,
    55,
    56,
    _M,
    57,
    58,
    59,
    60,
    _M,
    61,
    62,
    63,
    64,
    65,
    66,
    67,
    _M,
    _M,
    _M,
    _M,
]
assert len(APEX_M750_SLOTS) == 132
APEX_M_PACKET = 513


class ApexMKeyboard(_SteelSeriesDriver):
    """Apex M750 (0x0616) — SteelSeriesApexMController.cpp, interface 2, 513-byte feature reports."""

    type_name = "Keyboard"
    description = "SteelSeries Apex RGB Device"

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self._enable_led_control()
        modes = [
            Mode(0, "Direct", APEX_MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            Mode(1, "Onboard", APEX_MODE_ONBOARD, 0, color_mode=MODE_COLORS_NONE),
        ]
        names = list(APEX_LED_NAMES)  # no SKU available over hidraw -> ANSI map
        n = len(names)
        zones = [Zone(0, "Keyboard", ZONE_MATRIX, n, n, n, apex_matrix_for_sku(""), 0)]
        self.device = self._make_device(modes, zones, names)

    def _enable_led_control(self):
        self.hid.send_feature(_pad([0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x85], APEX_M_PACKET))
        self.hid.send_feature(_pad([0x00, 0x00, 0x00, 0x00, 0x03, 0x01, 0x00, 0xFF], APEX_M_PACKET))
        self.hid.send_feature(
            _pad([0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x85, 0xFF], APEX_M_PACKET)
        )  # buf[7] keeps 0xFF from packet 2

    def set_colors(self, colors: List[int]):
        buf = bytearray(APEX_M_PACKET)
        buf[0x03], buf[0x04], buf[0x05], buf[0x06], buf[0x07], buf[0x08] = 0x01, 0x8E, 0x01, 0x03, 0x06, 0x16
        for i, slot in enumerate(APEX_M750_SLOTS):
            off = 9 + i * 3
            if slot == _M or slot >= len(colors):
                buf[off : off + 3] = bytes((0xFF, 0x32, 0x00))
            else:
                buf[off : off + 3] = bytes(u32_to_rgb(colors[slot]))
        self.hid.send_feature(bytes(buf))
        self.device.colors[: len(colors)] = list(colors)[: len(APEX_LED_NAMES)]

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index  # SetMode() is a no-op on the M750
        if mode.value == APEX_MODE_DIRECT:
            self.set_colors(self.device.colors)


class ApexTZoneKeyboard(_SteelSeriesDriver):
    """Apex 3 (0x161A), 10 lighting zones — SteelSeriesApexTZoneController.cpp, interface 3, 33-byte writes."""

    type_name = "Keyboard"
    description = "SteelSeries Apex 3 Device"
    LED_COUNT, PKT, BRIGHTNESS_MAX = 10, 33, 0x64

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        modes = [
            Mode(
                0,
                "Direct",
                0,
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_MANUAL_SAVE,
                brightness_min=0,
                brightness_max=self.BRIGHTNESS_MAX,
                brightness=self.BRIGHTNESS_MAX,
                color_mode=MODE_COLORS_PER_LED,
            )
        ]
        n = self.LED_COUNT
        names = [f"LED {i}" for i in range(n)]
        self.device = self._make_device(modes, [Zone(0, "Keyboard", ZONE_LINEAR, n, n, n, None, 0)], names)

    def set_colors(self, colors: List[int]):
        brightness = self.device.current_mode.brightness & 0xFF
        self.hid.write(_pad([0x00, 0x0A, 0x00, brightness], self.PKT))
        buf = bytearray(self.PKT)
        buf[0x01] = 0x0B
        for i, col in enumerate(colors[: self.LED_COUNT]):
            buf[3 + i * 3 : 6 + i * 3] = bytes(u32_to_rgb(col))
        self.hid.write(bytes(buf))
        self.device.colors[: len(colors)] = list(colors)[: self.LED_COUNT]

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)
        if save:
            self.hid.write(_pad([0x00, 0x06, 0x00, 0x08], self.PKT))
            self.hid.write(_pad([0x00, 0x09, 0x00, 0x00], self.PKT))


class Apex8ZoneKeyboard(_SteelSeriesDriver):
    """Apex 3 TKL (0x1622), 8 lighting zones — SteelSeriesApex8ZoneController.cpp, interface 1 / usage page 0xFFC0."""

    type_name = "Keyboard"
    description = "SteelSeries Apex 3 Device"
    LED_COUNT, PKT, BRIGHTNESS_MAX = 8, 65, 0x10
    MODE_DIRECT, MODE_RAINBOW_WAVE = 0, 1

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        self.current_brightness: Optional[int] = None
        modes = [
            Mode(
                0,
                "Direct",
                self.MODE_DIRECT,
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS,
                brightness_min=0,
                brightness_max=self.BRIGHTNESS_MAX,
                brightness=self.BRIGHTNESS_MAX,
                color_mode=MODE_COLORS_PER_LED,
            ),
            Mode(
                1,
                "Rainbow Wave",
                self.MODE_RAINBOW_WAVE,
                MODE_FLAG_HAS_BRIGHTNESS,
                brightness_min=0,
                brightness_max=self.BRIGHTNESS_MAX,
                brightness=self.BRIGHTNESS_MAX,
                color_mode=MODE_COLORS_NONE,
            ),
        ]
        n = self.LED_COUNT
        names = [f"LED {i}" for i in range(n)]
        self.device = self._make_device(modes, [Zone(0, "Keyboard", ZONE_LINEAR, n, n, n, None, 0)], names)

    def _set_color(self, colors: Sequence[int], mode_value: int, brightness: int):
        buf = bytearray(self.PKT)
        buf[0x01], buf[0x02] = 0x21 + mode_value, 0xFF  # 0x21 = set LEDs, 0x22 = rainbow wave
        for i, col in enumerate(colors[: self.LED_COUNT]):
            buf[3 + i * 3 : 6 + i * 3] = bytes(u32_to_rgb(col))
        self.hid.write(bytes(buf))
        if self.current_brightness != brightness:
            self.hid.write(_pad([0x00, 0x23, brightness & 0xFF], self.PKT))
            self.current_brightness = brightness

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[: self.LED_COUNT]
        mode = self.device.current_mode
        self._set_color(self.device.colors, mode.value, mode.brightness)

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.color_mode == MODE_COLORS_PER_LED:
            self._set_color(self.device.colors, mode.value, mode.brightness)
        else:
            self._set_color([], mode.value, mode.brightness)
        # no persistent memory on this board (OpenRGB: Save() is empty)


class OldApexKeyboard(_SteelSeriesDriver):
    """Apex (OG) / Apex Fnatic (0x1202), Apex 350 (0x1206) — SteelSeriesOldApexController.cpp, interface 0.

    Five zones, one 32-byte payload behind a 0x00 report id; the fourth byte of each zone is brightness (8)."""

    type_name = "Keyboard"
    description = "SteelSeries Old Apex Device"
    ZONES = ["QWERTY", "TenKey", "FunctionKeys", "MXKeys", "Logo"]
    DIRECT = 0x08

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        modes = [Mode(0, "Direct", self.DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)]
        zones = [Zone(i, n, ZONE_LINEAR, 1, 1, 1, None, i) for i, n in enumerate(self.ZONES)]
        self.device = self._make_device(modes, zones, self.ZONES)

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[:5]
        buf = bytearray(32)
        buf[0x00], buf[0x01] = 0x07, 0x00  # all zones
        alpha = self.device.current_mode.value
        for i in range(5):
            r, g, b = u32_to_rgb(self.device.colors[i])
            buf[2 + i * 4 : 6 + i * 4] = bytes((r, g, b, alpha))
        self.hid.write(_prefixed(buf, 32))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)


# =====================================================================================================
# Headsets
# =====================================================================================================


class SiberiaHeadset(_SteelSeriesDriver):
    """Siberia 350 (0x1229) — SteelSeriesSiberiaController.cpp, interface 3, 16-byte writes with report id 0x01."""

    type_name = "Headset"
    description = "SteelSeries Siberia Device"

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        modes = [Mode(0, "Static", 0, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)]
        self.device = self._make_device(modes, [Zone(0, "Headset", ZONE_SINGLE, 1, 1, 1, None, 0)], ["Headset LED"])

    def _send(self, cmd: int, payload: Sequence[int]):
        buf = bytearray(16)
        buf[0], buf[1], buf[2], buf[3] = 0x01, 0x00, cmd, len(payload)
        buf[4 : 4 + len(payload)] = bytes(payload)
        self.hid.write(bytes(buf))

    def set_colors(self, colors: List[int]):
        if colors:
            self.device.colors[0] = colors[0]
        r, g, b = u32_to_rgb(self.device.colors[0])
        self._send(0x95, [0x80, 0xBF])
        self._send(0x80, [0x52, 0x20])
        self._send(0x83, [r, g, b])
        self._send(0x93, [0x03, 0x80])

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)


class Arctis5Headset(_SteelSeriesDriver):
    """Arctis 5 (0x1250, 0x12AA) — SteelSeriesArctis5Controller.cpp, interface 5, 37-byte writes with report id 0x06."""

    type_name = "Headset"
    description = "SteelSeries Arctis 5 Headset Device"
    REPORT_SIZE, REPORT_ID = 37, 0x06

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        modes = [Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)]
        names = ["Left", "Right"]
        self.device = self._make_device(modes, _single_zones(names), names)

    def _w(self, payload: Sequence[int]):
        self.hid.write(_pad([self.REPORT_ID] + list(payload), self.REPORT_SIZE))

    def _set_color(self, zone_id: int, color: int):
        r, g, b = u32_to_rgb(color)
        self._w([0x81, 0x43, 0x01, 0x22])
        self._w([0x81, 0x43, 0x01, 0x23])
        self._w([0x8A, 0x42, 0x00, 0x20, 0x41, 0x00, r, g, b, 0xFF, 0x32, 0xC8, 0xC8])
        self._w([0x8A, 0x42, 0x00, 0x20, 0x41, 0x08, zone_id, 0x01])
        self._w([0x8A, 0x42, 0x00, 0x20, 0x60, zone_id])
        self._w([0x8A, 0x42, 0x00, 0x20, 0x05])

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[:2]
        for zone in range(2):
            self._set_color(zone, self.device.colors[zone])

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index  # DeviceUpdateMode() is empty in OpenRGB


class ArctisNova3Headset(_SteelSeriesDriver):
    """Arctis Nova 3 (0x12EC) — SteelSeriesArctisNova3Controller.cpp, interface 4 / usage page 0xFFC0.

    Effect packets are 521-byte writes, select/apply packets 64 bytes, all with report id 0x06."""

    type_name = "Headset"
    description = "SteelSeries Arctis Nova 3 Headset Device"
    REPORT_SIZE, APPLY_SIZE, REPORT_ID = 521, 64, 0x06
    MODE_OFF, MODE_DIRECT, MODE_BREATHE, MODE_RAINBOW, MODE_ORANGE, MODE_BLUE = 0, 1, 2, 3, 4, 5
    SPEED_BYTES = {
        1: (0x30, 0x75),
        2: (0x2E, 0x68),
        3: (0x2C, 0x5B),
        4: (0x20, 0x4E),
        5: (0x1E, 0x41),
        6: (0x1C, 0x34),
        7: (0x1A, 0x27),
        8: (0x0E, 0x1A),
        9: (0x0C, 0x0D),
        10: (0x94, 0x02),
    }
    RAINBOW_STOPS = bytes(
        [
            0xFF,
            0x00,
            0x00,
            0xFF,
            0x00,
            0x00,
            0x00,
            0xFF,
            0xFF,
            0x00,
            0x2B,
            0x00,
            0xFF,
            0x00,
            0x2B,
            0x00,
            0xFF,
            0xFF,
            0x2B,
            0x00,
            0x00,
            0xFF,
            0x2B,
            0xFF,
            0x00,
            0xFF,
            0x2B,
            0xFF,
            0x00,
            0x00,
            0x26,
        ]
    )
    ORANGE_STOPS = bytes([0xFF, 0xEA, 0x00, 0xFF, 0xEA, 0x00, 0x00, 0xFF, 0x4D, 0x00, 0x7F, 0xFF, 0xEA, 0x00, 0x7F])
    BLUE_STOPS = bytes([0x8C, 0x00, 0xFF, 0x8C, 0x00, 0xFF, 0x00, 0x3B, 0xD1, 0xFF, 0x7F, 0x8C, 0x00, 0xFF, 0x7F])

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        speed = dict(speed_min=1, speed_max=10)
        modes = [
            Mode(
                0,
                "Direct",
                self.MODE_DIRECT,
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_MANUAL_SAVE,
                color_mode=MODE_COLORS_PER_LED,
            ),
            Mode(1, "Off", self.MODE_OFF, 0, color_mode=MODE_COLORS_NONE),
            Mode(
                2,
                "Breathe",
                self.MODE_BREATHE,
                MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_SPEED | MODE_FLAG_MANUAL_SAVE,
                speed=9,
                color_mode=MODE_COLORS_PER_LED,
                **speed,
            ),
            Mode(
                3,
                "Color Shift - Rainbow",
                self.MODE_RAINBOW,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_MANUAL_SAVE,
                speed=6,
                **speed,
            ),
            Mode(
                4,
                "Color Shift - Heat Orange",
                self.MODE_ORANGE,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_MANUAL_SAVE,
                speed=6,
                **speed,
            ),
            Mode(
                5,
                "Color Shift - Frost Blue",
                self.MODE_BLUE,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_MANUAL_SAVE,
                speed=6,
                **speed,
            ),
        ]
        names = ["Right", "Left"]
        self.device = self._make_device(modes, [Zone(0, "Nova 3", ZONE_LINEAR, 2, 2, 2, None, 0)], names)

    def _select_zone(self, zone_id: int, state: int):
        self.hid.write(_pad([self.REPORT_ID, 0xA5, 0x02 if zone_id == 0 else 0x01, state], self.APPLY_SIZE))

    def _apply_zone(self, zone_id: int):
        self.hid.write(_pad([self.REPORT_ID, 0x09 if zone_id == 0 else 0xA3], self.APPLY_SIZE))

    def _effect_buf(self, zone_id: int) -> bytearray:
        buf = bytearray(self.REPORT_SIZE)
        buf[0x00], buf[0x01], buf[0x02] = self.REPORT_ID, 0xAA, 0x01 if zone_id == 0 else 0x00
        buf[0x15], buf[0x17] = 0x55, 0x96
        return buf

    def _finish(self, zone_id: int, buf: bytearray):
        self.hid.write(bytes(buf))
        self._select_zone(zone_id, 0x0A)
        self._apply_zone(zone_id)

    def _update_led(self, zone_id: int):
        mode = self.device.current_mode
        speed = mode.speed
        color = self.device.colors[zone_id]
        if mode.value == self.MODE_OFF:
            self._select_zone(zone_id, 0x00)
            self._apply_zone(zone_id)
            return
        buf = self._effect_buf(zone_id)
        if mode.value == self.MODE_DIRECT:
            buf[0x0D], buf[0x0E], buf[0x1D], buf[0x25] = 0x98, 0x3A, 0x01, 0x02
            buf[0x2D:0x30] = bytes(u32_to_rgb(color))
            buf[0x30] = 0xFF
        else:
            buf[0x0D], buf[0x0E] = self.SPEED_BYTES.get(speed, (0x0C, 0x0D))
            if mode.value == self.MODE_BREATHE:
                r, g, b = u32_to_rgb(color)
                buf[0x25] = 0x03
                buf[0x29:0x2C] = bytes((r, g, b))
                buf[0x30] = 0x7F
                buf[0x31:0x34] = bytes((r, g, b))
                buf[0x34] = 0x7F
            elif mode.value == self.MODE_RAINBOW:
                buf[0x25] = 0x07
                buf[0x26 : 0x26 + len(self.RAINBOW_STOPS)] = self.RAINBOW_STOPS
            elif mode.value == self.MODE_ORANGE:
                buf[0x25] = 0x03
                buf[0x26 : 0x26 + len(self.ORANGE_STOPS)] = self.ORANGE_STOPS
            elif mode.value == self.MODE_BLUE:
                buf[0x25] = 0x03
                buf[0x26 : 0x26 + len(self.BLUE_STOPS)] = self.BLUE_STOPS
        self._finish(zone_id, buf)

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[:2]
        for zone in range(2):
            self._update_led(zone)

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)
        if save:
            self._apply_zone(0)
            self._apply_zone(1)


# =====================================================================================================
# Mousemats
# =====================================================================================================


class QckPrismMat(_SteelSeriesDriver):
    """QcK Prism Cloth (all sizes/editions) — SteelSeriesQCKMatController.cpp, interface 0.

    One 525-byte feature report carrying both LED colors, then a 65-byte 0x0D "apply" write."""

    type_name = "Mousemat"
    description = "SteelSeries QCK Mat Device"

    def __init__(self, hid: HidDevice, pid: int, name: str, path: str):
        super().__init__(hid, pid, name, path)
        modes = [Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)]
        names = ["Mat Bottom LED", "Mat Top LED"]
        self.device = self._make_device(modes, [Zone(0, "Mousemat", ZONE_SINGLE, 2, 2, 2, None, 0)], names)

    def set_colors(self, colors: List[int]):
        self.device.colors[: len(colors)] = list(colors)[:2]
        buf = bytearray(525)
        buf[0x01], buf[0x03] = 0x0E, 0x02
        buf[0x08], buf[0x09], buf[0x0A], buf[0x0E] = 0xFF, 0x32, 0xC8, 0x01
        buf[0x14], buf[0x15], buf[0x16] = 0xFF, 0x32, 0xC8
        buf[0x19], buf[0x1A], buf[0x1C] = 0x01, 0x01, 0x01
        buf[0x05:0x08] = bytes(u32_to_rgb(self.device.colors[0]))
        buf[0x11:0x14] = bytes(u32_to_rgb(self.device.colors[1]))
        self.hid.send_feature(bytes(buf))
        self.hid.write(_pad([0x00, 0x0D], 65))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)


# =====================================================================================================
# Detection table — SteelSeriesControllerDetect.cpp (name, interface, usage page, factory)
# =====================================================================================================

Factory = Callable[[HidDevice, int, str, str], _SteelSeriesDriver]


def _rival(proto: int) -> Factory:
    return lambda hid, pid, name, path: RivalMouse(hid, pid, name, path, proto)


def _aerox_wl(proto: int) -> Factory:
    return lambda hid, pid, name, path: AeroxWirelessMouse(hid, pid, name, path, proto)


VENDOR_PAGE = 0xFFC0
# pid -> (OpenRGB name, interface, usage page or None, factory)
DEVICES: Dict[int, Tuple[str, int, Optional[int], Factory]] = {
    # ---- mice
    0x1838: ("SteelSeries Aerox 3 Wireless", 3, VENDOR_PAGE, _aerox_wl(AEROX_3_WIRELESS)),
    0x183A: ("SteelSeries Aerox 3 Wireless Wired", 3, VENDOR_PAGE, _aerox_wl(AEROX_3_WIRELESS_WIRED)),
    0x1878: ("SteelSeries Aerox 3 CS2 Dragon Lore Edition Wireless", 3, VENDOR_PAGE, _aerox_wl(AEROX_3_WIRELESS)),
    0x187A: (
        "SteelSeries Aerox 3 CS2 Dragon Lore Edition Wireless Wired",
        3,
        VENDOR_PAGE,
        _aerox_wl(AEROX_3_WIRELESS_WIRED),
    ),
    0x1890: ("SteelSeries Aerox 3 Wireless Gen 2", 3, VENDOR_PAGE, _aerox_wl(AEROX_3_GEN2_WIRELESS)),
    0x1836: ("SteelSeries Aerox 3 Wired", 3, VENDOR_PAGE, Aerox3Mouse),
    0x1852: ("SteelSeries Aerox 5 Wireless", 3, VENDOR_PAGE, _aerox_wl(AEROX_5_WIRELESS)),
    0x1854: ("SteelSeries Aerox 5 Wireless Wired", 3, VENDOR_PAGE, _aerox_wl(AEROX_5_WIRELESS_WIRED)),
    0x185C: ("SteelSeries Aerox 5 Destiny 2 Edition Wireless", 3, VENDOR_PAGE, _aerox_wl(AEROX_5_DESTINY_WIRELESS)),
    0x185E: (
        "SteelSeries Aerox 5 Destiny 2 Edition Wireless Wired",
        3,
        VENDOR_PAGE,
        _aerox_wl(AEROX_5_DESTINY_WIRELESS_WIRED),
    ),
    0x1860: ("SteelSeries Aerox 5 Diablo IV Edition Wireless", 3, VENDOR_PAGE, _aerox_wl(AEROX_5_DIABLO_WIRELESS)),
    0x1862: (
        "SteelSeries Aerox 5 Diablo IV Edition Wireless Wired",
        3,
        VENDOR_PAGE,
        _aerox_wl(AEROX_5_DIABLO_WIRELESS_WIRED),
    ),
    0x1850: ("SteelSeries Aerox 5 Wired", 3, VENDOR_PAGE, Aerox5Mouse),
    0x1858: ("SteelSeries Aerox 9 Wireless", 3, VENDOR_PAGE, _aerox_wl(AEROX_9_WIRELESS)),
    0x185A: ("SteelSeries Aerox 9 Wireless Wired", 3, VENDOR_PAGE, _aerox_wl(AEROX_9_WIRELESS_WIRED)),
    0x1702: ("SteelSeries Rival 100", 0, None, _rival(RIVAL_100)),
    0x170C: ("SteelSeries Rival 100 DotA 2 Edition", 0, None, _rival(RIVAL_100)),
    0x1814: ("SteelSeries Rival 105", 0, None, _rival(RIVAL_100)),
    0x1816: ("SteelSeries Rival 106", 0, None, _rival(RIVAL_100)),
    0x1729: ("SteelSeries Rival 110", 0, None, _rival(RIVAL_100)),
    0x1710: ("SteelSeries Rival 300", 0, None, _rival(RIVAL_300)),  # also "Black Ops Edition" (same PID)
    0x1714: ("Acer Predator Gaming Mouse (Rival 300)", 0, None, _rival(RIVAL_300)),
    0x1394: ("SteelSeries Rival 300 CS:GO Fade Edition", 0, None, _rival(RIVAL_300)),
    0x1716: ("SteelSeries Rival 300 CS:GO Fade Edition (stm32)", 0, None, _rival(RIVAL_300)),
    0x171A: ("SteelSeries Rival 300 CS:GO Hyperbeast Edition", 0, None, _rival(RIVAL_300)),
    0x1392: ("SteelSeries Rival 300 Dota 2 Edition", 0, None, _rival(RIVAL_300)),
    0x1718: ("SteelSeries Rival 300 HP Omen Edition", 0, None, _rival(RIVAL_300)),
    0x1720: ("SteelSeries Rival 310", 0, None, SenseiMouse),
    0x171E: ("SteelSeries Rival 310 CS:GO Howl Edition", 0, None, SenseiMouse),
    0x1736: ("SteelSeries Rival 310 PUBG Edition", 0, None, SenseiMouse),
    0x1724: ("SteelSeries Rival 600", 0, None, _rival(RIVAL_600)),
    0x172E: ("SteelSeries Rival 600 Dota 2 Edition", 0, None, _rival(RIVAL_600)),
    0x172B: ("SteelSeries Rival 650", 0, None, _rival(RIVAL_650)),
    0x1726: ("SteelSeries Rival 650 Wireless", 0, None, _rival(RIVAL_650)),
    0x1700: ("SteelSeries Rival 700", 0, None, _rival(RIVAL_700)),
    0x1730: ("SteelSeries Rival 710", 0, None, _rival(RIVAL_700)),
    0x1824: ("SteelSeries Rival 3 (Old Firmware)", 3, None, Rival3Mouse),
    0x184C: ("SteelSeries Rival 3", 3, None, Rival3Mouse),
    0x1830: ("SteelSeries Rival 3 Wireless", 3, VENDOR_PAGE, _aerox_wl(RIVAL_3_WIRELESS)),
    0x183C: ("SteelSeries Rival 5", 0, VENDOR_PAGE, Rival5Mouse),
    0x1832: ("SteelSeries Sensei TEN", 0, None, SenseiMouse),
    0x1834: ("SteelSeries Sensei TEN CS:GO Neon Rider Edition", 0, None, SenseiMouse),
    0x1722: ("SteelSeries Sensei 310", 0, None, SenseiMouse),
    # ---- headsets
    0x1229: ("SteelSeries Siberia 350", 3, None, SiberiaHeadset),
    0x12EC: ("SteelSeries Arctis Nova 3", 4, VENDOR_PAGE, ArctisNova3Headset),
    0x1250: ("SteelSeries Arctis 5", 5, None, Arctis5Headset),
    0x12AA: ("SteelSeries Arctis 5", 5, None, Arctis5Headset),
    # ---- mousemats
    0x150A: ("SteelSeries QCK Prism Cloth Medium", 0, None, QckPrismMat),
    0x150D: ("SteelSeries QCK Prism Cloth XL", 0, None, QckPrismMat),
    0x151E: ("SteelSeries QCK Prism Cloth XL Destiny Ed.", 0, None, QckPrismMat),
    0x1520: ("SteelSeries QCK Prism Cloth XL Destiny 2 Lightfall Ed.", 0, None, QckPrismMat),
    0x1514: ("SteelSeries QCK Prism Cloth XL CS:GO Neon Rider Ed.", 0, None, QckPrismMat),
    0x151C: ("SteelSeries QCK Prism Cloth XL CS:GO Neo Noir Ed.", 0, None, QckPrismMat),
    0x1516: ("SteelSeries QCK Prism Cloth 3XL", 0, None, QckPrismMat),
    0x1518: ("SteelSeries QCK Prism Cloth 4XL", 0, None, QckPrismMat),
    0x151A: ("SteelSeries QCK Prism Cloth 5XL", 0, None, QckPrismMat),
    # ---- keyboards
    PID_APEX_3: ("SteelSeries Apex 3", 3, None, ApexTZoneKeyboard),
    PID_APEX_3_TKL: ("SteelSeries Apex 3 TKL", 1, VENDOR_PAGE, Apex8ZoneKeyboard),
    PID_APEX_5: ("SteelSeries Apex 5", 1, None, ApexKeyboard),
    PID_APEX_7: ("SteelSeries Apex 7", 1, None, ApexKeyboard),
    PID_APEX_7_TKL: ("SteelSeries Apex 7 TKL", 1, None, ApexKeyboard),
    PID_APEX_9_TKL: ("SteelSeries Apex 9 TKL", 1, None, ApexKeyboard),
    PID_APEX_9_MINI: ("SteelSeries Apex 9 Mini", 1, None, ApexKeyboard),
    PID_APEX_PRO: ("SteelSeries Apex Pro", 1, None, ApexKeyboard),
    PID_APEX_PRO_TKL: ("SteelSeries Apex Pro TKL", 1, None, ApexKeyboard),
    PID_APEX_PRO_TKL_2023: ("SteelSeries Apex Pro TKL 2023 Wired", 1, None, ApexKeyboard),
    PID_APEX_PRO_TKL_2023_WL_1: ("SteelSeries Apex Pro TKL 2023 Wireless", 3, VENDOR_PAGE, ApexKeyboard),
    PID_APEX_PRO_TKL_2023_WL_2: ("SteelSeries Apex Pro TKL 2023 Wireless", 3, VENDOR_PAGE, ApexKeyboard),
    PID_APEX_PRO_TKL_GEN3: ("SteelSeries Apex Pro TKL Gen 3 Wired", 1, None, ApexKeyboard),
    PID_APEX_PRO_TKL_GEN3_WL_1: ("SteelSeries Apex Pro TKL Gen 3 Wireless", 3, VENDOR_PAGE, ApexKeyboard),
    PID_APEX_PRO_TKL_GEN3_WL_2: ("SteelSeries Apex Pro TKL Gen 3 Wireless", 3, VENDOR_PAGE, ApexKeyboard),
    PID_APEX_M750: ("SteelSeries Apex M750", 2, None, ApexMKeyboard),
    PID_APEX_OG: ("SteelSeries Apex (OG)/Apex Fnatic", 0, None, OldApexKeyboard),
    PID_APEX_350: ("SteelSeries Apex 350", 0, None, OldApexKeyboard),
    PID_APEX_PRO3: ("SteelSeries Apex Pro 3", 1, None, ApexKeyboard),
}

USB_IDS: List[Tuple[int, int]] = [(VID, pid) for pid in sorted(DEVICES)]


def _matches(info: HidInfo, interface: int, usage_page: Optional[int]) -> bool:
    if info.interface != interface:
        return False
    if usage_page is not None and info.descriptor:
        # OpenRGB's _IPU detectors also require usage page 0xFFC0; look for the "Usage Page (2 bytes)" item
        tag = bytes([0x06, usage_page & 0xFF, usage_page >> 8])
        if tag not in info.descriptor:
            return False
    return True


def probe() -> List[_SteelSeriesDriver]:
    found: List[_SteelSeriesDriver] = []
    try:
        infos = enumerate_hid(VID)
    except Exception as e:  # noqa: BLE001 - probe must never raise
        log.info("SteelSeries enumeration failed: %s", e)
        return found
    for info in infos:
        entry = DEVICES.get(info.pid)
        if entry is None:
            continue
        name, interface, usage_page, factory = entry
        if not _matches(info, interface, usage_page):
            continue
        hid = None
        try:
            hid = HidDevice(info.path)
            found.append(factory(hid, info.pid, name, info.path))
            log.info("SteelSeries: %s on %s (interface %d)", name, info.path, info.interface)
        except Exception as e:  # noqa: BLE001
            log.info("skipping %s (%s): %s", info.path, name, e)
            if hid is not None:
                hid.close()
    return found
