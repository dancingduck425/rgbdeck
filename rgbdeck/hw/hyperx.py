"""HyperX (Kingston / HP) USB HID keyboards, mice, mousemats and microphones over /dev/hidraw.

EXPERIMENTAL: ported from OpenRGB sources, not yet verified on hardware.

Every packet in this module is a byte-for-byte port of the GPL-2.0 OpenRGB C++ controllers (local checkout
OpenRGB Controllers/...); the originating file is cited next to each packet builder. Nothing here
talks to OpenRGB at runtime.

Supported devices (VID:PID, USB interface OpenRGB opens on Linux):

Keyboards (HyperXKeyboardController/)
  Alloy Origins            0951:16E5, 03F0:0591   iface 0   feature reports, 65 B, 0x81-RGB quads, 9 packets
  Alloy Origins 60         0951:1734, 03F0:0C8E   iface 0   same family, 5 packets
  Alloy Origins 65         03F0:038F              iface 0   same family, 5 packets
  Alloy Elite 2            0951:1711, 03F0:058F   iface 0   same family, 9 packets, 18-LED strip + media keys
  Alloy Elite RGB          0951:16BE              iface 2   feature reports, 264 B, per-channel planes; onboard modes
  Alloy FPS RGB            0951:16DC              iface 2   feature reports, 264 B, per-channel planes
  Alloy Origins Core (TKL) 0951:16E6, 03F0:098F   iface 2   output reports, 0xA2 stream (ANSI / ABNT2 variants)
  Eve 1800                 03F0:08C2              iface 2   output reports 0x44, 10 zones
  Origins 2 65             03F0:0CC2              iface 3   output reports 0x44, 74 keys
Mice (HyperXMouseController/)
  Pulsefire Surge          0951:16D3, 03F0:0490   iface 1   feature 264 B, 32-LED ring + logo
  Pulsefire FPS Pro        0951:16D7              iface 1   feature 264 B, logo
  Pulsefire Core           0951:16DE, 03F0:0D8F   iface 1   feature 264 B, logo (same protocol as FPS Pro)
  Pulsefire Dart wireless  0951:16E1, 03F0:068E   iface 2   output 65 B, onboard modes + save
  Pulsefire Dart wired     0951:16E2, 03F0:088E   iface 1   output 65 B, onboard modes + save
  Pulsefire Raid           0951:16E4              iface 1   feature 264 B, scroll + logo
  Pulsefire Haste          0951:1727, 03F0:0F8F   see rules feature 65 B, logo
Mousemats (HyperXMousematController/)
  Fury Ultra               0951:1705              iface 0   feature 65 B, 15 underglow + 5 strip
  Pulsefire Mat            03F0:0F8D              iface 1   same protocol
  Pulsefire Mat RGB XL     0951:1741              iface 0   same protocol, 2 LEDs
Microphones (HyperXMicrophoneController/, HyperXMicrophoneV2Controller/)
  QuadCast S / DuoCast     0951:171F, 03F0:0F8B 068C 0294 028C 048C 0D8B 098C   iface 0   feature 65 B, 2 LEDs
  QuadCast 2 S             03F0:02B5              iface 1   output 64 B with echo replies, 108-LED matrix

Not ported: HyperX DRAM (SMBus, not HID).

Keep-alive: most of these devices fall back to their onboard effect when the host stops streaming. OpenRGB runs a
thread per device that resends the current frame whenever nothing was sent for KEEPALIVE_INTERVAL seconds
(0.05 s for most, 1.0 s for Alloy Elite 2 / Pulsefire Raid / QuadCast 2 S). Each driver exposes `keepalive()`
which resends the last frame; the backend should call it at least every `driver.keepalive_interval` seconds while
the device sits in Direct mode (RGB Deck's idle tick is 5 s, which is far slower than OpenRGB - expect the board
to flicker back to its onboard effect between ticks until the backend grows a faster timer).

QuadCast S caveat (from OpenRGB): the RGB interface has no interrupt IN endpoint, so Linux usbhid refuses to bind
and no /dev/hidraw node appears; OpenRGB falls back to hidapi-libusb. This driver can only pick it up when a hidraw
node exists (e.g. via a quirk), otherwise probe() simply does not find it.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from rgbdeck.hw.hidraw import HidDevice, HidInfo, enumerate_hid
from rgbdeck.models import (NO_LED, Device, DeviceError, Mode, MODE_COLORS_MODE_SPECIFIC, MODE_COLORS_NONE,
                            MODE_COLORS_PER_LED, MODE_FLAG_AUTOMATIC_SAVE, MODE_FLAG_HAS_BRIGHTNESS,
                            MODE_FLAG_HAS_DIRECTION_LR, MODE_FLAG_HAS_MODE_SPECIFIC_COLOR, MODE_FLAG_HAS_PER_LED_COLOR,
                            MODE_FLAG_HAS_RANDOM_COLOR, MODE_FLAG_HAS_SPEED, MODE_FLAG_MANUAL_SAVE, Zone, u32_to_rgb)

log = logging.getLogger("rgbdeck.hyperx")

VID_KINGSTON = 0x0951
VID_HP = 0x03F0

ZONE_SINGLE, ZONE_LINEAR, ZONE_MATRIX = 0, 1, 2
NA = NO_LED


# ----------------------------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------------------------
def _names(spec: str) -> List[str]:
    """'|'-separated LED names. Plain entries get OpenRGB's "Key: " prefix, "~Foo" is taken verbatim,
    an empty entry is an unused LED (OpenRGB's KEY_EN_UNUSED)."""
    out = []
    for item in spec.split("|"):
        if item == "":
            out.append("")
        elif item.startswith("~"):
            out.append(item[1:])
        else:
            out.append("Key: " + item)
    return out


def _fill_unused(names: List[str]) -> List[str]:
    return [n if n else f"Unused LED {i}" for i, n in enumerate(names)]


def _direct_mode(flags: int = 0, **kw) -> Mode:
    return Mode(0, "Direct", 0xFFFF, MODE_FLAG_HAS_PER_LED_COLOR | flags, color_mode=MODE_COLORS_PER_LED, **kw)


def _quad_packet(colors: Sequence[int]) -> bytes:
    """65-byte feature report of up to 16 "0x81 R G B" entries (report id 0).
    HyperXAlloyOriginsController.cpp SendDirectColorPacket / HyperXMousematController.cpp SendDirect."""
    buf = bytearray(65)
    for i, c in enumerate(colors[:16]):
        r, g, b = u32_to_rgb(c)
        buf[i * 4 + 1] = 0x81
        buf[i * 4 + 2] = r
        buf[i * 4 + 3] = g
        buf[i * 4 + 4] = b
    return bytes(buf)


def _has_usage_page(desc: bytes, page: int) -> bool:
    if not desc:
        return True
    if page <= 0xFF:
        return bytes([0x05, page]) in desc or bytes([0x06, page, 0x00]) in desc
    return bytes([0x06, page & 0xFF, page >> 8]) in desc


def _has_usage(desc: bytes, usage: int) -> bool:
    if not desc:
        return True
    if usage <= 0xFF:
        return bytes([0x09, usage]) in desc or bytes([0x0A, usage, 0x00]) in desc
    return bytes([0x0A, usage & 0xFF, usage >> 8]) in desc


def _hid_serial(path: str) -> str:
    """USB serial (HID_UNIQ) from sysfs; OpenRGB reads hid_get_serial_number_string."""
    try:
        uevent = open(f"/sys/class/hidraw/{os.path.basename(path)}/device/uevent").read()
    except OSError:
        return ""
    for line in uevent.splitlines():
        if line.startswith("HID_UNIQ="):
            return line.split("=", 1)[1].strip()
    return ""


def _usb_parent(path: str) -> str:
    """Identify the physical USB device behind a hidraw node so one mouse with several matching
    interfaces yields one driver."""
    try:
        real = os.path.realpath(f"/sys/class/hidraw/{os.path.basename(path)}/device")
        iface_dir = os.path.basename(os.path.dirname(real))          # e.g. 1-3:1.2
        return iface_dir.split(":", 1)[0] or path
    except OSError:
        return path


# ----------------------------------------------------------------------------------------------------------------
# base driver
# ----------------------------------------------------------------------------------------------------------------
class _HyperXDevice:
    """Owns the hidraw handle, remembers the last frame for keepalive(), builds the Device model."""

    KEEPALIVE_INTERVAL = 0.05      # seconds; OpenRGB's KeepaliveThread resend threshold for this device
    KEEPALIVE_DIRECT_ONLY = True   # OpenRGB only pings while active_mode == 0 (Direct)
    TYPE_NAME = "Keyboard"
    DESCRIPTION = "HyperX device"

    def __init__(self, hid: HidDevice, info: HidInfo, name: str):
        self.hid = hid
        self.info = info
        self.name = name or self.DESCRIPTION
        self.keepalive_interval = self.KEEPALIVE_INTERVAL
        self.last_frame: Optional[List[int]] = None
        self.last_send = 0.0
        self.device = self._build_device()

    # -- model ----------------------------------------------------------------------------------------------------
    def _build_device(self) -> Device:
        raise NotImplementedError

    def _make_device(self, modes: List[Mode], zones: List[Zone], led_names: List[str], version: str = "") -> Device:
        return Device(0, self.TYPE_NAME, self.name, "HyperX", self.DESCRIPTION + " (direct hidraw, ported from OpenRGB)",
                      version, _hid_serial(self.info.path), "HID: " + self.info.path, 0, modes, zones,
                      led_names, [0] * len(led_names))

    # -- transport hooks ------------------------------------------------------------------------------------------
    def _send_direct(self, colors: List[int]):
        raise NotImplementedError

    def _fit(self, colors: Sequence[int]) -> List[int]:
        n = len(self.device.led_names)
        out = [int(c) & 0xFFFFFF for c in colors[:n]]
        out += [0] * (n - len(out))
        return out

    # -- contract -------------------------------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        frame = self._fit(colors)
        self.device.colors = list(frame)
        self.last_frame = frame
        self._send_direct(frame)
        self.last_send = time.monotonic()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index

    def keepalive(self):
        """Resend the last frame (what OpenRGB's per-device KeepaliveThread does when idle)."""
        if self.last_frame is None:
            return
        if self.KEEPALIVE_DIRECT_ONLY and self.device.active_mode != 0:
            return
        self._send_direct(self.last_frame)
        self.last_send = time.monotonic()

    def close(self):
        self.hid.close()


# ----------------------------------------------------------------------------------------------------------------
# Alloy Origins / Origins 60 / Origins 65 / Elite 2 -- "0x81 quad" feature-report family
# ----------------------------------------------------------------------------------------------------------------
ORIGINS_LED_NAMES = _names(
    "Escape|`|Tab|Caps Lock|Left Shift|Left Control|\\ (ISO)|1|Q|A|Z|Left Windows|F1|2|W|S|X|Left Alt|F2|3|E|D|C|"
    "F3|4|R|F|V|F4|5|T|G|B|Space|F5|6|Y|H|N|F6|7|U|J|M|F7|8|I|K|,|Right Alt|F8|9|O|L|.|F9|0|P|;|/|Right Fn|"
    "F10|-|[|'|F11|=|]|#|/ (ABNT)|Menu|F12|Backspace|\\ (ANSI)|Enter|Right Shift|Right Control|Print Screen|Insert|"
    "Delete|Left Arrow|Scroll Lock|Home|End|Up Arrow|Down Arrow|Pause/Break|Page Up|Page Down|Right Arrow|Num Lock|"
    "Number Pad 7|Number Pad 4|Number Pad 1|Number Pad 0|Number Pad /|Number Pad 8|Number Pad 5|Number Pad 2|"
    "Number Pad *|Number Pad 9|Number Pad 6|Number Pad 3|Number Pad .|Number Pad -|Number Pad +|Number Pad Enter")

# RGBController_HyperXAlloyOrigins.cpp matrix_map[6][23]
ORIGINS_MATRIX = [
    [0, NA, 12, 18, 23, 28, NA, 34, 39, 44, 50, NA, 55, 61, 65, 71, 77, 81, 86, NA, NA, NA, NA],
    [1, 7, 13, 19, 24, 29, 35, 40, 45, 51, 56, NA, 62, 66, 71, 72, 78, 82, 87, 90, 95, 99, 104],
    [2, NA, 8, 14, 20, 25, NA, 30, 36, 41, 46, 52, 57, 63, 67, 73, 79, 83, 88, 91, 96, 100, 105],
    [3, NA, 9, 15, 21, 26, NA, 31, 37, 42, 47, 53, 58, 64, 68, 74, NA, NA, NA, 92, 97, 101, NA],
    [4, 6, 10, 16, 22, 27, NA, 32, NA, 38, 43, 48, 54, 59, 69, 75, NA, 84, NA, 93, 98, 102, 106],
    [5, 11, 17, NA, NA, NA, NA, 33, NA, NA, NA, NA, 49, 60, 70, 76, 80, 85, 89, 94, NA, 103, NA],
]
# HyperXAlloyOriginsController.cpp skip_idx: blank slots inserted into the color stream at these positions
ORIGINS_SKIP = (23, 29, 41, 47, 59, 70, 71, 87, 88, 93, 99, 100, 102, 108, 113, 114, 120, 123, 124)

ELITE2_LED_NAMES = _names(
    "Escape|`|Tab|Caps Lock|Left Shift|Left Control|\\ (ISO)|1|Q|A|Z|Left Windows|F1|2|W|S|X|Left Alt|F2|3|E|D|C|"
    "F3|4|R|F|V|F4|5|T|G|B|Space|F5|6|Y|H|N|F6|7|U|J|M|F7|8|I|K|,|Right Alt|F8|9|O|L|.|Menu|F9|0|P|;|/|"
    "Right Windows|F10|-|[|'|F11|=|]|#|F12|Backspace|\\ (ANSI)|Enter|Right Shift|Right Control|Print Screen|Insert|"
    "Delete|Left Arrow|Scroll Lock|Home|End|Up Arrow|Down Arrow|Pause/Break|Page Up|Page Down|Right Arrow|Num Lock|"
    "Number Pad 7|Number Pad 4|Number Pad 1|Number Pad 0|Number Pad /|Number Pad 8|Number Pad 5|Number Pad 2|"
    "Media Mute|Number Pad *|Number Pad 9|Number Pad 6|Number Pad 3|Number Pad .|Media Previous|Number Pad -|"
    "Number Pad +|Media Play/Pause|Media Next|Number Pad Enter") + [f"RGB Strip {i}" for i in range(1, 19)]

# RGBController_HyperXAlloyElite2.cpp matrix_map[8][22]
ELITE2_MATRIX = [
    [NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, NA, 104, 107, 108, 98, NA, NA],
    [110, 111, 112, 113, NA, 114, 115, 116, NA, 117, 118, 119, 120, NA, 121, 122, 123, NA, 124, 125, 126, 127],
    [0, 12, 18, 23, 28, NA, 34, 39, 44, 50, 56, 62, 66, 70, NA, 76, 80, 85, NA, NA, NA, NA],
    [1, 7, 13, 19, 24, 29, 35, 40, 45, 51, 57, 63, 67, 71, NA, 77, 81, 86, 89, 94, 99, 105],
    [2, NA, 8, 14, 20, 25, 30, 36, 41, 46, 52, 58, 64, 68, 72, 78, 82, 87, 90, 95, 100, 106],
    [3, NA, 9, 15, 21, 26, 31, 37, 42, 47, 53, 59, 65, 69, 73, NA, NA, NA, 91, 96, 101, NA],
    [4, 6, NA, 10, 16, 22, 27, 32, 38, 43, 48, 54, 60, 74, NA, NA, 83, NA, 92, 97, 102, 109],
    [5, 11, 17, NA, NA, NA, NA, 33, NA, NA, NA, 49, 61, 55, 75, 79, 84, 88, 93, NA, 103, NA],
]
# HyperXAlloyElite2Controller.cpp SKIP_INDICES
ELITE2_SKIP = (23, 29, 41, 47, 70, 71, 76, 77, 87, 88, 93, 99, 100, 102, 108, 113)

_ORIGINS60_ROWS = (
    "|Escape|1|2|3|4|5|6|7|8|9|0|-|=||Backspace|"
    "Tab|Q|W|E|R|T|Y|U|I|O|P|[|]|\\ (ANSI)|"
    "Caps Lock|A|S|D|F|G|H|J|K|L|;|'||Enter|"
    "Left Shift||Z|X|C|V|B|N|M|,|.|/||Right Shift|")
ORIGINS60_LED_NAMES = _names(_ORIGINS60_ROWS + "Left Control|Left Windows|Left Alt|~Left Space|Space|~Right Space|"
                             "Right Alt|Menu|Right Control||||Right Fn")
ORIGINS65_LED_NAMES = _names(_ORIGINS60_ROWS + "Left Control|Left Windows|Left Alt|~Left Space|Space|~Right Space|"
                             "Right Alt||||Right Fn|Home|Delete|Page Up|Page Down|Up Arrow|Left Arrow|Down Arrow|"
                             "Right Arrow")
# RGBController_HyperXAlloyOrigins60and65.cpp matrix_map_60[5][14] / matrix_map_65[5][15]
ORIGINS60_MATRIX = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15],
    [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29],
    [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, NA, 43],
    [44, NA, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, NA, 57],
    [58, 59, 60, 61, NA, NA, 62, NA, NA, 63, 64, 65, 66, 70],
]
ORIGINS65_MATRIX = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 69],
    [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 70],
    [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, NA, 43, 71],
    [44, NA, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 57, 73, 72],
    [58, 59, 60, 61, NA, NA, 62, NA, NA, 63, 64, 68, 74, 75, 76],
]


class HyperXAlloyOrigins(_HyperXDevice):
    """Alloy Origins / Origins 60 / Origins 65 / Elite 2: 65-byte feature reports on interface 0.
    HyperXAlloyOriginsController.cpp, HyperXAlloyOrigins60and65Controller.cpp, HyperXAlloyElite2Controller.cpp."""

    DESCRIPTION = "HyperX Alloy Origins Keyboard Device"
    LED_NAMES = ORIGINS_LED_NAMES
    MATRIX = ORIGINS_MATRIX
    SKIP: Tuple[int, ...] = ORIGINS_SKIP
    INIT_BYTE9 = 0x09
    PACKETS = 9
    PAD_WITH_QUAD = True        # Origins pads with "0x81 00 00 00"; Elite 2 pads with an all-zero report

    def _build_device(self) -> Device:
        names = _fill_unused(self.LED_NAMES)
        zone = Zone(0, "Keyboard", ZONE_MATRIX, len(names), len(names), len(names), [list(r) for r in self.MATRIX])
        return self._make_device([_direct_mode()], [zone], names)

    def _send_direct(self, colors: List[int]):
        slots = list(colors)
        for s in self.SKIP:                         # colors.insert(begin + skip_idx, 0) in OpenRGB
            slots.insert(s, 0)
        init = bytearray(65)                        # SendDirectInitialization: 00 04 F2 .. [9]=09/05/00
        init[1] = 0x04
        init[2] = 0xF2
        init[9] = self.INIT_BYTE9
        self.hid.send_feature(bytes(init))
        for p in range(self.PACKETS):
            chunk = slots[p * 16:p * 16 + 16]
            if chunk:
                self.hid.send_feature(_quad_packet(chunk))
            elif self.PAD_WITH_QUAD:
                self.hid.send_feature(_quad_packet([0]))
            else:
                self.hid.send_feature(bytes(65))


class HyperXAlloyOrigins60(HyperXAlloyOrigins):
    DESCRIPTION = "HyperX Alloy Origins 60 Keyboard Device"
    LED_NAMES = ORIGINS60_LED_NAMES
    MATRIX = ORIGINS60_MATRIX
    SKIP = ()
    INIT_BYTE9 = 0x05
    PACKETS = 5


class HyperXAlloyOrigins65(HyperXAlloyOrigins60):
    DESCRIPTION = "HyperX Alloy Origins 65 Keyboard Device"
    LED_NAMES = ORIGINS65_LED_NAMES
    MATRIX = ORIGINS65_MATRIX


class HyperXAlloyElite2(HyperXAlloyOrigins):
    """RGBController_HyperXAlloyElite2.cpp: keepalive threshold 1000 ms (50 ms poll)."""
    DESCRIPTION = "HyperX Alloy Elite 2 Keyboard Device"
    KEEPALIVE_INTERVAL = 1.0
    LED_NAMES = ELITE2_LED_NAMES
    MATRIX = ELITE2_MATRIX
    SKIP = ELITE2_SKIP
    INIT_BYTE9 = 0x00
    PACKETS = 9
    PAD_WITH_QUAD = False


# ----------------------------------------------------------------------------------------------------------------
# Alloy Elite RGB / Alloy FPS RGB -- 264-byte feature reports, one plane per color channel
# ----------------------------------------------------------------------------------------------------------------
# HyperXAlloyEliteController.cpp / HyperXAlloyFPSController.cpp `keys[]`: byte offset of each of the 106 keys
ELITE_KEY_OFFSETS = (
    0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14,
    0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x20, 0x21, 0x22,
    0x23, 0x24, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30,
    0x31, 0x32, 0x33, 0x34, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3E, 0x3F, 0x41,
    0x44, 0x45, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x51, 0x54, 0x55,
    0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5E, 0x5F, 0x61, 0x64, 0x65, 0x68, 0x69, 0x6A,
    0x6B, 0x6C, 0x6E, 0x6F, 0x74, 0x75, 0x78, 0x79, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E,
    0x7F, 0x81, 0x84, 0x85, 0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x91,
    0x94, 0x95)
# HyperXAlloyEliteController.cpp extended_red/grn/blu: offsets of the 18 strip LEDs + 4 media keys
ELITE_EXT_RED = (0x08, 0x48, 0x88, 0x09, 0x89, 0x0A, 0x8A, 0x0B, 0x8B, 0x0C, 0x8C, 0x0D, 0x8D, 0x0E, 0x8F, 0x8E,
                 0x0F, 0x4F, 0x92, 0x13, 0x93, 0x12)
ELITE_EXT_GRN = (0x29, 0x28, 0x78, 0x19, 0x79, 0x1A, 0x7A, 0x1B, 0x7B, 0x1C, 0x7C, 0x1D, 0x7D, 0x1E, 0x6E, 0x7E,
                 0x1F, 0x6F, 0x82, 0x23, 0x83, 0x22)
ELITE_EXT_BLU = (0x39, 0x38, 0x68, 0x3A, 0x69, 0x2A, 0x6A, 0x2B, 0x6B, 0x2C, 0x6C, 0x2D, 0x6D, 0x2E, 0x5E, 0x5D,
                 0x2F, 0x5F, 0x72, 0x33, 0x73, 0x32)

ELITE_KEY_NAMES = _names(
    "Escape|`|Tab|Caps Lock|Left Shift|Left Control|F12|=|F9|9|O|L|,|Menu|Enter (ISO)|Left Arrow|F1|1|Q|A|\\ (ISO)|"
    "Left Windows|Print Screen|F10|0|P|;|.|Enter|Down Arrow|F2|2|W|S|Z|Left Alt|Scroll Lock|Backspace|F11|-|[|'|/|"
    "Right Arrow|F3|3|E|D|X|Pause/Break|Delete|Number Pad 7|Num Lock|Number Pad 6|F4|4|R|F|C|Space|Insert|End|"
    "Number Pad 8|Number Pad /|Number Pad 1|F5|5|T|G|V|Home|Page Down|Number Pad 9|Number Pad *|Number Pad 2|F6|6|Y|"
    "H|B|Page Up|Right Shift|Number Pad -|Number Pad 3|F7|7|U|J|N|Right Alt|]|Right Control|Number Pad 4|"
    "Number Pad +|Number Pad 0|F8|8|I|K|M|Right Windows|\\ (ANSI)|Up Arrow|Number Pad 5|Number Pad Enter|"
    "Number Pad .")
# RGBController_HyperXAlloyElite.cpp / RGBController_HyperXAlloyFPS.cpp matrix_map[6][23] (identical)
ELITE_MATRIX = [
    [0, NA, 16, 30, 44, 54, NA, 65, 75, 84, 95, NA, 8, 23, 38, 6, 22, 36, 49, NA, NA, NA, NA],
    [1, 17, 31, 45, 55, 66, 76, 85, 96, 9, 24, NA, 39, 7, 37, NA, 60, 70, 80, 52, 63, 73, 82],
    [2, NA, 18, 32, 46, 56, NA, 67, 77, 86, 97, 10, 25, 40, 90, 101, 50, 61, 71, 51, 62, 72, 93],
    [3, NA, 19, 33, 47, 57, NA, 68, 78, 87, 98, 11, 26, 41, 28, 14, NA, NA, NA, 92, 103, 53, NA],
    [4, 20, 34, 48, 58, 69, NA, 79, NA, 88, 99, 12, 27, 42, 81, NA, NA, 102, NA, 64, 74, 83, 104],
    [5, 21, 35, NA, NA, NA, NA, 59, NA, NA, NA, NA, 89, 100, 13, 91, 15, 29, 43, 94, NA, 105, NA],
]

ELITE_PKT_SET_EFFECT, ELITE_PKT_SET_COLOR, ELITE_PKT_DIRECT = 0x02, 0x06, 0x16
ELITE_CH_RED, ELITE_CH_GREEN, ELITE_CH_BLUE, ELITE_CH_EXT = 0x01, 0x02, 0x03, 0x04
ELITE_MODE_WAVE, ELITE_MODE_STATIC, ELITE_MODE_BREATHING = 0x00, 0x01, 0x02
ELITE_REACTIVE_NONE = 0xFF
ELITE_COLOR_SINGLE, ELITE_COLOR_DUAL, ELITE_COLOR_SPECTRUM = 0x00, 0x01, 0x02


class HyperXAlloyFPS(_HyperXDevice):
    """Alloy FPS RGB (0951:16DC, interface 2, usage page 0xFF01). HyperXAlloyFPSController.cpp."""
    DESCRIPTION = "HyperX Alloy FPS Device"
    PLANE_DELAY = 0.010         # std::this_thread::sleep_for(10ms) between channel planes

    def _build_device(self) -> Device:
        n = len(ELITE_KEY_NAMES)
        zone = Zone(0, "Keyboard", ZONE_MATRIX, n, n, n, [list(r) for r in ELITE_MATRIX])
        return self._make_device([_direct_mode()], [zone], list(ELITE_KEY_NAMES))

    @staticmethod
    def _plane(pkt_id: int, b2: int, b3: int, values: Sequence[int]) -> bytes:
        """264-byte report: 07 <pkt> <b2> <b3>, one byte per key at ELITE_KEY_OFFSETS."""
        buf = bytearray(264)
        buf[0] = 0x07
        buf[1] = pkt_id
        buf[2] = b2
        buf[3] = b3
        for off, v in zip(ELITE_KEY_OFFSETS, values):
            buf[off] = v
        return bytes(buf)

    def _send_direct(self, colors: List[int]):
        rgb = [u32_to_rgb(c) for c in colors[:106]]
        for ch, idx in ((ELITE_CH_RED, 0), (ELITE_CH_GREEN, 1), (ELITE_CH_BLUE, 2)):
            if ch != ELITE_CH_RED:
                time.sleep(self.PLANE_DELAY)
            self.hid.send_feature(self._plane(ELITE_PKT_DIRECT, ch, 0xA0, [t[idx] for t in rgb]))


class HyperXAlloyElite(HyperXAlloyFPS):
    """Alloy Elite RGB (0951:16BE, interface 2, usage page 0xFF01). HyperXAlloyEliteController.cpp.
    Direct mode streams 4 planes (R, G, B, extended); onboard Static/Wave/Breathing are written to profile 1."""
    DESCRIPTION = "HyperX Alloy Elite Device"
    PLANE_DELAY = 0.005
    LED_NAMES = ELITE_KEY_NAMES + [f"RGB Strip {i}" for i in range(1, 19)] + _names(
        "Media Previous|Media Play/Pause|Media Next|Media Mute")

    def _build_device(self) -> Device:
        zones = [Zone(0, "Keyboard", ZONE_MATRIX, 106, 106, 106, [list(r) for r in ELITE_MATRIX], 0),
                 Zone(1, "RGB Strip", ZONE_LINEAR, 18, 18, 18, None, 106),
                 Zone(2, "Media Keys", ZONE_SINGLE, 4, 4, 4, None, 124)]
        auto = MODE_FLAG_AUTOMATIC_SAVE
        modes = [
            Mode(0, "Direct", ELITE_MODE_STATIC, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            Mode(1, "Static", ELITE_MODE_STATIC, MODE_FLAG_HAS_PER_LED_COLOR | auto, color_mode=MODE_COLORS_PER_LED),
            Mode(2, "Wave", ELITE_MODE_WAVE, MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_DIRECTION_LR | auto,
                 speed_min=0x00, speed_max=0x09, speed=0x09, direction=0, color_mode=MODE_COLORS_NONE),
            Mode(3, "Breathing", ELITE_MODE_BREATHING,
                 MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_RANDOM_COLOR | auto,
                 speed_min=0x00, speed_max=0x09, speed=0x09, colors_min=1, colors_max=2,
                 color_mode=MODE_COLORS_MODE_SPECIFIC, colors=[0, 0]),
        ]
        return self._make_device(modes, zones, list(self.LED_NAMES))

    @staticmethod
    def _ext_plane(pkt_id: int, b2: int, b3: int, colors: Sequence[int]) -> bytes:
        """Extended plane: strip + media LEDs, R/G/B interleaved at ELITE_EXT_* offsets, bytes 0x08..0x93 copied."""
        ext = bytearray(150)
        for i, c in enumerate(colors[:22]):
            r, g, b = u32_to_rgb(c)
            ext[ELITE_EXT_RED[i]] = r
            ext[ELITE_EXT_GRN[i]] = g
            ext[ELITE_EXT_BLU[i]] = b
        buf = bytearray(264)
        buf[0] = 0x07
        buf[1] = pkt_id
        buf[2] = b2
        buf[3] = b3
        buf[0x08:0x94] = ext[0x08:0x94]
        return bytes(buf)

    def _planes(self, colors: List[int], direct: bool):
        rgb = [u32_to_rgb(c) for c in colors[:106]]
        for ch, idx in ((ELITE_CH_RED, 0), (ELITE_CH_GREEN, 1), (ELITE_CH_BLUE, 2)):
            plane = [t[idx] for t in rgb]
            if direct:                                                      # SendDirect: 07 16 <ch> A0
                self.hid.send_feature(self._plane(ELITE_PKT_DIRECT, ch, 0xA0, plane))
            else:                                                           # SendColor: 07 06 <profile=1> <ch>
                self.hid.send_feature(self._plane(ELITE_PKT_SET_COLOR, 0x01, ch, plane))
            time.sleep(self.PLANE_DELAY)
        if direct:
            self.hid.send_feature(self._ext_plane(ELITE_PKT_DIRECT, ELITE_CH_EXT, 0xA0, colors[106:128]))
        else:
            self.hid.send_feature(self._ext_plane(ELITE_PKT_SET_COLOR, 0x01, ELITE_CH_EXT, colors[106:128]))

    def _send_direct(self, colors: List[int]):
        self._planes(colors, direct=True)

    def set_colors(self, colors: List[int]):
        # RGBController_HyperXAlloyElite::DeviceUpdateLEDs: Direct -> SetLEDsDirect, anything else -> SetLEDs (profile)
        frame = self._fit(colors)
        self.device.colors = list(frame)
        self.last_frame = frame
        self._planes(frame, direct=self.device.active_mode == 0)
        self.last_send = time.monotonic()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        colors = list(mode.colors) if mode.color_mode == MODE_COLORS_MODE_SPECIFIC else []
        self._send_effect(mode.value, mode.direction, mode.speed, colors)

    def _send_effect(self, effect: int, direction: int, speed: int, colors: List[int]):
        """HyperXAlloyEliteController::SetMode -> SendEffect(profile 1, ...), then 100 ms."""
        mc = [0] * 9
        if len(colors) == 1:
            color_mode = ELITE_COLOR_SINGLE
            mc[0:3] = u32_to_rgb(colors[0])
        elif len(colors) == 2:
            color_mode = ELITE_COLOR_DUAL
            mc[3:6] = u32_to_rgb(colors[0])
            mc[6:9] = u32_to_rgb(colors[1])
        else:
            color_mode = ELITE_COLOR_SPECTRUM
        buf = bytearray(264)
        buf[0x00] = 0x07
        buf[0x01] = ELITE_PKT_SET_EFFECT
        buf[0x02] = 0x01                      # profile
        buf[0x09] = 0x01
        buf[0x0A] = effect & 0xFF
        buf[0x0D] = direction & 0xFF
        buf[0x0E] = direction & 0xFF
        for i in range(0x1B, 0x23):
            buf[i] = ELITE_REACTIVE_NONE
        buf[0x29], buf[0x41], buf[0x59] = mc[0], mc[1], mc[2]
        buf[0x2A], buf[0x42], buf[0x5A] = mc[3], mc[4], mc[5]
        buf[0x2B], buf[0x43], buf[0x5B] = mc[6], mc[7], mc[8]
        buf[0x6B:0x71] = bytes((0x09, 0x09, 0x05, 0x05, 0x06, 0x05))
        buf[0x71] = speed & 0xFF
        buf[0x72] = 0x09
        for i in range(0x73, 0x7B):
            buf[i] = color_mode
        self.hid.send_feature(bytes(buf))
        time.sleep(0.100)


# ----------------------------------------------------------------------------------------------------------------
# Alloy Origins Core (TKL) -- output reports, 0xA2 color stream, layout from OpenRGB's KeyboardLayoutManager
# ----------------------------------------------------------------------------------------------------------------
CORE_VARIANT_ANSI, CORE_VARIANT_ABNT2 = 0x09, 0x10

# KeyboardLayoutManager.cpp: TKL = main block + function row + extras, sorted by (row, col). Index order here is the
# order the manager ends up with, which is what RGBController_HyperXAlloyOriginsCore.cpp's default values index.
_CORE_BASE: List[Tuple[int, int, str]] = (
    [(0, 0, "Escape")] + [(0, c + 2, f"F{c + 1}") for c in range(12)]
    + [(0, 14, "Print Screen"), (0, 15, "Scroll Lock"), (0, 16, "Pause/Break")]
    + [(1, 0, "`")] + [(1, c, str(c % 10)) for c in range(1, 11)]
    + [(1, 11, "-"), (1, 12, "="), (1, 13, "Backspace"), (1, 14, "Insert"), (1, 15, "Home"), (1, 16, "Page Up")]
    + [(2, 0, "Tab")] + [(2, c + 1, k) for c, k in enumerate("QWERTYUIOP")]
    + [(2, 11, "["), (2, 12, "]"), (2, 13, "\\ (ANSI)"), (2, 14, "Delete"), (2, 15, "End"), (2, 16, "Page Down")]
    + [(3, 0, "Caps Lock")] + [(3, c + 1, k) for c, k in enumerate("ASDFGHJKL")]
    + [(3, 10, ";"), (3, 11, "'"), (3, 12, "#"), (3, 13, "Enter")]
    + [(4, 0, "Left Shift"), (4, 1, "\\ (ISO)")] + [(4, c + 2, k) for c, k in enumerate("ZXCVBNM")]
    + [(4, 9, ","), (4, 10, "."), (4, 11, "/"), (4, 13, "Right Shift"), (4, 15, "Up Arrow")]
    + [(5, 0, "Left Control"), (5, 1, "Left Windows"), (5, 2, "Left Alt"), (5, 6, "Space"), (5, 10, "Right Alt"),
       (5, 11, "Right Fn"), (5, 12, "Menu"), (5, 13, "Right Control"), (5, 14, "Left Arrow"), (5, 15, "Down Arrow"),
       (5, 16, "Right Arrow")]
)
# RGBController_HyperXAlloyOriginsCore.cpp hyperx_core_default_values (LED slot per key, same order as _CORE_BASE)
CORE_DEFAULT_VALUES = (
    0, 1, 2, 3, 4, 5, 6, 7, 48, 49, 50, 51, 52, 53, 54, 55,
    8, 9, 10, 11, 12, 13, 14, 15, 16, 56, 57, 58, 59, 60, 61, 62, 63,
    17, 18, 19, 20, 21, 22, 23, 24, 64, 65, 66, 67, 68, 69, 70, 71, 72,
    25, 26, 27, 28, 29, 30, 31, 32, 73, 74, 75, 76, 77, 78,
    33, 34, 35, 36, 37, 38, 39, 40, 79, 80, 81, 82, 84, 85,
    41, 42, 43, 45, 86, 87, 88, 89, 90, 91, 92,
)


@dataclass
class _CoreKey:
    row: int
    col: int
    name: str        # OpenRGB key name ("Key: A"), "" = unused
    alt: str = ""    # regional alternate name, preferred when present
    value: int = 0   # LED slot in the 94-slot color buffer


def core_keymap(variant: int) -> List[_CoreKey]:
    """Reproduce KeyboardLayoutManager(KEYBOARD_LAYOUT_ANSI_QWERTY|ABNT2, KEYBOARD_SIZE_TKL, hyperx_core_layout)."""
    keys = [_CoreKey(r, c, "Key: " + n, "", v) for (r, c, n), v in zip(_CORE_BASE, CORE_DEFAULT_VALUES)]

    def at(row: int, col: int) -> Optional[_CoreKey]:
        return next((k for k in keys if k.row == row and k.col == col), None)

    def remove(row: int, col: int):                      # KEYBOARD_OPCODE_SWAP_ONLY with KEY_EN_UNUSED
        k = at(row, col)
        if k:
            keys.remove(k)

    if variant == CORE_VARIANT_ABNT2:
        # KeyboardLayoutManager.cpp `abnt2` overlay
        for (r, c), alt in {(1, 0): "Key: '", (2, 11): "Key: ´ `", (2, 12): "Key: [", (3, 10): "Key: ç",
                            (3, 11): "Key: ~", (3, 12): "Key: ]"}.items():
            k = at(r, c)
            if k:
                k.alt = alt
        # INSERT_SHIFT_RIGHT KEY_EN_SEMICOLON @ (4, 11): insert before "/", shift the rest of row 4 right
        pos = next(i for i, k in enumerate(keys) if (k.row, k.col) >= (4, 11))
        for k in keys[pos:]:
            if k.row == 4 and k.col >= 11:
                k.col += 1
        keys.insert(pos, _CoreKey(4, 11, "Key: ;"))
        remove(2, 13)                                     # SWAP_ONLY unused @ (2, 13): drop ANSI backslash
        # REMOVE_SHIFT_LEFT @ (4, 13): no key there, so the keys right of it move one column left
        for k in keys:
            if k.row == 4 and k.col > 13:
                k.col -= 1
        # RGBController_HyperXAlloyOriginsCore.cpp regional_overlay_abnt2 (SWAP_ONLY with explicit values)
        for (r, c), (name, value) in {(4, 11): ("Key: ;", 82), (4, 12): ("Key: /", 83)}.items():
            k = at(r, c)
            if k:
                k.name, k.value = name, value
    else:
        # `ansi_qwerty` overlay: drop ISO-only keys
        remove(3, 12)
        remove(4, 1)
    return keys


class HyperXAlloyOriginsCore(_HyperXDevice):
    """Alloy Origins Core (0951:16E6 / 03F0:098F, interface 2). HyperXAlloyOriginsCoreController.cpp:
    output reports (hid_write), 380-byte GRB buffer streamed as 0xA2 packets, brightness via 0xA7."""
    DESCRIPTION = "HyperX Alloy Origins Core Keyboard Device"
    BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 255

    def __init__(self, hid: HidDevice, info: HidInfo, name: str):
        self.variant = 0
        self.keys: List[_CoreKey] = []
        super().__init__(hid, info, name)

    def _query_variant(self) -> int:
        """Command 0x10 -> 65-byte answer, layout variant at byte 56 (0x09 ANSI, 0x10 ABNT2)."""
        pkt = bytearray(65)
        pkt[1] = 0x10
        try:
            self.hid.drain()
            self.hid.write(bytes(pkt))
            reply = self.hid.read(65, timeout=1.0)
        except DeviceError as e:
            log.info("%s: variant query failed (%s), assuming ANSI", self.name, e)
            return 0
        if not reply or len(reply) <= 56:
            return 0
        return reply[56]

    def _build_device(self) -> Device:
        self.variant = self._query_variant()
        log.info("%s: layout variant 0x%02X", self.name, self.variant)
        self.keys = core_keymap(self.variant)
        rows = max(k.row for k in self.keys) + 1
        cols = max(k.col for k in self.keys) + 1
        matrix = [[NA] * cols for _ in range(rows)]
        for i, k in enumerate(self.keys):
            matrix[k.row][k.col] = i
        names = _fill_unused([k.alt or k.name for k in self.keys])
        n = len(names)
        zone = Zone(0, "Keyboard", ZONE_MATRIX, n, n, n, matrix)
        direct = _direct_mode(MODE_FLAG_HAS_BRIGHTNESS, brightness_min=self.BRIGHTNESS_MIN,
                              brightness_max=self.BRIGHTNESS_MAX, brightness=self.BRIGHTNESS_MAX)
        return self._make_device([direct], [zone], names)

    def color_buffer(self, colors: List[int]) -> bytes:
        """SetLEDsDirect: 48-byte segments of 2x(6 G + 2 pad), 2x(6 R + 2 pad), 2x(6 B + 2 pad)."""
        buf = bytearray(380)
        for k, c in list(zip(self.keys, colors))[:94]:
            v = k.value
            pos = (v // 12) * 48 + ((v // 6) & 1) * 8 + v % 6
            r, g, b = u32_to_rgb(c)
            buf[pos] = g
            buf[pos + 16] = r
            buf[pos + 32] = b
        return bytes(buf)

    def _send_direct(self, colors: List[int]):
        """SendRGBToDevice: 00 A2 <seq> 00 <len> + up to 60 payload bytes, written as len+5 bytes."""
        data = self.color_buffer(colors)
        seq = 0
        sent = 0
        while sent < len(data):
            payload = min(60, len(data) - sent)
            pkt = bytearray(5 + payload)
            pkt[1] = 0xA2
            pkt[2] = seq
            pkt[4] = payload
            pkt[5:] = data[sent:sent + payload]
            self.hid.write(bytes(pkt))
            seq += 1
            sent += payload

    def set_brightness(self, brightness: int):
        pkt = bytearray(65)
        pkt[1] = 0xA7
        pkt[4] = 0x01
        pkt[5] = brightness & 0xFF
        self.hid.write(bytes(pkt))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_brightness(mode.brightness)             # DeviceUpdateMode -> SetBrightness


# ----------------------------------------------------------------------------------------------------------------
# Eve 1800 / Origins 2 65 -- output reports with report id 0x44
# ----------------------------------------------------------------------------------------------------------------
ORIGINS2_65_LED_NAMES = _names(
    "Escape|1|2|3|4|5|6|7|8|9|0|-|=|Backspace|Delete|"
    "Tab|Q|W|E|R|T|Y|U|I|O|P|[|]||Home|"
    "Caps Lock|A|S|D|F|G|H|J|K|L|;|'|#|Enter (ISO)|Page Up|"
    "Left Shift|Z|X|C|V|B|N|M|,|.|/|Right Shift|Up Arrow||Page Down|"
    "Left Control|\\ (ISO)|Left Windows|Left Alt||~Left Space|Space|~Right Space||Right Alt|Right Fn|Left Arrow|"
    "Down Arrow|Right Arrow")
# RGBController_HyperXOrigins2_65.cpp matrix_map[5][15]
ORIGINS2_65_MATRIX = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
    [15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, NA, 29],
    [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44],
    [45, 61, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 59],
    [60, 62, 63, 65, NA, NA, 66, NA, NA, 68, 69, 70, 71, 72, 73],
]


def _report44(cmd: int, b2: int, colors: Sequence[int]) -> bytes:
    """65-byte output report 44 <cmd> <b2> 00 + up to 20 RGB triplets (Eve 1800 / Origins 2 65 / shared format)."""
    buf = bytearray(65)
    buf[0] = 0x44
    buf[1] = cmd
    buf[2] = b2
    for i, c in enumerate(colors[:20]):
        buf[4 + i * 3:7 + i * 3] = u32_to_rgb(c)
    return bytes(buf)


class HyperXOrigins2_65(_HyperXDevice):
    """Origins 2 65 (03F0:0CC2, interface 3). HyperXOrigins2_65Controller.cpp: init 44 01 04, 4 x (44 02 seq)."""
    DESCRIPTION = "HyperX Origins 2 65 Keyboard Device"

    def _build_device(self) -> Device:
        names = _fill_unused(ORIGINS2_65_LED_NAMES)
        n = len(names)
        zone = Zone(0, "Keyboard", ZONE_MATRIX, n, n, n, [list(r) for r in ORIGINS2_65_MATRIX])
        return self._make_device([_direct_mode()], [zone], names)

    def _send_direct(self, colors: List[int]):
        self.hid.write(_report44(0x01, 0x04, []))
        for seq in range(4):
            chunk = colors[seq * 20:seq * 20 + 20] or [0]
            self.hid.write(_report44(0x02, seq, chunk))


class HyperXEve1800(_HyperXDevice):
    """Eve 1800 (03F0:08C2, interface 2). HyperXEve1800Controller.cpp: 10 zones in one 44 02 report, brightness 40 01."""
    DESCRIPTION = "HyperX Eve 1800 Keyboard Device"
    ZONES = 10

    def _build_device(self) -> Device:
        names = [f"Zone {i + 1}" for i in range(self.ZONES)]
        zone = Zone(0, "Keyboard", ZONE_LINEAR, self.ZONES, self.ZONES, self.ZONES)
        direct = _direct_mode(MODE_FLAG_HAS_BRIGHTNESS, brightness_min=0x00, brightness_max=0xFF, brightness=0xFF)
        return self._make_device([direct], [zone], names)

    def _send_direct(self, colors: List[int]):
        self.hid.write(_report44(0x01, 0x04, []))
        self.hid.write(_report44(0x02, 0x00, colors or [0]))

    def set_brightness(self, brightness: int):
        buf = bytearray(65)
        buf[0] = 0x40
        buf[1] = 0x01
        buf[4] = brightness & 0xFF
        self.hid.write(bytes(buf))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self.set_brightness(mode.brightness)             # DeviceUpdateMode: brightness, then the frame again
        if self.last_frame is not None:
            self._send_direct(self.last_frame)


# ----------------------------------------------------------------------------------------------------------------
# Mice
# ----------------------------------------------------------------------------------------------------------------
class HyperXPulsefireSurge(_HyperXDevice):
    """Pulsefire Surge (0951:16D3 / 03F0:0490, interface 1, usage page 0xFF01). HyperXPulsefireSurgeController.cpp:
    one 264-byte feature report 07 14 00 A0 with R/G/B planes for the 32-LED ring and RGB for the logo."""
    TYPE_NAME = "Mouse"
    DESCRIPTION = "HyperX Pulsefire Surge Device"
    PKT_DIRECT = 0x14

    def _build_device(self) -> Device:
        zones = [Zone(0, "LED Strip", ZONE_LINEAR, 32, 32, 32, None, 0), Zone(1, "Logo", ZONE_SINGLE, 1, 1, 1, None, 32)]
        names = [f"LED Strip LED {i + 1}" for i in range(32)] + ["Logo"]
        return self._make_device([_direct_mode()], zones, names)

    def _send_direct(self, colors: List[int]):
        buf = bytearray(264)
        buf[0x00] = 0x07
        buf[0x01] = self.PKT_DIRECT
        buf[0x03] = 0xA0
        for i in range(32):
            r, g, b = u32_to_rgb(colors[i])
            buf[0x08 + i] = r
            buf[0x28 + i] = g
            buf[0x48 + i] = b
        buf[0x6C:0x6F] = u32_to_rgb(colors[32])
        self.hid.send_feature(bytes(buf))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if self.last_frame is not None:                  # DeviceUpdateMode -> DeviceUpdateLEDs
            self._send_direct(self.last_frame)


class HyperXPulsefireFPSPro(HyperXPulsefireSurge):
    """Pulsefire FPS Pro (0951:16D7) and Pulsefire Core (0951:16DE / 03F0:0D8F), interface 1, usage page 0xFF01.
    HyperXPulsefireFPSProController.cpp: 264-byte feature report 07 0A R G B 00 00 00 A0."""
    DESCRIPTION = "HyperX Pulsefire FPS Pro Device"
    PKT_DIRECT = 0x0A

    def _build_device(self) -> Device:
        return self._make_device([_direct_mode()], [Zone(0, "Logo", ZONE_SINGLE, 1, 1, 1)], ["Logo"])

    def _send_direct(self, colors: List[int]):
        buf = bytearray(264)
        buf[0x00] = 0x07
        buf[0x01] = self.PKT_DIRECT
        buf[0x02:0x05] = u32_to_rgb(colors[0])
        buf[0x08] = 0xA0
        self.hid.send_feature(bytes(buf))


class HyperXPulsefireRaid(_HyperXDevice):
    """Pulsefire Raid (0951:16E4, interface 1, usage page 0xFF01, usage 0x01). HyperXPulsefireRaidController.cpp:
    264-byte feature report 07 0A R1 G1 B1 R2 G2 B2 A0, 10 ms after each; keepalive threshold 1 s.
    OpenRGB flags brightness on Direct but never sends it (the controller has no brightness packet)."""
    TYPE_NAME = "Mouse"
    DESCRIPTION = "HyperX Pulsefire Raid Device"
    KEEPALIVE_INTERVAL = 1.0

    def _build_device(self) -> Device:
        zones = [Zone(0, "Scroll Wheel", ZONE_SINGLE, 1, 1, 1, None, 0), Zone(1, "Logo", ZONE_SINGLE, 1, 1, 1, None, 1)]
        direct = Mode(0, "Direct", 0x00, MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS,
                      brightness_min=0x00, brightness_max=0x64, brightness=0x64, color_mode=MODE_COLORS_PER_LED)
        return self._make_device([direct], zones, ["Scroll Wheel", "Logo"])

    def _send_direct(self, colors: List[int]):
        buf = bytearray(264)
        buf[0] = 0x07
        buf[1] = 0x0A
        for i in range(2):
            buf[3 * i + 2:3 * i + 5] = u32_to_rgb(colors[i])
        buf[8] = 0xA0
        self.hid.send_feature(bytes(buf))
        time.sleep(0.010)


class HyperXPulsefireHaste(_HyperXDevice):
    """Pulsefire Haste (0951:1727, 03F0:0F8F). HyperXPulsefireHasteController.cpp: two 65-byte feature reports,
    00 04 F2 .. [8]=02 then 00 81 R G B .. [8]=02."""
    TYPE_NAME = "Mouse"
    DESCRIPTION = "HyperX Pulsefire Haste Device"

    def _build_device(self) -> Device:
        return self._make_device([_direct_mode()], [Zone(0, "Logo", ZONE_SINGLE, 1, 1, 1)], ["Logo"])

    def _send_direct(self, colors: List[int]):
        setup = bytearray(65)
        setup[1] = 0x04
        setup[2] = 0xF2
        setup[8] = 0x02
        self.hid.send_feature(bytes(setup))
        col = bytearray(65)
        col[1] = 0x81
        col[2:5] = u32_to_rgb(colors[0])
        col[8] = 0x02
        self.hid.send_feature(bytes(col))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if self.last_frame is not None:
            self._send_direct(self.last_frame)


DART_PKT_DIRECT, DART_PKT_SAVE = 0xD2, 0xDE
DART_MODE_STATIC, DART_MODE_CYCLE, DART_MODE_BREATHING, DART_MODE_REACTIVE = 0x00, 0x12, 0x20, 0x30
DART_SPEED_MIN, DART_SPEED_MAX, DART_SPEED_MED, DART_SPEED_NONE = 0x64, 0x00, 0x32, 0x00
DART_BRIGHTNESS_MIN, DART_BRIGHTNESS_MAX = 0x00, 0x64
DART_LED_LOGO, DART_LED_SCROLL, DART_LED_ALL = 0x00, 0x10, 0x20


class HyperXPulsefireDart(_HyperXDevice):
    """Pulsefire Dart (wireless 0951:16E1 / 03F0:068E iface 2 page 0xFF00; wired 0951:16E2 / 03F0:088E iface 1
    page 0xFF13). HyperXPulsefireDartController.cpp: 65-byte output reports 00 D2 <led> <mode> 08 RGB RGB <bri> <spd>,
    2 ms after each; 00 DE FF saves to onboard memory. No keepalive thread in OpenRGB.
    Note: OpenRGB's speed range is inverted (speed_min 0x64 is slowest, speed_max 0x00 fastest)."""
    TYPE_NAME = "Mouse"
    DESCRIPTION = "HyperX Pulsefire Dart Device"
    KEEPALIVE_INTERVAL = 0.0     # OpenRGB has no keepalive for this device

    def _build_device(self) -> Device:
        zones = [Zone(0, "Scroll Wheel", ZONE_SINGLE, 1, 1, 1, None, 0), Zone(1, "Logo", ZONE_SINGLE, 1, 1, 1, None, 1)]
        bri = dict(brightness_min=DART_BRIGHTNESS_MIN, brightness_max=DART_BRIGHTNESS_MAX, brightness=DART_BRIGHTNESS_MAX)
        per_led = MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_MANUAL_SAVE
        with_speed = MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_MANUAL_SAVE
        modes = [
            Mode(0, "Direct", DART_MODE_STATIC, per_led, speed=DART_SPEED_NONE, color_mode=MODE_COLORS_PER_LED, **bri),
            Mode(1, "Breathing", DART_MODE_BREATHING, per_led | MODE_FLAG_HAS_SPEED, speed_min=DART_SPEED_MIN,
                 speed_max=DART_SPEED_MAX, speed=DART_SPEED_MED, color_mode=MODE_COLORS_PER_LED, **bri),
            Mode(2, "Spectrum Cycle", DART_MODE_CYCLE, with_speed, speed_min=DART_SPEED_MIN, speed_max=DART_SPEED_MAX,
                 speed=DART_SPEED_MED, color_mode=MODE_COLORS_NONE, **bri),
            Mode(3, "Reactive", DART_MODE_REACTIVE, per_led | MODE_FLAG_HAS_SPEED, speed_min=DART_SPEED_MIN,
                 speed_max=DART_SPEED_MAX, speed=DART_SPEED_MED, color_mode=MODE_COLORS_PER_LED, **bri),
        ]
        return self._make_device(modes, zones, ["Scroll Wheel", "Logo"])

    def _packet(self, color: int, led: int, mode: int, brightness: int, speed: int) -> bytes:
        buf = bytearray(65)
        buf[0x01] = DART_PKT_DIRECT
        buf[0x02] = led
        buf[0x03] = mode & 0xFF
        buf[0x04] = 0x08
        rgb = u32_to_rgb(color)
        buf[0x05:0x08] = rgb
        buf[0x08:0x0B] = rgb
        buf[0x0B] = brightness & 0xFF
        buf[0x0C] = speed & 0xFF
        return bytes(buf)

    def _send_direct(self, colors: List[int]):
        """RGBController_HyperXPulsefireDart::DeviceUpdateMode (also used for LED updates)."""
        mode = self.device.current_mode
        if mode.color_mode == MODE_COLORS_PER_LED:
            self.hid.write(self._packet(colors[0], DART_LED_SCROLL, mode.value, mode.brightness, mode.speed))
            time.sleep(0.002)
            self.hid.write(self._packet(colors[1], DART_LED_LOGO, mode.value, mode.brightness, mode.speed))
        else:
            self.hid.write(self._packet(colors[0], DART_LED_ALL, mode.value, mode.brightness, mode.speed))
        time.sleep(0.002)

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        self._send_direct(self.last_frame or [0] * 2)
        if save:
            buf = bytearray(65)
            buf[0x01] = DART_PKT_SAVE
            buf[0x02] = 0xFF
            self.hid.write(bytes(buf))
            time.sleep(0.002)

    def keepalive(self):
        """The Dart keeps its last state on its own; OpenRGB runs no keepalive thread for it."""


# ----------------------------------------------------------------------------------------------------------------
# Mousemats
# ----------------------------------------------------------------------------------------------------------------
class HyperXMousemat(_HyperXDevice):
    """Fury Ultra (0951:1705), Pulsefire Mat (03F0:0F8D), Pulsefire Mat RGB XL (0951:1741).
    HyperXMousematController.cpp SendDirect: 00 04 F2 .. [9]=02, then two 16-entry 0x81-quad reports (32 slots;
    OpenRGB reads past its 20/2-colour vector there - we send zeros)."""
    TYPE_NAME = "Mousemat"
    DESCRIPTION = "HyperX Mousemat Device"
    KEEPALIVE_DIRECT_ONLY = True

    def __init__(self, hid: HidDevice, info: HidInfo, name: str, underglow: int = 15, strip: int = 5):
        self.underglow = underglow
        self.strip = strip
        super().__init__(hid, info, name)

    def _build_device(self) -> Device:
        zones: List[Zone] = []
        names: List[str] = []
        if self.underglow:
            zones.append(Zone(len(zones), "Underglow", ZONE_LINEAR, self.underglow, self.underglow, self.underglow,
                              None, 0))
            names += [f"Underglow LED {i + 1}" for i in range(self.underglow)] if self.underglow > 1 else ["Underglow"]
        if self.strip:
            zones.append(Zone(len(zones), "LED Strip", ZONE_LINEAR, self.strip, self.strip, self.strip, None, len(names)))
            names += [f"LED Strip LED {i + 1}" for i in range(self.strip)] if self.strip > 1 else ["LED Strip"]
        return self._make_device([_direct_mode()], zones, names)

    def _send_direct(self, colors: List[int]):
        init = bytearray(65)
        init[1] = 0x04
        init[2] = 0xF2
        init[9] = 0x02
        self.hid.send_feature(bytes(init))
        slots = list(colors) + [0] * (32 - len(colors))
        self.hid.send_feature(_quad_packet(slots[0:16]))
        self.hid.send_feature(_quad_packet(slots[16:32]))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if self.last_frame is not None:
            self._send_direct(self.last_frame)


# ----------------------------------------------------------------------------------------------------------------
# Microphones
# ----------------------------------------------------------------------------------------------------------------
class HyperXQuadCastS(_HyperXDevice):
    """QuadCast S / DuoCast, interface 0. HyperXMicrophoneController.cpp: 65-byte feature reports, 15 ms pacing.
    Direct = colour packet (81 RGB 81 RGB) + register 0xF2; save = 0x53 transaction with one frame."""
    TYPE_NAME = "Microphone"
    DESCRIPTION = "HyperX Microphone Device"
    KEEPALIVE_DIRECT_ONLY = False
    PACE = 0.015

    def _build_device(self) -> Device:
        direct = Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_MANUAL_SAVE, color_mode=MODE_COLORS_PER_LED)
        return self._make_device([direct], [Zone(0, "Microphone", ZONE_SINGLE, 2, 2, 2)], ["Top", "Bottom"])

    @staticmethod
    def _color_packet(pairs: Sequence[Tuple[int, int]]) -> bytes:
        buf = bytearray(65)
        for i, (top, bot) in enumerate(pairs[:8]):
            idx = 8 * i
            buf[idx + 1] = 0x81
            buf[idx + 2:idx + 5] = u32_to_rgb(top)
            buf[idx + 5] = 0x81
            buf[idx + 6:idx + 9] = u32_to_rgb(bot)
        return bytes(buf)

    def _register(self, reg: int, p1: int, p2: int):
        buf = bytearray(65)
        buf[1] = 0x04
        buf[2] = reg
        buf[8] = p1
        buf[9] = p2
        self.hid.send_feature(bytes(buf))
        time.sleep(self.PACE)

    def _send_direct(self, colors: List[int]):
        self.hid.send_feature(self._color_packet([(colors[0], colors[1])]))
        time.sleep(self.PACE)
        self._register(0xF2, 0, 1)

    def save_colors(self, colors: List[int], frames: int = 1):
        """SaveColors(colors, num_frames): animation frames of (top, bottom) pairs written to onboard memory."""
        pairs = [(colors[2 * f], colors[2 * f + 1]) for f in range(frames)]
        packets = (frames + 7) // 8
        self._register(0x53, packets, 0)
        for p in range(packets):
            time.sleep(self.PACE)
            self.hid.send_feature(self._color_packet(pairs[p * 8:p * 8 + 8]))
        self._register(0x02, 0, 0)
        self._register(0x23, 1, 0)
        eot = bytearray(65)                              # SendEOT
        eot[0x01] = 0x08
        eot[0x3C] = 0x28
        eot[0x3D] = frames & 0xFF
        eot[0x3F] = 0xAA
        eot[0x40] = 0x55
        self.hid.send_feature(bytes(eot))
        time.sleep(self.PACE)
        self._register(0x02, 0, 0)
        self._send_direct(colors)                        # "likes one temporary direct packet after the save"

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        frame = self.last_frame or [0, 0]
        if save:
            self.save_colors(frame, 1)
        else:
            self._send_direct(frame)


# RGBController_HyperXMicrophoneV2.cpp led_mapping (9 rows x 12 columns: 6 front, 6 rear)
QUADCAST2S_MATRIX = [
    [26, 27, 44, 45, 62, 63, 80, 81, 98, 99, 8, 9],
    [25, 28, 43, 46, 61, 64, 79, 82, 97, 100, 7, 10],
    [24, 29, 42, 47, 60, 65, 78, 83, 96, 101, 6, 11],
    [23, 30, 41, 48, 59, 66, 77, 84, 95, 102, 5, 12],
    [22, 31, 40, 49, 58, 67, 76, 85, 94, 103, 4, 13],
    [21, 32, 39, 50, 57, 68, 75, 86, 93, 104, 3, 14],
    [20, 33, 38, 51, 56, 69, 74, 87, 92, 105, 2, 15],
    [19, 34, 37, 52, 55, 70, 73, 88, 91, 106, 1, 16],
    [18, 35, 36, 53, 54, 71, 72, 89, 90, 107, 0, 17],
]


class HyperXQuadCast2S(_HyperXDevice):
    """QuadCast 2 S (03F0:02B5, interface 1, usage page 0xFF13, usage 0xFF00). HyperXMicrophoneV2Controller.cpp:
    64-byte output reports (report id 0x44), each answered by the device echoing bytes 0-1 at offsets 14-15;
    6 colour packets of 20 LEDs. Keepalive threshold 1 s (250 ms poll)."""
    TYPE_NAME = "Microphone"
    DESCRIPTION = "HyperX Microphone Device"
    KEEPALIVE_INTERVAL = 1.0
    KEEPALIVE_DIRECT_ONLY = False
    LEDS = 108
    REPLY_TIMEOUT = 2.0

    def __init__(self, hid: HidDevice, info: HidInfo, name: str):
        self.errors = 0
        self.last_error = 0.0
        self.pause_until = 0.0
        super().__init__(hid, info, name)

    def _build_device(self) -> Device:
        direct = Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_MANUAL_SAVE, colors_min=self.LEDS,
                      colors_max=self.LEDS, color_mode=MODE_COLORS_PER_LED)
        zone = Zone(0, "Microphone", ZONE_MATRIX, self.LEDS, self.LEDS, self.LEDS, [list(r) for r in QUADCAST2S_MATRIX])
        return self._make_device([direct], [zone], [f"LED {i}" for i in range(self.LEDS)])

    def _track_error(self):
        now = time.monotonic()
        if now - self.last_error < 10:
            self.errors += 1
            if self.errors >= 5:
                self.hid.drain()
            if self.errors >= 10:
                log.warning("%s: repeated communication errors; another program (NGENUITY?) may own the device; "
                            "pausing updates for 5 s", self.name)
                self.pause_until = now + 5
                self.errors = 0
        else:
            self.errors = 1
        self.last_error = now

    def _xfer(self, pkt: bytes):
        self.hid.write(pkt)
        reply = self.hid.read(64, timeout=self.REPLY_TIMEOUT)
        if not reply or len(reply) < 16 or reply[14] != pkt[0] or reply[15] != pkt[1]:
            log.debug("%s: no/invalid echo for %02X %02X", self.name, pkt[0], pkt[1])
            self._track_error()

    def _color_packets(self, colors: List[int], cmd: int):
        for p in range(6):
            buf = bytearray(64)
            buf[0] = 0x44
            buf[1] = cmd
            buf[2] = p
            for c, col in enumerate(colors[p * 20:p * 20 + 20]):
                buf[4 + 3 * c:7 + 3 * c] = u32_to_rgb(col)
            self._xfer(bytes(buf))

    def _send_direct(self, colors: List[int]):
        if time.monotonic() < self.pause_until:
            return
        self._xfer(bytes([0x44, 0x01, 0x06]) + bytes(61))
        self._color_packets(colors, 0x02)

    def save_colors(self, colors: List[int]):
        if time.monotonic() < self.pause_until:
            return
        self._xfer(bytes([0x44, 0x03, 0x01, 0x06]) + bytes(60))
        self._color_packets(colors, 0x04)
        self._xfer(bytes([0x42, 0x02, 0x00, 0x00, 0x00, 0xE8, 0x03]) + bytes(57))    # "framerate" (static frame)
        self._xfer(bytes([0x40, 0x01, 0x00, 0x00, 0xFF]) + bytes(59))

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        frame = self.last_frame or [0] * self.LEDS
        if save:
            self.save_colors(frame)
        else:
            self._send_direct(frame)


# ----------------------------------------------------------------------------------------------------------------
# detection table (Linux rows of the *ControllerDetect.cpp REGISTER_HID_DETECTOR_* lines)
# ----------------------------------------------------------------------------------------------------------------
@dataclass
class _Rule:
    vid: int
    pid: int
    name: str
    factory: Callable[[HidDevice, HidInfo, str], _HyperXDevice]
    iface: int = -1          # -1 = any interface
    page: int = -1           # -1 = any usage page
    usage: int = -1          # -1 = any usage


def _mat(underglow: int, strip: int):
    return lambda hid, info, name: HyperXMousemat(hid, info, name, underglow, strip)


RULES: List[_Rule] = [
    # HyperXKeyboardController/*/*Detect.cpp
    _Rule(VID_KINGSTON, 0x16E5, "HyperX Alloy Origins", HyperXAlloyOrigins, 0),
    _Rule(VID_HP, 0x0591, "HyperX Alloy Origins (HP)", HyperXAlloyOrigins, 0),
    _Rule(VID_KINGSTON, 0x1734, "HyperX Alloy Origins 60", HyperXAlloyOrigins60, 0),
    _Rule(VID_HP, 0x0C8E, "HyperX Alloy Origins 60 (HP)", HyperXAlloyOrigins60, 0),
    _Rule(VID_HP, 0x038F, "HyperX Alloy Origins 65 (HP)", HyperXAlloyOrigins65, 0),
    _Rule(VID_KINGSTON, 0x1711, "HyperX Alloy Elite 2", HyperXAlloyElite2, 0),
    _Rule(VID_HP, 0x058F, "HyperX Alloy Elite 2 (HP)", HyperXAlloyElite2, 0),
    _Rule(VID_KINGSTON, 0x16BE, "HyperX Alloy Elite RGB", HyperXAlloyElite, 2, 0xFF01),
    _Rule(VID_KINGSTON, 0x16DC, "HyperX Alloy FPS RGB", HyperXAlloyFPS, 2, 0xFF01),
    _Rule(VID_KINGSTON, 0x16E6, "HyperX Alloy Origins Core", HyperXAlloyOriginsCore, 2),
    _Rule(VID_HP, 0x098F, "HyperX Alloy Origins Core (HP)", HyperXAlloyOriginsCore, 2),
    _Rule(VID_HP, 0x08C2, "HyperX Eve 1800 (HP)", HyperXEve1800, 2),
    _Rule(VID_HP, 0x0CC2, "HyperX Origins 2 65 (HP)", HyperXOrigins2_65, 3),
    # HyperXMouseController/HyperXMouseControllerDetect.cpp
    _Rule(VID_KINGSTON, 0x16D3, "HyperX Pulsefire Surge", HyperXPulsefireSurge, 1, 0xFF01),
    _Rule(VID_HP, 0x0490, "HyperX Pulsefire Surge (HP)", HyperXPulsefireSurge, 1, 0xFF01),
    _Rule(VID_KINGSTON, 0x16D7, "HyperX Pulsefire FPS Pro", HyperXPulsefireFPSPro, 1, 0xFF01),
    _Rule(VID_KINGSTON, 0x16DE, "HyperX Pulsefire Core", HyperXPulsefireFPSPro, 1, 0xFF01),
    _Rule(VID_HP, 0x0D8F, "HyperX Pulsefire Core (HP)", HyperXPulsefireFPSPro, 1, 0xFF01),
    _Rule(VID_KINGSTON, 0x16E1, "HyperX Pulsefire Dart (Wireless)", HyperXPulsefireDart, 2, 0xFF00),
    _Rule(VID_HP, 0x068E, "HyperX Pulsefire Dart (Wireless)", HyperXPulsefireDart, 2, 0xFF00),
    _Rule(VID_KINGSTON, 0x16E2, "HyperX Pulsefire Dart (Wired)", HyperXPulsefireDart, 1, 0xFF13),
    _Rule(VID_HP, 0x088E, "HyperX Pulsefire Dart (Wired)", HyperXPulsefireDart, 1, 0xFF13),
    _Rule(VID_KINGSTON, 0x16E4, "HyperX Pulsefire Raid", HyperXPulsefireRaid, 1, 0xFF01, 0x01),
    _Rule(VID_HP, 0x0F8F, "HyperX Pulsefire Haste", HyperXPulsefireHaste, 3, 0xFF90),
    _Rule(VID_HP, 0x0F8F, "HyperX Pulsefire Haste", HyperXPulsefireHaste, 0, 0x0001, 0x01),
    _Rule(VID_KINGSTON, 0x1727, "HyperX Pulsefire Haste", HyperXPulsefireHaste, -1, 0x0001, 0x02),
    # HyperXMousematController/HyperXMousematControllerDetect.cpp
    _Rule(VID_KINGSTON, 0x1705, "HyperX Fury Ultra", _mat(15, 5), 0),
    _Rule(VID_HP, 0x0F8D, "HyperX Pulsefire Mat", _mat(15, 5), 1, 0xFF90, 0xFF00),
    _Rule(VID_KINGSTON, 0x1741, "HyperX Pulsefire Mat RGB Mouse Pad XL", _mat(2, 0), 0, 0x0C, 0x01),
    # HyperXMicrophoneController/HyperXMicrophoneControllerDetect.cpp (interface 0)
    _Rule(VID_KINGSTON, 0x171F, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x0F8B, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x068C, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x0294, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x028C, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x048C, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x0D8B, "HyperX Quadcast S", HyperXQuadCastS, 0),
    _Rule(VID_HP, 0x098C, "HyperX DuoCast", HyperXQuadCastS, 0),
    # HyperXMicrophoneV2Controller/HyperXMicrophoneV2ControllerDetect.cpp
    _Rule(VID_HP, 0x02B5, "HyperX QuadCast 2 S", HyperXQuadCast2S, 1, 0xFF13, 0xFF00),
]

USB_IDS: List[Tuple[int, int]] = sorted({(r.vid, r.pid) for r in RULES})


def _rule_matches(rule: _Rule, info: HidInfo) -> bool:
    if rule.iface != -1 and info.interface != -1 and info.interface != rule.iface:
        return False
    if rule.page != -1 and not _has_usage_page(info.descriptor, rule.page):
        return False
    if rule.usage != -1 and not _has_usage(info.descriptor, rule.usage):
        return False
    return True


def probe() -> List[_HyperXDevice]:
    """Open every HyperX device whose hidraw node matches one of OpenRGB's Linux detector rows. Never raises."""
    found: List[_HyperXDevice] = []
    ids = set(USB_IDS)
    try:
        infos = [i for i in enumerate_hid() if (i.vid, i.pid) in ids]
    except Exception as e:  # noqa: BLE001 - enumeration must not take the backend down
        log.info("HyperX enumeration failed: %s", e)
        return found
    claimed: Dict[str, bool] = {}
    for rule in RULES:
        for info in infos:
            if (info.vid, info.pid) != (rule.vid, rule.pid) or not _rule_matches(rule, info):
                continue
            key = f"{info.vid:04x}:{info.pid:04x}:{_usb_parent(info.path)}"
            if claimed.get(key):
                continue
            try:
                hid = HidDevice(info.path)
            except DeviceError as e:
                log.info("%s at %s: %s", rule.name, info.path, e)
                continue
            try:
                drv = rule.factory(hid, info, rule.name)
            except Exception as e:  # noqa: BLE001 - one bad device must not hide the others
                hid.close()
                log.info("%s at %s failed to initialise: %s", rule.name, info.path, e)
                continue
            claimed[key] = True
            found.append(drv)
            log.info("%s: %d LEDs on %s (interface %d) [EXPERIMENTAL, unverified port]", rule.name,
                     len(drv.device.led_names), info.path, info.interface)
    return found
