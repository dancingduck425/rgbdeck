# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
# Packet formats derived from OpenRGB (https://gitlab.com/CalcProgrammer1/OpenRGB),
# Copyright (C) the OpenRGB contributors, GPL-2.0-or-later.
"""
Corsair keyboards / mice / mousemats / headset stands over /dev/hidraw.

EXPERIMENTAL: ported from OpenRGB sources, not yet verified on hardware.

Four Corsair wire protocols live here, chosen by product id:

* Legacy ("V1", 0x07/0x0E/0x7F 64-byte output reports) -- ported from
  OpenRGB Controllers/CorsairPeripheralController/CorsairPeripheralController.cpp and
  RGBController_CorsairPeripheral.cpp.  Keyboards K55 RGB, K65 RGB/LUX/RAPIDFIRE, K68 RGB,
  K70 RGB/LUX/RAPIDFIRE/MK.2/MK.2 SE/MK.2 LP/PRO Opto-mech, K95 RGB/PLATINUM/PLATINUM SE,
  Strafe RGB/Red/MK.2; mice Glaive RGB/PRO, Harpoon RGB/PRO, Ironclaw RGB, M65/M65 PRO/M65 RGB
  Elite, Nightsword, Scimitar RGB/PRO/Elite, Sabre RGB/Optical; MM800 RGB Polaris; ST100.
  Direct mode on all of them; onboard effects only on K70 RGB MK.2 / MK.2 Low Profile, as in
  OpenRGB.
* K55 RGB PRO XT (0x1BA1) -- CorsairK55RGBPROXTController.cpp (0x08 output reports, direct
  mode + onboard effects, keepalive every 50 s).
* K65 Mini (0x1BAF) -- CorsairK65MiniController.cpp (1024-byte 0x08 reports, direct only).
* "V2" (0x08 / 0x09 output reports with 0x0D/0x06/0x07/0x05 transactions) -- ported from
  Controllers/CorsairPeripheralV2Controller/*.cpp plus the key placement rules of
  KeyboardLayoutManager/KeyboardLayoutManager.cpp.  K55 RGB PRO, K57 RGB (wired), K60 RGB PRO
  (+SE/LP/TKL), K70 CORE RGB (+TKL), K70 RGB PRO (+V2), K70 RGB TKL (+CS), K95 RGB PLATINUM XT,
  K100 RGB (optical/MX Red); Dark Core RGB SE/PRO SE (wired), Harpoon Wireless, Ironclaw Wireless
  (+SE), Katar PRO (+V2/XT), M55 RGB PRO, M65 RGB Ultra (wired/wireless), M75 (+wireless),
  Scimitar Elite RGB (2nd gen), Slipstream receivers, MM700 (+3XL).  Direct mode only (OpenRGB's
  V2 hardware-mode writer is commented out upstream).

All Corsair devices here use the vendor collection on USB interface 1 (usage page 0xFFC2 for
legacy, 0xFF42 for the PRO XT and V2 generations); the MM800 and ST100 are matched on the usage
page alone, exactly as OpenRGB's detectors do.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from rgbdeck.models import (
    Device,
    DeviceError,
    Mode,
    Zone,
    NO_LED,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_NONE,
    MODE_COLORS_PER_LED,
    MODE_COLORS_RANDOM,
    MODE_FLAG_AUTOMATIC_SAVE,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_DIRECTION_LR,
    MODE_FLAG_HAS_DIRECTION_UD,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_HAS_RANDOM_COLOR,
    MODE_FLAG_HAS_SPEED,
    u32_to_rgb,
)
from rgbdeck.hw.hidraw import HidDevice, HidInfo, enumerate_hid

log = logging.getLogger("rgbdeck.corsair")

VID = 0x1B1C
PKT = 65  # 64-byte report + leading report-id byte 0x00 (hidraw strips it)
NA = NO_LED
ZONE_SINGLE, ZONE_LINEAR, ZONE_MATRIX = 0, 1, 2

# ---------------------------------------------------------------------------------------------
# Product ids (CorsairPeripheralControllerDetect.cpp, CorsairPeripheralV2Devices.h)
# ---------------------------------------------------------------------------------------------
V1_KEYBOARDS: Dict[int, str] = {
    0x1B3D: "Corsair K55 RGB",
    0x1B17: "Corsair K65 RGB",
    0x1B37: "Corsair K65 LUX RGB",
    0x1B39: "Corsair K65 RGB RAPIDFIRE",
    0x1B4F: "Corsair K68 RGB",
    0x1B3F: "Corsair K68 RED",
    0x1BA5: "Corsair K68 RED SHADOW",
    0x1B13: "Corsair K70 RGB",
    0x1B36: "Corsair K70 LUX",
    0x1B33: "Corsair K70 LUX RGB",
    0x1B38: "Corsair K70 RGB RAPIDFIRE",
    0x1B49: "Corsair K70 RGB MK.2",
    0x1B6B: "Corsair K70 RGB MK.2 SE",
    0x1B55: "Corsair K70 RGB MK.2 Low Profile",
    0x1BC6: "Corsair K70 RGB PRO Optomechanical",
    0x1B11: "Corsair K95 RGB",
    0x1B2D: "Corsair K95 RGB PLATINUM",
    0x1B82: "Corsair K95 RGB PLATINUM SE",
    0x1B20: "Corsair Strafe",
    0x1B44: "Corsair Strafe Red",
    0x1B48: "Corsair Strafe MK.2",
}
V1_MICE: Dict[int, str] = {
    0x1B34: "Corsair Glaive RGB",
    0x1B74: "Corsair Glaive RGB PRO",
    0x1B3C: "Corsair Harpoon RGB",
    0x1B75: "Corsair Harpoon RGB PRO",
    0x1B5D: "Corsair Ironclaw RGB",
    0x1B12: "Corsair M65",
    0x1B2E: "Corsair M65 PRO",
    0x1B5A: "Corsair M65 RGB Elite",
    0x1B5C: "Corsair Nightsword",
    0x1B1E: "Corsair Scimitar RGB",
    0x1B3E: "Corsair Scimitar PRO RGB",
    0x1B8B: "Corsair Scimitar Elite RGB",
    0x1B2F: "Corsair Sabre RGB",
    0x1B32: "Corsair Sabre RGB Optical",
}
V1_PAGE_ONLY: Dict[int, str] = {0x1B3B: "Corsair MM800 RGB Polaris", 0x0A34: "Corsair ST100 RGB"}
V1_PIDS: Dict[int, str] = {**V1_KEYBOARDS, **V1_MICE, **V1_PAGE_ONLY}
PID_K55_RGB_PRO_XT = 0x1BA1
PID_K65_MINI = 0x1BAF
# V2 detector list: pid -> (name, "hw" | "sw" controller flavour)
V2_PIDS: Dict[int, Tuple[str, str]] = {
    0x1BA4: ("Corsair K55 RGB PRO", "sw"),
    0x1B6E: ("Corsair K57 RGB (Wired)", "sw"),
    0x1BA0: ("Corsair K60 RGB PRO", "sw"),
    0x1B8D: ("Corsair K60 RGB PRO SE", "sw"),
    0x1BAD: ("Corsair K60 RGB PRO Low Profile", "sw"),
    0x1BC7: ("Corsair K60 RGB PRO TKL Black", "hw"),
    0x1BED: ("Corsair K60 RGB PRO TKL White", "hw"),
    0x1BFD: ("Corsair K70 Core RGB", "hw"),
    0x2B01: ("Corsair K70 Core RGB TKL", "hw"),
    0x1BC4: ("Corsair K70 RGB PRO", "hw"),
    0x1BB3: ("Corsair K70 RGB PRO V2", "hw"),
    0x1B73: ("Corsair K70 RGB TKL", "hw"),
    0x1BB9: ("Corsair K70 RGB TKL Champion Series", "hw"),
    0x1B89: ("Corsair K95 RGB PLATINUM XT", "hw"),
    0x1B7C: ("Corsair K100 RGB Optical", "hw"),
    0x1BC5: ("Corsair K100 RGB Optical", "hw"),
    0x1B7D: ("Corsair K100 MX Red", "hw"),
    0x1B4B: ("Corsair Dark Core RGB SE (Wired)", "hw"),
    0x1B7E: ("Corsair Dark Core RGB Pro SE (Wired)", "hw"),
    0x1B5E: ("Corsair Harpoon Wireless (Wired)", "sw"),
    0x1B4C: ("Corsair Ironclaw Wireless (Wired)", "sw"),
    0x2B32: ("Corsair Ironclaw Wireless SE (Wired)", "sw"),
    0x2B00: ("Corsair Ironclaw Wireless SE", "sw"),
    0x1B93: ("Corsair Katar Pro", "hw"),
    0x1BBA: ("Corsair Katar Pro V2", "hw"),
    0x1BAC: ("Corsair Katar Pro XT", "hw"),
    0x1B70: ("Corsair M55 RGB PRO", "sw"),
    0x1B9E: ("Corsair M65 RGB Ultra Wired", "sw"),
    0x1BB5: ("Corsair M65 RGB Ultra Wireless (Wired)", "hw"),
    0x1BF0: ("Corsair M75 Gaming Mouse", "hw"),
    0x1BDC: ("Corsair M75 Wireless", "hw"),
    0x1BF1: ("Corsair M75 Wireless (Wired)", "hw"),
    0x1BE3: ("Corsair Scimitar Elite RGB", "hw"),
    0x1BA6: ("Corsair Slipstream Wireless Receiver HW", "hw"),
    0x1B65: ("Corsair Slipstream Wireless Receiver SW", "sw"),
    0x1B66: ("Corsair Slipstream Wireless Receiver HW", "sw"),
    0x1B9B: ("Corsair MM700", "sw"),
    0x1BC9: ("Corsair MM700 3XL", "sw"),
}
V2_WIRELESS_RECEIVERS = {0x1BA6, 0x1B66, 0x1B65, 0x1BDC, 0x2B00}
V2_K57_WIRED = 0x1B6E
V2_K100 = {0x1B7C, 0x1BC5, 0x1B7D}

USB_IDS: List[Tuple[int, int]] = sorted({(VID, p) for p in (*V1_PIDS, PID_K55_RGB_PRO_XT, PID_K65_MINI, *V2_PIDS)})

# ---------------------------------------------------------------------------------------------
# Key names (RGBController/RGBControllerKeyNames.cpp spellings)
# ---------------------------------------------------------------------------------------------
K_ESC, K_F1, K_F2, K_F3, K_F4, K_F5, K_F6 = (
    "Key: Escape",
    "Key: F1",
    "Key: F2",
    "Key: F3",
    "Key: F4",
    "Key: F5",
    "Key: F6",
)
K_F7, K_F8, K_F9, K_F10, K_F11, K_F12 = "Key: F7", "Key: F8", "Key: F9", "Key: F10", "Key: F11", "Key: F12"
K_PRSC, K_SCLK, K_PSBK = "Key: Print Screen", "Key: Scroll Lock", "Key: Pause/Break"
K_BKTK, K_MINUS, K_EQ, K_BSPC = "Key: `", "Key: -", "Key: =", "Key: Backspace"
K_INS, K_HOME, K_PGUP, K_DEL, K_END, K_PGDN = (
    "Key: Insert",
    "Key: Home",
    "Key: Page Up",
    "Key: Delete",
    "Key: End",
    "Key: Page Down",
)
K_TAB, K_LBR, K_RBR, K_ANSI_BS, K_ISO_BS = "Key: Tab", "Key: [", "Key: ]", "Key: \\ (ANSI)", "Key: \\ (ISO)"
K_CAPS, K_SEMI, K_QUOTE, K_POUND, K_ENTER, K_ISO_ENTER = (
    "Key: Caps Lock",
    "Key: ;",
    "Key: '",
    "Key: #",
    "Key: Enter",
    "Key: Enter (ISO)",
)
K_LSHIFT, K_COMMA, K_PERIOD, K_SLASH, K_RSHIFT = "Key: Left Shift", "Key: ,", "Key: .", "Key: /", "Key: Right Shift"
K_LCTRL, K_LWIN, K_LALT, K_SPACE, K_RALT, K_RFN, K_RWIN, K_MENU, K_RCTRL = (
    "Key: Left Control",
    "Key: Left Windows",
    "Key: Left Alt",
    "Key: Space",
    "Key: Right Alt",
    "Key: Right Fn",
    "Key: Right Windows",
    "Key: Menu",
    "Key: Right Control",
)
K_UP, K_LEFT, K_DOWN, K_RIGHT = "Key: Up Arrow", "Key: Left Arrow", "Key: Down Arrow", "Key: Right Arrow"
K_NMLK, K_NMDV, K_NMTM, K_NMMI, K_NMPL, K_NMPD, K_NMER = (
    "Key: Num Lock",
    "Key: Number Pad /",
    "Key: Number Pad *",
    "Key: Number Pad -",
    "Key: Number Pad +",
    "Key: Number Pad .",
    "Key: Number Pad Enter",
)
K_NM = {i: f"Key: Number Pad {i}" for i in range(10)}
K_PLAY, K_PREV, K_NEXT, K_STOP, K_MUTE, K_VOLUP = (
    "Key: Media Play/Pause",
    "Key: Media Previous",
    "Key: Media Next",
    "Key: Media Stop",
    "Key: Media Mute",
    "Key: Media Volume +",
)
K_BRIGHT, K_WINLOCK, K_ISO_SLASH = "Key: Brightness", "Key: Windows Lock", "Key: / (ISO)"
UNUSED = ""


def _k(c: str) -> str:
    return "Key: " + c


# ---------------------------------------------------------------------------------------------
# Legacy (V1) protocol constants -- CorsairPeripheralController.h
# ---------------------------------------------------------------------------------------------
CMD_WRITE, CMD_READ, CMD_STREAM = 0x07, 0x0E, 0x7F
PROP_FIRMWARE_INFO, PROP_SPECIAL_FUNCTION, PROP_LIGHTING_CONTROL = 0x01, 0x04, 0x05
PROP_SUBMIT_MOUSE_COLOR, PROP_SUBMIT_KBZONES_COLOR_24, PROP_SUBMIT_KEYBOARD_COLOR_24 = 0x22, 0x25, 0x28
LIGHTING_HARDWARE, LIGHTING_SOFTWARE = 0x01, 0x02
LAYOUT_ANSI, LAYOUT_ISO, LAYOUT_ABNT, LAYOUT_JIS, LAYOUT_DUBEOLSIK = 0, 1, 2, 3, 4
TYPE_NORMAL, TYPE_K95_PLAT, TYPE_K95, TYPE_K55, TYPE_K70_MK2, TYPE_K68, TYPE_K70_LUX_RGB = range(7)
V1_MODE_DIRECT = 0xFF
V1_HW_COLOR_PULSE, V1_HW_COLOR_SHIFT, V1_HW_COLOR_WAVE, V1_HW_RAINBOW_WAVE = 0x01, 0x00, 0x04, 0x03
V1_HW_RAIN, V1_HW_SPIRAL, V1_HW_TYPE_KEY, V1_HW_TYPE_RIPPLE, V1_HW_VISOR = 0x06, 0x02, 0x08, 0x09, 0x05
V1_HW_SPEED_MIN, V1_HW_SPEED_MAX, V1_HW_BRIGHT_MIN, V1_HW_BRIGHT_MAX = 1, 3, 0, 3
V1_FW_INFO_MAX_ATTEMPTS = 10
V1_HW_MODE_PIDS = {0x1B49, 0x1B55}  # K70 RGB MK.2 and MK.2 Low Profile (Detect.cpp)
TYPE_KEYBOARD, TYPE_MOUSE, TYPE_MOUSEMAT, TYPE_HEADSET_STAND = "Keyboard", "Mouse", "Mousemat", "Headset Stand"

# Internal LED ids per LED index (CorsairPeripheralController.cpp keys[], keys_k70_mk2[], ...)
_V1_COMMON_IDS = [
    0x00,
    0x01,
    0x02,
    0x03,
    0x04,
    0x05,
    0x06,
    0x07,
    0x08,
    0x09,
    0x0C,
    0x0D,
    0x0E,
    0x0F,
    0x11,
    0x12,
    0x14,
    0x15,
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
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2A,
    0x2B,
    0x2C,
    0x30,
    0x31,
    0x32,
    0x33,
    0x34,
    0x35,
    0x36,
    0x37,
    0x38,
    0x39,
    0x3C,
    0x3D,
    0x3E,
    0x3F,
    0x40,
    0x42,
    0x43,
    0x44,
    0x45,
    0x48,
    73,
    74,
    75,
    76,
    78,
    79,
    80,
    81,
    84,
    85,
    86,
    87,
    88,
    89,
    90,
    91,
    92,
    93,
    96,
    97,
    98,
    99,
    100,
    101,
    102,
    103,
    104,
    105,
    108,
    109,
    110,
    111,
    112,
    113,
    115,
    116,
    117,
    120,
    121,
    122,
    123,
    124,
    126,
    127,
    128,
    129,
    132,
    133,
    134,
    135,
    136,
    137,
    139,
    140,
    141,
]
V1_IDS_NORMAL = _V1_COMMON_IDS + [0x10, 114]
V1_IDS_K70_MK2 = _V1_COMMON_IDS + [16, 114, 47, 59, 125]
V1_IDS_K95_PLAT = _V1_COMMON_IDS + [
    0x10,
    114,
    0x0A,
    0x16,
    0x22,
    0x2E,
    0x3A,
    0x46,
    125,
    144,
    145,
    146,
    158,
    160,
    147,
    148,
    149,
    150,
    151,
    152,
    153,
    154,
    155,
    159,
    162,
    161,
    156,
    157,
]
V1_IDS_K95 = _V1_COMMON_IDS + [
    0x10,
    114,
    0x0A,
    0x16,
    0x22,
    0x2E,
    0x3A,
    0x46,
    0x52,
    0x5E,
    0x6A,
    0x76,
    0x3B,
    0x47,
    0x53,
    0x5F,
    0x6B,
    0x77,
    0x83,
    0x8F,
    0x0B,
    0x17,
    0x23,
    0x2F,
]
V1_ST100_REMAP = [0x00, 0x01, 0x02, 0x03, 0x05, 0x06, 0x07, 0x08, 0x04]
V1_SKIP_K95_PLAT_ANSI = [0x31, 0x3F, 0x41, 0x42, 0x51, 0x53, 0x55, 0x6F, 0x7E, 0x7F, 0x80, 0x81]
V1_SKIP_K95_PLAT_ISO = [0x3F, 0x41, 0x42, 0x50, 0x53, 0x55, 0x6F, 0x78, 0x7E, 0x7F, 0x80, 0x81]
V1_SKIP_K70_MK2_ISO = [
    0x3F,
    0x41,
    0x42,
    0x50,
    0x53,
    0x55,
    0x6F,
    0x78,
    0x79,
    0x7A,
    0x7B,
    0x7C,
    0x7D,
    0x7E,
    0x7F,
    0x80,
    0x81,
]

# LED names in LED-index order (RGBController_CorsairPeripheral.cpp led_names*[])
_V1_COMMON_NAMES = [
    K_ESC,
    K_BKTK,
    K_TAB,
    K_CAPS,
    K_LSHIFT,
    K_LCTRL,
    K_F12,
    K_EQ,
    K_WINLOCK,
    K_NM[7],
    K_F1,
    _k("1"),
    _k("Q"),
    _k("A"),
    K_LWIN,
    K_PRSC,
    K_MUTE,
    K_NM[8],
    K_F2,
    _k("2"),
    _k("W"),
    _k("S"),
    _k("Z"),
    K_LALT,
    K_SCLK,
    K_BSPC,
    K_STOP,
    K_NM[9],
    K_F3,
    _k("3"),
    _k("E"),
    _k("D"),
    _k("X"),
    K_PSBK,
    K_DEL,
    K_PREV,
    K_F4,
    _k("4"),
    _k("R"),
    _k("F"),
    _k("C"),
    K_SPACE,
    K_INS,
    K_END,
    K_PLAY,
    K_NM[4],
    K_F5,
    _k("5"),
    _k("T"),
    _k("G"),
    _k("V"),
    K_HOME,
    K_PGDN,
    K_NEXT,
    K_NM[5],
    K_F6,
    _k("6"),
    _k("Y"),
    _k("H"),
    _k("B"),
    K_PGUP,
    K_RSHIFT,
    K_NMLK,
    K_NM[6],
    K_F7,
    _k("7"),
    _k("U"),
    _k("J"),
    _k("N"),
    K_RALT,
    K_RBR,
    K_RCTRL,
    K_NMDV,
    K_NM[1],
    K_F8,
    _k("8"),
    _k("I"),
    _k("K"),
    _k("M"),
    K_RWIN,
    K_ANSI_BS,
    K_UP,
    K_NMTM,
    K_NM[2],
    K_F9,
    _k("9"),
    _k("O"),
    _k("L"),
    K_COMMA,
    K_MENU,
    K_LEFT,
    K_NMMI,
    K_NM[3],
    K_F10,
    _k("0"),
    _k("P"),
    K_SEMI,
    K_PERIOD,
    K_ENTER,
    K_DOWN,
    K_NMPL,
    K_NM[0],
    K_F11,
    K_MINUS,
    K_LBR,
    K_QUOTE,
    K_SLASH,
    K_BRIGHT,
    K_RIGHT,
    K_NMER,
    K_NMPD,
]
V1_NAMES_NORMAL = _V1_COMMON_NAMES + [K_ISO_SLASH, K_ISO_BS]
V1_NAMES_K70_MK2 = _V1_COMMON_NAMES + [K_ISO_SLASH, K_ISO_BS, "Key: Logo Left", "Key: Logo Right", "Key: Profile"]
V1_NAMES_K95_PLAT = (
    _V1_COMMON_NAMES
    + [K_ISO_SLASH, K_ISO_BS]
    + [f"Key: Macro G{i}" for i in range(1, 7)]
    + ["Key: Preset"]
    + [f"Light Bar {i}" for i in range(1, 20)]
)
V1_NAMES_K95 = (
    _V1_COMMON_NAMES
    + [K_ISO_SLASH, K_ISO_BS]
    + [f"Key: Macro G{i}" for i in range(1, 19)]
    + ["Key: MR", "Key: M1", "Key: M2", "Key: M3"]
)
V1_NAMES_K55 = ["LEFT", "MIDDLE", "RIGHT"]
V1_MOUSE_NAMES: Dict[str, List[str]] = {  # corsair_m65_elite_leds / sabre / harpoon pro
    "Corsair M65 RGB Elite": ["", "", "Logo", "DPI", "Scroll Wheel"],
    "Corsair Ironclaw RGB": ["", "", "Logo", "DPI", "Scroll Wheel"],
    "Corsair Sabre RGB": ["", "Underglow", "Logo", "DPI", "Scroll Wheel"],
    "Corsair Harpoon RGB PRO": ["", "", "", "Logo"],
}

_M = [  # matrix_map[6][23]
    [0, NA, 10, 18, 28, 36, NA, 46, 55, 64, 74, NA, 84, 93, 102, 6, 15, 24, 33, 26, 35, 44, 53],
    [1, 11, 19, 29, 37, 47, 56, 65, 75, 85, 94, NA, 103, 7, 25, NA, 42, 51, 60, 62, 72, 82, 91],
    [2, NA, 12, 20, 30, 38, NA, 48, 57, 66, 76, 86, 95, 104, 70, 80, 34, 43, 52, 9, 17, 27, 100],
    [3, NA, 13, 21, 31, 39, NA, 49, 58, 67, 77, 87, 96, 105, 98, 112, NA, NA, NA, 45, 54, 63, NA],
    [4, 111, 22, 32, 40, 50, NA, 59, NA, 68, 78, 88, 97, 106, 61, NA, NA, 81, NA, 73, 83, 92, 109],
    [5, 14, 23, NA, NA, NA, NA, 41, NA, NA, NA, NA, 69, 79, 89, 71, 90, 99, 108, 101, NA, 110, NA],
]
V1_MATRIX_NORMAL = _M
V1_MATRIX_K70_LUX_RGB = [  # matrix_map_k70_lux_rgb[7][23]
    [NA] * 16 + [107, 8, NA, NA, 16, NA, NA],
    [0, NA, 10, 18, 28, 36, NA, 46, 55, 64, 74, NA, 84, 93, 102, 6, 15, 24, 33, 26, 35, 44, 53],
    [1, 11, 19, 29, 37, 47, NA, 56, 65, 75, 85, 94, 103, 7, 25, NA, 42, 51, 60, 62, 72, 82, 91],
    [2, NA, 12, 20, 30, 38, 48, 57, 66, 76, 86, NA, 95, 104, 70, 80, 34, 43, 52, 9, 17, 27, 100],
    [3, NA, 13, 21, 31, 39, 49, 58, 67, 77, 87, NA, 96, 105, 98, 112, NA, NA, NA, 45, 54, 63, NA],
    [4, 111, NA, 22, 32, 40, 50, 59, 68, 78, 88, 97, NA, 106, 61, NA, NA, 81, NA, 73, 83, 92, 109],
    [5, NA, 14, 23, NA, NA, NA, 41, NA, NA, NA, 69, NA, 79, 89, 71, 90, 99, 108, 101, NA, 110, NA],
]
V1_MATRIX_K70_MK2 = [
    [NA, NA, NA, 115, 107, 8, NA, NA, NA, NA, NA, 113, 114, NA, NA, NA, NA, NA, NA, 16, NA, NA, NA]
] + _M
V1_MATRIX_K95_PLAT = [[NA, NA, NA, 119, 107, 8] + [NA] * 14 + [16, NA, NA, NA]] + [
    [113 + i] + row for i, row in enumerate(_M)
]
V1_MATRIX_K95 = [[NA, NA, NA, 131, 132, 133, 134] + [NA] * 12 + [107, 8, NA, NA, 16, NA, NA]] + [
    [113 + 3 * i, 114 + 3 * i, 115 + 3 * i] + row for i, row in enumerate(_M)
]


@dataclass
class _V1Layout:
    ids: List[int]
    names: List[str]
    zones: List[Tuple[str, int, int, Optional[List[List[int]]]]]  # (name, type, count, matrix)
    data_sz: int = 24  # bytes in the third stream packet (48 for K95 / K95 Platinum)


V1_LAYOUTS: Dict[int, _V1Layout] = {
    TYPE_NORMAL: _V1Layout(V1_IDS_NORMAL, V1_NAMES_NORMAL, [("Keyboard", ZONE_MATRIX, 113, V1_MATRIX_NORMAL)]),
    TYPE_K68: _V1Layout(V1_IDS_NORMAL, V1_NAMES_NORMAL, [("Keyboard", ZONE_MATRIX, 113, V1_MATRIX_NORMAL)]),
    TYPE_K70_LUX_RGB: _V1Layout(
        V1_IDS_K70_MK2, V1_NAMES_K70_MK2, [("Keyboard", ZONE_MATRIX, 116, V1_MATRIX_K70_LUX_RGB)]
    ),
    TYPE_K70_MK2: _V1Layout(V1_IDS_K70_MK2, V1_NAMES_K70_MK2, [("Keyboard", ZONE_MATRIX, 116, V1_MATRIX_K70_MK2)]),
    TYPE_K95_PLAT: _V1Layout(
        V1_IDS_K95_PLAT,
        V1_NAMES_K95_PLAT,
        [("Keyboard", ZONE_MATRIX, 120, V1_MATRIX_K95_PLAT), ("Light Bar", ZONE_LINEAR, 19, None)],
        48,
    ),
    TYPE_K95: _V1Layout(V1_IDS_K95, V1_NAMES_K95, [("Keyboard", ZONE_MATRIX, 135, V1_MATRIX_K95)], 48),
    TYPE_K55: _V1Layout(
        [],
        V1_NAMES_K55,
        [("Left", ZONE_SINGLE, 1, None), ("Middle", ZONE_SINGLE, 1, None), ("Right", ZONE_SINGLE, 1, None)],
    ),
}


def _pkt(*head: int, size: int = PKT) -> bytearray:
    buf = bytearray(size)
    buf[: len(head)] = bytes(head)
    return buf


def _direct_mode() -> Mode:
    return Mode(0, "Direct", V1_MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)


def _v1_hw_mode(idx: int, name: str, value: int, extra_flags: int, color_mode: int, ncolors: int) -> Mode:
    flags = MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_AUTOMATIC_SAVE | extra_flags
    if ncolors:
        flags |= MODE_FLAG_HAS_RANDOM_COLOR | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR
    return Mode(
        idx,
        name,
        value,
        flags,
        V1_HW_SPEED_MIN,
        V1_HW_SPEED_MAX,
        V1_HW_BRIGHT_MIN,
        V1_HW_BRIGHT_MAX,
        ncolors,
        ncolors,
        V1_HW_SPEED_MIN,
        V1_HW_BRIGHT_MAX // 2,
        0,
        color_mode,
        [0] * ncolors,
    )


def _v1_hw_modes() -> List[Mode]:
    """RGBController_CorsairPeripheral.cpp: the hardware modes of the K70 RGB MK.2 family."""
    lr_ud = MODE_FLAG_HAS_DIRECTION_LR | MODE_FLAG_HAS_DIRECTION_UD
    return [
        _v1_hw_mode(1, "ColorPulse", V1_HW_COLOR_PULSE, 0, MODE_COLORS_RANDOM, 2),
        _v1_hw_mode(2, "ColorShift", V1_HW_COLOR_SHIFT, 0, MODE_COLORS_RANDOM, 2),
        _v1_hw_mode(3, "ColorWave", V1_HW_COLOR_WAVE, lr_ud, MODE_COLORS_RANDOM, 2),
        _v1_hw_mode(4, "RainbowWave", V1_HW_RAINBOW_WAVE, lr_ud, MODE_COLORS_NONE, 0),
        _v1_hw_mode(5, "Rain", V1_HW_RAIN, 0, MODE_COLORS_RANDOM, 2),
        _v1_hw_mode(6, "Spiral", V1_HW_SPIRAL, MODE_FLAG_HAS_DIRECTION_LR, MODE_COLORS_NONE, 0),
        _v1_hw_mode(7, "TypeKey", V1_HW_TYPE_KEY, 0, MODE_COLORS_RANDOM, 2),
        _v1_hw_mode(8, "TypeRipple", V1_HW_TYPE_RIPPLE, 0, MODE_COLORS_RANDOM, 2),
        _v1_hw_mode(9, "Visor", V1_HW_VISOR, 0, MODE_COLORS_RANDOM, 2),
    ]


def _build_device(
    type_name: str,
    name: str,
    desc: str,
    version: str,
    path: str,
    modes: List[Mode],
    zone_specs: Sequence[Tuple[str, int, int, Optional[List[List[int]]]]],
    names: List[str],
) -> Device:
    zones, start = [], 0
    for i, (zname, ztype, count, matrix) in enumerate(zone_specs):
        zones.append(Zone(i, zname, ztype, count, count, count, matrix, start))
        start += count
    names = list(names[:start]) + [f"LED {i + 1}" for i in range(len(names), start)]
    return Device(0, type_name, name, "Corsair", desc, version, "", "HID: " + path, 0, modes, zones, names, [0] * start)


class CorsairLegacy:
    """CorsairPeripheralController.cpp: K55/K65/K68/K70/K95/Strafe keyboards, RGB mice, MM800, ST100."""

    def __init__(self, path: str, pid: int, name: str, hid: Optional[HidDevice] = None):
        self.hid = hid or HidDevice(path)
        self.usb_pid = pid
        self.name = name
        self.type = ""
        self.logical_layout = TYPE_NORMAL
        self.physical_layout = LAYOUT_ANSI
        self.version = ""
        try:
            self._read_firmware_info()
            if not self.type:
                raise DeviceError(f"{name}: unknown Corsair device type (no firmware-info answer)")
            # constructor sequence of CorsairPeripheralController()
            self._enter_software_mode()
        except DeviceError:
            self.hid.close()
            raise
        self.layout = V1_LAYOUTS[self.logical_layout] if self.type == TYPE_KEYBOARD else None
        self.hw_modes = self.type == TYPE_KEYBOARD and pid in V1_HW_MODE_PIDS
        self.device = self._build(path)

    # ---- init / mode switching ------------------------------------------------------------
    def _needs_k55_k95_setup(self) -> bool:
        return self.type == TYPE_KEYBOARD and self.logical_layout in (TYPE_K55, TYPE_K95_PLAT, TYPE_K70_MK2, TYPE_K68)

    def _enter_software_mode(self):
        """SwitchMode(true) / constructor tail: special function, lighting control, K55/K95 key map."""
        if self._needs_k55_k95_setup():
            self._special_function_control()
        self._lighting_control()
        if self._needs_k55_k95_setup():
            self._setup_k55_k95_lighting_control()

    def _special_function_control(self):
        self.hid.write(bytes(_pkt(0x00, CMD_WRITE, PROP_SPECIAL_FUNCTION, LIGHTING_SOFTWARE)))

    def _lighting_control(self):
        buf = _pkt(0x00, CMD_WRITE, PROP_LIGHTING_CONTROL, LIGHTING_SOFTWARE)
        # byte 5: 3 for keyboards and the headset stand, 1 for mice, 4 for mousemats
        buf[5] = {TYPE_MOUSE: 0x01, TYPE_MOUSEMAT: 0x04}.get(self.type, 0x03)
        self.hid.write(bytes(buf))

    def _setup_k55_k95_lighting_control(self):
        """SetupK55AndK95LightingControl(): 'probably a key mapping packet' -- 4 x 30 (id, 0xC0) pairs."""
        self.hid.write(bytes(_pkt(0x00, CMD_WRITE, PROP_LIGHTING_CONTROL, 0x08, 0x00, 0x01)))
        skipped = V1_SKIP_K95_PLAT_ANSI
        if self.physical_layout == LAYOUT_ISO:
            skipped = (
                V1_SKIP_K70_MK2_ISO if self.logical_layout in (TYPE_K70_MK2, TYPE_K70_LUX_RGB) else V1_SKIP_K95_PLAT_ISO
            )
        identifier = 0
        for _ in range(4):
            buf = _pkt(0x00, CMD_WRITE, 0x40, 0x1E)
            for j in range(30):
                for s in skipped:  # same single-pass skip as the C++ loop
                    if identifier == s:
                        identifier += 1
                buf[5 + 2 * j] = identifier & 0xFF
                buf[5 + 2 * j + 1] = 0xC0
                identifier += 1
            self.hid.write(bytes(buf))

    def _read_firmware_info(self):
        """ReadFirmwareInfo(): 0x0E 0x01 query, retried up to 10 x 1 s; feature-report fallback."""
        buf, offset = bytes(PKT), 0
        for attempt in range(V1_FW_INFO_MAX_ATTEMPTS):
            query = bytes(_pkt(0x00, CMD_READ, PROP_FIRMWARE_INFO))
            self.hid.write(query)
            reply = self.hid.read(PKT, 1.0)
            offset = 0
            if not reply:
                self.hid.send_feature(query)
                reply = self.hid.get_feature(0, PKT)
                offset = 1
            buf = bytes(reply) + bytes(PKT)
            dev_type = buf[0x14 + offset]
            log.debug(
                "%s firmware info attempt %d/%d: type %02X", self.name, attempt + 1, V1_FW_INFO_MAX_ATTEMPTS, dev_type
            )
            if dev_type in (0xC0, 0xC1, 0xC2):
                break
        dev_type = buf[0x14 + offset]
        if dev_type == 0xC0:
            pid = (buf[0x0E] << 8) | buf[0x0F]
            self.logical_layout = {
                0x1B2D: TYPE_K95_PLAT,
                0x1B11: TYPE_K95,
                0x1B3D: TYPE_K55,
                0x1B33: TYPE_K70_LUX_RGB,
                0x1B38: TYPE_K70_MK2,
                0x1B49: TYPE_K70_MK2,
                0x1B6B: TYPE_K70_MK2,
                0x1B55: TYPE_K70_MK2,
                0x1B48: TYPE_K70_MK2,
                0x1B4F: TYPE_K68,
            }.get(pid, TYPE_NORMAL)
            layout = buf[0x17 + offset]
            self.physical_layout = (
                layout
                if layout in (LAYOUT_ANSI, LAYOUT_ISO, LAYOUT_ABNT, LAYOUT_JIS, LAYOUT_DUBEOLSIK)
                else LAYOUT_ANSI
            )
            self.type = TYPE_KEYBOARD
        elif dev_type == 0xC1:
            self.type = TYPE_MOUSE
            self._special_function_control()
        elif dev_type == 0xC2:
            pid = (buf[0x0F] << 8) | buf[0x0E]
            self.type = TYPE_HEADSET_STAND if pid == 0x0A34 else TYPE_MOUSEMAT
            self._special_function_control()
        else:
            return
        self.version = f"{buf[0x09 + offset]}.{buf[0x08 + offset]}"

    # ---- model ------------------------------------------------------------------------------
    def _build(self, path: str) -> Device:
        modes = [_direct_mode()] + (_v1_hw_modes() if self.hw_modes else [])
        if self.type == TYPE_KEYBOARD:
            zones, names = self.layout.zones, self.layout.names
        elif self.type == TYPE_MOUSE:
            zones = [("Mouse Zone", ZONE_SINGLE, 15, None)]
            special = V1_MOUSE_NAMES.get(self.name, [])
            names = [(special[i] if i < len(special) and special[i] else f"Mouse LED {i + 1}") for i in range(15)]
        elif self.type == TYPE_MOUSEMAT:
            zones = [("Mousemat Zone", ZONE_LINEAR, 15, None)]
            names = [f"Mousemat LED {i + 1}" for i in range(15)]
        else:
            zones = [("Base LED Strip", ZONE_LINEAR, 8, None), ("Logo", ZONE_SINGLE, 1, None)]
            names = [f"Mousemat LED {i + 1}" for i in range(9)]
        return _build_device(
            self.type,
            self.name,
            "Corsair RGB Peripheral Device (direct hidraw)",
            self.version,
            path,
            modes,
            zones,
            names,
        )

    # ---- colors -----------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        colors = list(colors)[: len(self.device.colors)]
        if self.type == TYPE_KEYBOARD:
            if self.logical_layout == TYPE_K55:
                self._submit_keyboard_zones(colors + [0] * (3 - len(colors)))
            else:
                self._set_keyboard_full(colors)
        elif self.type == TYPE_MOUSE:
            self._submit_mouse(colors)
        elif self.type == TYPE_MOUSEMAT:
            self._submit_mousemat(colors)
        else:
            # ST100: the logo sits in the middle of the base strip, remap so it is last (and use the mat protocol)
            remap = [0] * 9
            for i, c in enumerate(colors[:9]):
                remap[V1_ST100_REMAP[i]] = c
            self._submit_mousemat(remap)
        self.device.colors[: len(colors)] = colors

    def _set_keyboard_full(self, colors: List[int]):
        """SetLEDsKeyboardFull(): one 168-byte plane per channel streamed as 60+60+data_sz, then 0x28 submit."""
        planes = [bytearray(168) for _ in range(3)]
        ids = self.layout.ids
        for idx, color in enumerate(colors):
            if idx >= len(ids):
                break
            r, g, b = u32_to_rgb(color)
            planes[0][ids[idx]], planes[1][ids[idx]], planes[2][ids[idx]] = r, g, b
        data_sz = self.layout.data_sz
        for channel, plane in enumerate(planes, start=1):
            self._stream(1, 60, plane[0:60])
            self._stream(2, 60, plane[60:120])
            self._stream(3, data_sz, plane[120 : 120 + data_sz])
            self._submit_keyboard_full(channel, 3, 2 if channel == 3 else 1)

    def _stream(self, packet_id: int, data_sz: int, data: bytes):
        buf = _pkt(0x00, CMD_STREAM, packet_id, data_sz)
        buf[5 : 5 + data_sz] = bytes(data[:data_sz]).ljust(data_sz, b"\0")
        self.hid.write(bytes(buf))

    def _submit_keyboard_full(self, channel: int, packet_count: int, finish: int):
        self.hid.write(bytes(_pkt(0x00, CMD_WRITE, PROP_SUBMIT_KEYBOARD_COLOR_24, channel, packet_count, finish)))

    def _submit_keyboard_zones(self, colors: List[int]):
        buf = _pkt(0x00, CMD_WRITE, PROP_SUBMIT_KBZONES_COLOR_24, 0x00, 0x00)
        for i in range(3):
            buf[5 + 3 * i : 8 + 3 * i] = bytes(u32_to_rgb(colors[i]))
        self.hid.write(bytes(buf))

    def _submit_mouse(self, colors: List[int]):
        buf = _pkt(0x00, CMD_WRITE, PROP_SUBMIT_MOUSE_COLOR, len(colors), 0x00)
        for i, c in enumerate(colors):
            buf[5 + 4 * i] = i
            buf[6 + 4 * i : 9 + 4 * i] = bytes(u32_to_rgb(c))
        self.hid.write(bytes(buf))

    def _submit_mousemat(self, colors: List[int]):
        buf = _pkt(0x00, CMD_WRITE, PROP_SUBMIT_MOUSE_COLOR, len(colors), 0x00)
        for i, c in enumerate(colors):
            buf[5 + 3 * i : 8 + 3 * i] = bytes(u32_to_rgb(c))
        self.hid.write(bytes(buf))

    # ---- modes ------------------------------------------------------------------------------
    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == V1_MODE_DIRECT:
            self._enter_software_mode()
            return
        direction = mode.direction
        if mode.flags & (MODE_FLAG_HAS_DIRECTION_LR | MODE_FLAG_HAS_DIRECTION_UD):
            direction += 1
            if mode.value == V1_HW_SPIRAL:
                direction += 4
        self._set_hardware_mode(mode, direction)
        self.hid.write(bytes(_pkt(0x00, CMD_WRITE, PROP_SPECIAL_FUNCTION, LIGHTING_HARDWARE)))  # SwitchMode(false)

    def _set_hardware_mode(self, mode: Mode, direction: int):
        """SetHardwareMode(): brightness, 'lght_00.d' profile name, 0x7F mode stream, 0x17 0x09 / 0x08 commit."""
        buf = _pkt(0x00, CMD_WRITE, 0x05, 0x02)
        buf[5] = mode.brightness & 0xFF
        self.hid.write(bytes(buf))
        buf = _pkt(0x00, CMD_WRITE, 0x17, 0x05)
        buf[5:14] = b"lght_00.d"
        self.hid.write(bytes(buf))
        buf = _pkt(0x00, CMD_STREAM, 0x01, 0x0D)
        buf[5] = mode.value & 0xFF
        buf[8] = direction & 0xFF
        if mode.value == V1_HW_TYPE_KEY:
            buf[9] = mode.speed & 0xFF
        else:
            buf[6] = mode.speed & 0xFF
        if mode.color_mode == MODE_COLORS_RANDOM:
            buf[7] = 0x01
        elif mode.color_mode == MODE_COLORS_MODE_SPECIFIC:
            buf[7] = 0x03
            cols = (list(mode.colors) + [0, 0])[:2]
            buf[10:13] = bytes(u32_to_rgb(cols[0]))
            buf[13] = 0xFF
            buf[14:17] = bytes(u32_to_rgb(cols[1]))
            buf[17] = 0xFF
        self.hid.write(bytes(buf))
        self.hid.write(bytes(_pkt(0x00, CMD_WRITE, 0x17, 0x09)))
        self.hid.write(bytes(_pkt(0x00, CMD_WRITE, 0x17, 0x08)))

    def close(self):
        try:
            # hand lighting back to the onboard firmware (the packet OpenRGB's SwitchMode(false) sends)
            self.hid.write(bytes(_pkt(0x00, CMD_WRITE, PROP_SPECIAL_FUNCTION, LIGHTING_HARDWARE)))
        except DeviceError:
            pass
        self.hid.close()


# ---------------------------------------------------------------------------------------------
# K55 RGB PRO XT -- CorsairK55RGBPROXTController.cpp / RGBController_CorsairK55RGBPROXT.cpp
# ---------------------------------------------------------------------------------------------
XT_COLOR_BANK = 137
XT_MODE_DIRECT, XT_STATIC, XT_COLOR_PULSE, XT_COLOR_SHIFT, XT_COLOR_WAVE = 0xFFFF, 0x207E, 0xAD4F, 0xA5FA, 0x7BFF
XT_RAINBOW_WAVE, XT_RAIN, XT_SPIRAL, XT_WATER_COLOR, XT_TYPE_KEY, XT_TYPE_RIPPLE, XT_VISOR = (
    0xB94C,
    0xA07E,
    0xAB87,
    0x0022,
    0xB1F9,
    0x09A2,
    0x90C0,
)
XT_COLOR_PREDEF, XT_COLOR_RANDOM, XT_COLOR_UNKNOWN = 0x01, 0x02, 0x03
XT_SPEED_MIN, XT_SPEED_MED, XT_SPEED_MAX = 0x03, 0x04, 0x05
XT_DIR_NONE, XT_DIR_DOWN, XT_DIR_UP, XT_DIR_RIGHT, XT_DIR_LEFT, XT_DIR_CW, XT_DIR_CCW = 0, 1, 2, 4, 5, 6, 7
XT_KEEPALIVE_S = 50.0
XT_IDS = [
    127,
    128,
    129,
    130,
    131,
    132,
    37,
    49,
    39,
    53,
    102,
    101,
    26,
    96,
    104,
    54,
    27,
    16,
    0,
    25,
    103,
    55,
    28,
    22,
    18,
    23,
    56,
    29,
    4,
    3,
    2,
    57,
    30,
    17,
    5,
    21,
    31,
    58,
    32,
    19,
    6,
    1,
    40,
    59,
    33,
    24,
    7,
    60,
    34,
    20,
    9,
    13,
    61,
    35,
    8,
    10,
    12,
    14,
    11,
    50,
    62,
    41,
    15,
    47,
    51,
    107,
    63,
    42,
    43,
    48,
    52,
    118,
    64,
    38,
    44,
    46,
    106,
    97,
    65,
    36,
    105,
    66,
    69,
    72,
    76,
    67,
    70,
    73,
    78,
    77,
    68,
    71,
    74,
    75,
    79,
    91,
    88,
    85,
    94,
    80,
    92,
    89,
    86,
    81,
    93,
    90,
    87,
    95,
    82,
    83,
    84,
]
XT_FILLER = bytes(
    [
        0x70,
        0x6E,
        0x4E,
        0x4D,
        0x4C,
        0x65,
        0x6F,
        0x2C,
        0x4B,
        0x4A,
        0x49,
        0x48,
        0x47,
        0x46,
        0x45,
        0x05,
        0x19,
        0x06,
        0x1B,
        0x1D,
        0x64,
        0x6A,
        0x34,
        0x6B,
        0x6C,
        0x69,
        0x38,
        0x37,
        0x36,
        0x10,
        0x11,
        0x16,
        0x04,
        0x39,
        0x2F,
        0x13,
        0x12,
        0x0C,
        0x18,
        0x33,
        0x0F,
        0x0E,
        0x0D,
        0x0B,
        0x0A,
        0x09,
        0x07,
        0x27,
        0x7A,
        0x26,
        0x25,
        0x24,
        0x23,
        0x22,
        0x21,
        0x20,
        0x1C,
        0x17,
        0x15,
        0x08,
        0x1A,
        0x14,
        0x2B,
        0x2D,
        0x3F,
        0x3E,
        0x3D,
        0x3C,
        0x3B,
        0x3A,
        0x29,
        0x1F,
        0x1E,
        0x35,
        0x44,
        0x43,
        0x42,
        0x41,
        0x40,
        0x62,
        0x00,
        0x5B,
        0x5A,
        0x59,
        0x5E,
        0x5D,
        0x5C,
        0x88,
        0x87,
        0x86,
        0x85,
        0x84,
        0x83,
        0x63,
        0x53,
        0x4F,
        0x61,
        0x60,
        0x5F,
        0x58,
        0x57,
        0x56,
        0x55,
        0x54,
        0x2A,
        0x2E,
        0x28,
        0x32,
        0x30,
        0x51,
        0x50,
        0x52,
        0x6D,
    ]
)
XT_NAMES = [
    "Key: G1",
    "Key: G2",
    "Key: G3",
    "Key: G4",
    "Key: G5",
    "Key: G6",
    K_ESC,
    K_BKTK,
    K_TAB,
    K_CAPS,
    K_LSHIFT,
    K_LCTRL,
    _k("1"),
    K_ISO_BS,
    K_LWIN,
    K_F1,
    _k("2"),
    _k("Q"),
    _k("A"),
    _k("Z"),
    K_LALT,
    K_F2,
    _k("3"),
    _k("W"),
    _k("S"),
    _k("X"),
    K_F3,
    _k("4"),
    _k("E"),
    _k("D"),
    _k("C"),
    K_F4,
    _k("5"),
    _k("R"),
    _k("F"),
    _k("V"),
    _k("6"),
    K_F5,
    _k("7"),
    _k("T"),
    _k("G"),
    _k("B"),
    K_SPACE,
    K_F6,
    _k("8"),
    _k("Y"),
    _k("H"),
    K_F7,
    _k("9"),
    _k("U"),
    _k("J"),
    _k("N"),
    K_F8,
    _k("0"),
    _k("I"),
    _k("K"),
    _k("M"),
    _k("O"),
    _k("L"),
    K_COMMA,
    K_F9,
    K_MINUS,
    _k("P"),
    K_SEMI,
    K_PERIOD,
    K_RALT,
    K_F10,
    K_EQ,
    K_LBR,
    K_QUOTE,
    K_SLASH,
    K_RFN,
    K_F11,
    K_BSPC,
    K_RBR,
    K_POUND,
    K_RSHIFT,
    K_MENU,
    K_F12,
    K_ENTER,
    K_RCTRL,
    K_PRSC,
    K_INS,
    K_DEL,
    K_LEFT,
    K_SCLK,
    K_HOME,
    K_END,
    K_UP,
    K_DOWN,
    K_PSBK,
    K_PGUP,
    K_PGDN,
    K_RIGHT,
    K_NMLK,
    K_NM[7],
    K_NM[4],
    K_NM[1],
    K_NM[0],
    K_NMDV,
    K_NM[8],
    K_NM[5],
    K_NM[2],
    K_NMTM,
    K_NM[9],
    K_NM[6],
    K_NM[3],
    K_NMPD,
    K_NMMI,
    K_NMPL,
    K_NMER,
]
XT_MATRIX = [
    [0, 6, NA, 15, 21, 26, 31, NA, 37, 43, 47, 52, NA, 60, 66, 72, 78, 81, 85, 90, NA, NA, NA, NA],
    [1, 7, 12, 16, 22, 27, 32, 36, 38, 44, 48, 53, NA, 61, 67, 73, NA, 82, 86, 91, 94, 99, 103, 108],
    [2, 8, NA, 17, 23, 28, 33, NA, 39, 45, 49, 54, 57, 62, 68, 74, 79, 83, 87, 92, 95, 100, 104, 109],
    [3, 9, NA, 18, 24, 29, 34, NA, 40, 46, 50, 55, 58, 63, 69, 75, NA, NA, NA, NA, 96, 101, 105, NA],
    [4, 10, 13, 19, 25, 30, 35, NA, 41, NA, 51, 56, 59, 64, 70, 76, NA, NA, 88, NA, 97, 102, 106, 110],
    [5, 11, 14, 20, NA, NA, NA, NA, 42, NA, NA, NA, NA, 65, 71, 77, 80, 84, 89, 93, 98, NA, 107, NA],
]


def _xt_mode(idx: int, name: str, value: int, flags: int, color_mode: int, ncolors: int, direction: int = 0) -> Mode:
    speedy = bool(flags & MODE_FLAG_HAS_SPEED)
    return Mode(
        idx,
        name,
        value,
        flags,
        XT_SPEED_MIN if speedy else 0,
        XT_SPEED_MAX if speedy else 0,
        0,
        0,
        ncolors,
        ncolors,
        XT_SPEED_MED if speedy else 0,
        0,
        direction,
        color_mode,
        [0] * ncolors,
    )


def _xt_modes() -> List[Mode]:
    S, LR, UD = MODE_FLAG_HAS_SPEED, MODE_FLAG_HAS_DIRECTION_LR, MODE_FLAG_HAS_DIRECTION_UD
    RC = MODE_FLAG_HAS_RANDOM_COLOR | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR
    water = _xt_mode(8, "WaterColor", XT_WATER_COLOR, S, MODE_COLORS_NONE, 0)
    water.colors = [0x00FFFFFF]
    return [
        Mode(0, "Direct", XT_MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
        _xt_mode(1, "Static", XT_STATIC, MODE_FLAG_HAS_MODE_SPECIFIC_COLOR, MODE_COLORS_MODE_SPECIFIC, 1),
        _xt_mode(2, "ColorPulse", XT_COLOR_PULSE, S | RC, MODE_COLORS_RANDOM, 2),
        _xt_mode(3, "ColorShift", XT_COLOR_SHIFT, S | RC, MODE_COLORS_RANDOM, 2),
        _xt_mode(4, "ColorWave", XT_COLOR_WAVE, S | RC | LR | UD, MODE_COLORS_RANDOM, 2, 1),
        _xt_mode(5, "RainbowWave", XT_RAINBOW_WAVE, S | LR | UD, MODE_COLORS_NONE, 0, 1),
        _xt_mode(6, "Rain", XT_RAIN, S | RC, MODE_COLORS_RANDOM, 2, 3),
        _xt_mode(7, "Spiral", XT_SPIRAL, S | LR, MODE_COLORS_NONE, 0, 1),
        water,
        _xt_mode(9, "TypeKey", XT_TYPE_KEY, S | RC, MODE_COLORS_RANDOM, 2),
        _xt_mode(10, "TypeRipple", XT_TYPE_RIPPLE, S | RC, MODE_COLORS_RANDOM, 2),
        _xt_mode(11, "Visor", XT_VISOR, S | RC, MODE_COLORS_RANDOM, 2, 1),
    ]


class CorsairK55ProXT:
    def __init__(self, path: str, pid: int, name: str, hid: Optional[HidDevice] = None):
        self.hid = hid or HidDevice(path)
        self.bank = [bytearray(XT_COLOR_BANK) for _ in range(3)]
        self.last_update = time.monotonic()
        try:
            self._lighting_control()
        except DeviceError:
            self.hid.close()
            raise
        self.device = _build_device(
            TYPE_KEYBOARD,
            name,
            "Corsair K55 RGB PRO XT Keyboard Device (direct hidraw)",
            "",
            path,
            _xt_modes(),
            [("Keyboard", ZONE_MATRIX, len(XT_NAMES), XT_MATRIX)],
            XT_NAMES,
        )

    def _lighting_control(self):
        self.hid.write(bytes(_pkt(0x00, 0x08, 0x01, 0x03, 0x00, 0x02)))
        self.hid.write(bytes(_pkt(0x00, 0x08, 0x02, 0x5F)))
        self.hid.write(bytes(_pkt(0x00, 0x08, 0x0D, 0x00, 0x01)))

    def set_colors(self, colors: List[int]):
        colors = list(colors)[: len(XT_IDS)]
        for idx, c in enumerate(colors):
            r, g, b = u32_to_rgb(c)
            self.bank[0][XT_IDS[idx]], self.bank[1][XT_IDS[idx]], self.bank[2][XT_IDS[idx]] = r, g, b
        stream = bytes(self.bank[0] + self.bank[1] + self.bank[2]) + bytes(64)
        buf = _pkt(0x00, 0x08, 0x06, 0x00, 0x9B, 0x01)
        buf[12:PKT] = stream[: PKT - 12]
        self.hid.write(bytes(buf))
        pos = PKT - 12
        buf[2] = 0x07
        for _ in range(6):
            buf[4:PKT] = stream[pos : pos + PKT - 4]
            pos += PKT - 4
            self.hid.write(bytes(buf))
        self.device.colors[: len(colors)] = colors
        self.last_update = time.monotonic()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == XT_MODE_DIRECT:
            self._lighting_control()
            return
        self._set_hardware_mode(mode)
        self.hid.write(bytes(_pkt(0x00, 0x00, 0x01, 0x03, 0x00, 0x01)))  # SwitchMode(false)

    def _set_hardware_mode(self, mode: Mode):
        self._lighting_control()
        self.hid.write(bytes(_pkt(0x00, 0x08, 0x0D, 0x01, 0x61, 0x6D)))
        self.hid.write(bytes(_pkt(0x00, 0x08, 0x09, 0x01)))
        buf = _pkt(0x00, 0x08, 0x06, 0x01, 0x78)
        buf[8], buf[9] = mode.value & 0xFF, (mode.value >> 8) & 0xFF
        if mode.color_mode != MODE_COLORS_NONE:
            buf[0x0A] = XT_COLOR_RANDOM if mode.color_mode == MODE_COLORS_RANDOM else XT_COLOR_PREDEF
        if mode.value == XT_WATER_COLOR:
            buf[0x0A] = XT_COLOR_UNKNOWN
        buf[0x0B] = mode.speed & 0xFF
        if mode.value in (XT_COLOR_WAVE, XT_RAINBOW_WAVE, XT_RAIN, XT_SPIRAL, XT_VISOR):
            spiral = mode.value == XT_SPIRAL
            buf[0x0C] = {
                0: XT_DIR_CCW if spiral else XT_DIR_LEFT,
                1: XT_DIR_CW if spiral else XT_DIR_RIGHT,
                2: XT_DIR_UP,
                3: XT_DIR_DOWN,
            }.get(mode.direction, XT_DIR_NONE)
        fill_dst, fill_src = 0x0F, 0
        if buf[0x0A] != XT_COLOR_RANDOM:
            buf[0x0E] = len(mode.colors) & 0xFF
            for i, c in enumerate(mode.colors):
                r, g, b = u32_to_rgb(c)
                buf[0x0F + 4 * i : 0x13 + 4 * i] = bytes([0xFF, b, g, r])
                buf[4] = (buf[4] + 4) & 0xFF
                fill_dst += 4
        n = PKT - fill_dst
        buf[fill_dst:PKT] = XT_FILLER[fill_src : fill_src + n].ljust(n, b"\0")
        fill_src += n
        self.hid.write(bytes(buf))
        buf = _pkt(0x00, 0x08, 0x07, 0x01)
        buf[4:PKT] = XT_FILLER[fill_src : fill_src + PKT - 4].ljust(PKT - 4, b"\0")
        fill_src += PKT - 4
        self.hid.write(bytes(buf))
        buf = _pkt(0x00, 0x08, 0x07, 0x01)
        tail = XT_FILLER[fill_src:]
        buf[4 : 4 + len(tail)] = tail
        self.hid.write(bytes(buf))
        self.hid.write(bytes(_pkt(0x00, 0x08, 0x05, 0x01, 0x01)))

    def keepalive(self):
        """The PRO XT falls back to its rainbow after ~1 min without traffic in software mode."""
        if self.device.active_mode == 0 and time.monotonic() - self.last_update > XT_KEEPALIVE_S:
            self.set_colors(self.device.colors)

    def close(self):
        try:
            self.hid.write(bytes(_pkt(0x00, 0x00, 0x01, 0x03, 0x00, 0x01)))
        except DeviceError:
            pass
        self.hid.close()


# ---------------------------------------------------------------------------------------------
# K65 Mini -- CorsairK65MiniController.cpp / RGBController_CorsairK65Mini.cpp
# ---------------------------------------------------------------------------------------------
MINI_PKT = 1025
MINI_WRITE = 0x08
MINI_KEEPALIVE_S = 50.0
MINI_MATRIX = [
    [0, 5, 8, 13, 17, 22, 26, 31, 35, 40, 44, 49, 54, NA, 60],
    [1, NA, 9, 14, 18, 23, 27, 32, 36, 41, 45, 50, 55, 58, NA],
    [2, NA, 10, 15, 19, 24, 28, 33, 37, 42, 46, 51, 56, 59, 61],
    [3, 6, 11, 16, 20, 25, 29, 34, 38, 43, 47, 52, NA, NA, 62],
    [4, 7, 12, NA, 21, NA, 30, NA, 39, NA, 48, 53, 57, NA, 63],
]
# (led id, name) indexed by the matrix entries above; OpenRGB leaves the two extra space-bar LEDs unnamed
MINI_KEYS: List[Tuple[int, str]] = [
    (41, K_ESC),
    (43, K_TAB),
    (57, K_CAPS),
    (106, K_LSHIFT),
    (105, K_LCTRL),
    (30, _k("1")),
    (100, K_ISO_BS),
    (108, K_LWIN),
    (31, _k("2")),
    (20, _k("Q")),
    (4, _k("A")),
    (29, _k("Z")),
    (107, K_LALT),
    (32, _k("3")),
    (26, _k("W")),
    (22, _k("S")),
    (27, _k("X")),
    (33, _k("4")),
    (8, _k("E")),
    (7, _k("D")),
    (6, _k("C")),
    (0, "Space Bar LED Left"),
    (34, _k("5")),
    (21, _k("R")),
    (9, _k("F")),
    (25, _k("V")),
    (35, _k("6")),
    (23, _k("T")),
    (10, _k("G")),
    (5, _k("B")),
    (44, K_SPACE),
    (36, _k("7")),
    (28, _k("Y")),
    (11, _k("H")),
    (17, _k("N")),
    (37, _k("8")),
    (24, _k("U")),
    (13, _k("J")),
    (16, _k("M")),
    (1, "Space Bar LED Right"),
    (38, _k("9")),
    (12, _k("I")),
    (14, _k("K")),
    (54, K_COMMA),
    (39, _k("0")),
    (18, _k("O")),
    (15, _k("L")),
    (55, K_PERIOD),
    (111, K_RALT),
    (45, K_MINUS),
    (19, _k("P")),
    (51, K_SEMI),
    (56, K_SLASH),
    (122, K_RFN),
    (46, K_EQ),
    (47, K_LBR),
    (52, K_QUOTE),
    (101, K_MENU),
    (48, K_RBR),
    (50, K_POUND),
    (42, K_BSPC),
    (40, K_ISO_ENTER),
    (110, K_RSHIFT),
    (109, K_RCTRL),
]


def _mini_layout() -> Tuple[List[str], List[int], List[List[int]]]:
    """RGBController_CorsairK65Mini::SetupZones(): walk the matrix column by column, LED index = visit order."""
    names, positions, remap = [], [], {}
    for w in range(len(MINI_MATRIX[0])):
        for h in range(len(MINI_MATRIX)):
            key = MINI_MATRIX[h][w]
            if key != NA:
                remap[key] = len(names)
                positions.append(MINI_KEYS[key][0])
                names.append(MINI_KEYS[key][1])
    matrix = [[NA if v == NA else remap[v] for v in row] for row in MINI_MATRIX]
    return names, positions, matrix


class CorsairK65Mini:
    def __init__(self, path: str, pid: int, name: str, hid: Optional[HidDevice] = None):
        self.hid = hid or HidDevice(path)
        self.last_update = time.monotonic()
        names, self.positions, matrix = _mini_layout()
        try:
            self._lighting_control()
        except DeviceError:
            self.hid.close()
            raise
        self.device = _build_device(
            TYPE_KEYBOARD,
            name,
            "Corsair K65 Mini Keyboard Device (direct hidraw)",
            "",
            path,
            [Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)],
            [("Keyboard", ZONE_MATRIX, len(names), matrix)],
            names,
        )

    def _lighting_control(self):
        self.hid.write(bytes(_pkt(0x00, MINI_WRITE, 0x01, 0x03, 0x00, 0x02, size=MINI_PKT)))
        self.hid.write(bytes(_pkt(0x00, MINI_WRITE, 0x02, 0x6E, size=MINI_PKT)))
        self.hid.write(bytes(_pkt(0x00, MINI_WRITE, 0x0D, 0x01, 0x22, size=MINI_PKT)))

    def set_colors(self, colors: List[int]):
        colors = list(colors)[: len(self.positions)]
        buf = _pkt(0x00, MINI_WRITE, 0x06, 0x01, 0x73, 0x01, 0x00, 0x00, 0x12, size=MINI_PKT)
        for i, c in enumerate(colors):
            pos = self.positions[i]
            buf[0x0A + pos * 3 : 0x0D + pos * 3] = bytes(u32_to_rgb(c))
        self.hid.write(bytes(buf))
        self.device.colors[: len(colors)] = colors
        self.last_update = time.monotonic()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index  # direct only; DeviceUpdateMode() is empty upstream

    def keepalive(self):
        if time.monotonic() - self.last_update > MINI_KEEPALIVE_S:
            self.set_colors(self.device.colors)

    def close(self):
        self.hid.close()


# ---------------------------------------------------------------------------------------------
# V2 protocol -- CorsairPeripheralV2Controller.cpp + KeyboardLayoutManager.cpp
# ---------------------------------------------------------------------------------------------
V2_WRITE_WIRED, V2_WRITE_WIRELESS = 0x08, 0x09
V2_CMD_SET, V2_CMD_GET, V2_CMD_STOP_TX, V2_CMD_BLK_W1, V2_CMD_BLK_WN, V2_CMD_START_TX = (
    0x01,
    0x02,
    0x05,
    0x06,
    0x07,
    0x0D,
)
V2_VALUE_MODE = 0x03
V2_MODE_HW, V2_MODE_SW = 0x01, 0x02
V2_LIGHT_CTRL1, V2_LIGHT_CTRL2 = 1, 0x22
V2_MODE_DIRECT = 0x0012
V2_TIMEOUT, V2_TIMEOUT_SHORT, V2_WRITE_PACE = 0.050, 0.003, 0.002
V2_PID_RETRIES, V2_PID_RETRY_DELAY = 8, 0.120
V2_UPDATE_PERIOD_S = 30.0
V2_HW_DATA_OFFSET = 2
V2_KB_ANSI, V2_KB_ISO, V2_KB_ABNT, V2_KB_JIS = 1, 2, 3, 4

# KeyboardLayoutManager opcodes and base maps ------------------------------------------------
(
    OP_INSERT_SHIFT_RIGHT,
    OP_SWAP_ONLY,
    OP_REMOVE_SHIFT_LEFT,
    OP_INS_SHFT_ADJACENT,
    OP_INSERT_ROW,
    OP_REMOVE_ROW,
    OP_ADD_ALT_NAME,
) = range(7)
SIZE_MAIN, SIZE_FN_ROW, SIZE_EXTRA, SIZE_NUMPAD = 1, 2, 4, 8
SIZE_FULL = SIZE_MAIN | SIZE_FN_ROW | SIZE_EXTRA | SIZE_NUMPAD
SIZE_TKL = SIZE_MAIN | SIZE_FN_ROW | SIZE_EXTRA
Edit = Tuple[int, int, int, str, int]  # (row, col, value, name, opcode)

_ZONE_MAIN: List[Tuple[int, int, str]] = (
    [(1, c, n) for c, n in enumerate([K_BKTK] + [_k(str(d)) for d in "1234567890"] + [K_MINUS, K_EQ, K_BSPC])]
    + [(2, c, n) for c, n in enumerate([K_TAB] + [_k(ch) for ch in "QWERTYUIOP"] + [K_LBR, K_RBR, K_ANSI_BS])]
    + [(3, c, n) for c, n in enumerate([K_CAPS] + [_k(ch) for ch in "ASDFGHJKL"] + [K_SEMI, K_QUOTE, K_POUND, K_ENTER])]
    + [
        (4, c, n)
        for c, n in enumerate([K_LSHIFT, K_ISO_BS] + [_k(ch) for ch in "ZXCVBNM"] + [K_COMMA, K_PERIOD, K_SLASH])
    ]
    + [(4, 13, K_RSHIFT)]
    + [
        (5, 0, K_LCTRL),
        (5, 1, K_LWIN),
        (5, 2, K_LALT),
        (5, 6, K_SPACE),
        (5, 10, K_RALT),
        (5, 11, K_RFN),
        (5, 12, K_MENU),
        (5, 13, K_RCTRL),
    ]
)
_ZONE_FN_ROW = [(0, 0, K_ESC)] + [(0, 2 + i, _k(f"F{i + 1}")) for i in range(12)]
_ZONE_EXTRAS = [
    (0, 14, K_PRSC),
    (0, 15, K_SCLK),
    (0, 16, K_PSBK),
    (1, 14, K_INS),
    (1, 15, K_HOME),
    (1, 16, K_PGUP),
    (2, 14, K_DEL),
    (2, 15, K_END),
    (2, 16, K_PGDN),
    (4, 15, K_UP),
    (5, 14, K_LEFT),
    (5, 15, K_DOWN),
    (5, 16, K_RIGHT),
]
_ZONE_NUMPAD = [
    (1, 17, K_NMLK),
    (1, 18, K_NMDV),
    (1, 19, K_NMTM),
    (1, 20, K_NMMI),
    (2, 17, K_NM[7]),
    (2, 18, K_NM[8]),
    (2, 19, K_NM[9]),
    (2, 20, K_NMPL),
    (3, 17, K_NM[4]),
    (3, 18, K_NM[5]),
    (3, 19, K_NM[6]),
    (4, 17, K_NM[1]),
    (4, 18, K_NM[2]),
    (4, 19, K_NM[3]),
    (4, 20, K_NMER),
    (5, 18, K_NM[0]),
    (5, 19, K_NMPD),
]
# regional overlays (only the ones the Corsair V2 code can select): ANSI removes # and ISO \, ISO removes ANSI \
_REGIONAL: Dict[int, List[Edit]] = {
    V2_KB_ANSI: [(3, 12, 0, UNUSED, OP_SWAP_ONLY), (4, 1, 0, UNUSED, OP_SWAP_ONLY)],
    V2_KB_ISO: [(2, 13, 0, UNUSED, OP_SWAP_ONLY)],
    V2_KB_JIS: [],  # jis overlay only adds alternate names
}

# Corsair key values, in base-map (row, column) order -- CorsairPeripheralV2Devices.cpp
V2_TKL_VALUES = [
    41,
    58,
    59,
    60,
    61,
    62,
    63,
    64,
    65,
    66,
    67,
    68,
    69,
    70,
    71,
    72,
    53,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    37,
    38,
    39,
    45,
    46,
    42,
    73,
    74,
    75,
    43,
    20,
    26,
    8,
    21,
    23,
    28,
    24,
    12,
    18,
    19,
    47,
    48,
    49,
    76,
    77,
    78,
    57,
    4,
    22,
    7,
    9,
    10,
    11,
    13,
    14,
    15,
    51,
    52,
    50,
    40,
    106,
    100,
    29,
    27,
    6,
    25,
    5,
    17,
    16,
    54,
    55,
    56,
    110,
    82,
    105,
    108,
    107,
    44,
    111,
    122,
    101,
    109,
    80,
    81,
    79,
]
V2_FULL_VALUES = [
    41,
    58,
    59,
    60,
    61,
    62,
    63,
    64,
    65,
    66,
    67,
    68,
    69,
    70,
    71,
    72,
    53,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    37,
    38,
    39,
    45,
    46,
    42,
    73,
    74,
    75,
    83,
    84,
    85,
    86,
    43,
    20,
    26,
    8,
    21,
    23,
    28,
    24,
    12,
    18,
    19,
    47,
    48,
    49,
    76,
    77,
    78,
    95,
    96,
    97,
    87,
    57,
    4,
    22,
    7,
    9,
    10,
    11,
    13,
    14,
    15,
    51,
    52,
    50,
    40,
    92,
    93,
    94,
    106,
    100,
    29,
    27,
    6,
    25,
    5,
    17,
    16,
    54,
    55,
    56,
    110,
    82,
    89,
    90,
    91,
    88,
    105,
    108,
    107,
    44,
    111,
    122,
    101,
    109,
    80,
    81,
    79,
    98,
    99,
]


def _g_keys(prefix: str = "") -> List[Edit]:
    return [(r, 0, 130 + r, f"{prefix}G{r}", OP_INSERT_SHIFT_RIGHT) for r in range(1, 7)]


_MEDIA_INS = [
    (0, 17, 123, K_STOP, OP_INSERT_SHIFT_RIGHT),
    (0, 18, 126, K_PREV, OP_INSERT_SHIFT_RIGHT),
    (0, 19, 124, K_PLAY, OP_INSERT_SHIFT_RIGHT),
    (0, 20, 125, K_NEXT, OP_INSERT_SHIFT_RIGHT),
]
V2_LAYOUT_K57: Tuple[int, List[int], List[Edit]] = (
    SIZE_FULL,
    V2_FULL_VALUES,
    [
        (0, 17, 0, "Power/Wireless Indicator", OP_INSERT_ROW),
        (0, 18, 1, "Lock/Macro Indicator", OP_INSERT_SHIFT_RIGHT),
        *_g_keys("Key: "),
    ],
)
V2_LAYOUT_K60 = (SIZE_FULL, V2_FULL_VALUES, [])
V2_LAYOUT_K60_TKL = (SIZE_TKL, V2_TKL_VALUES, [])
V2_LAYOUT_K70 = (SIZE_FULL, V2_FULL_VALUES, [])
V2_LAYOUT_K70_PRO = (
    SIZE_FULL,
    V2_FULL_VALUES,
    [
        (0, 0, 128, "Profile", OP_INSERT_ROW),
        (0, 1, 113, "Light", OP_INSERT_SHIFT_RIGHT),
        (0, 2, 114, "Lock", OP_INSERT_SHIFT_RIGHT),
        (0, 10, 138, "Logo", OP_INSERT_SHIFT_RIGHT),
        (0, 18, 102, K_MUTE, OP_INSERT_SHIFT_RIGHT),
        (1, 17, 123, K_STOP, OP_INSERT_SHIFT_RIGHT),
        (1, 18, 126, K_PREV, OP_INSERT_SHIFT_RIGHT),
        (1, 19, 124, K_PLAY, OP_INSERT_SHIFT_RIGHT),
        (1, 20, 125, K_NEXT, OP_INSERT_SHIFT_RIGHT),
        (6, 5, 140, K_SPACE, OP_INSERT_SHIFT_RIGHT),
        (6, 7, 141, K_SPACE, OP_INSERT_SHIFT_RIGHT),
    ],
)
V2_LAYOUT_K70_TKL_CS = (
    SIZE_TKL,
    V2_TKL_VALUES,
    [
        (0, 1, 123, K_STOP, OP_INSERT_ROW),
        (0, 2, 126, K_PREV, OP_INSERT_SHIFT_RIGHT),
        (0, 3, 124, K_PLAY, OP_INSERT_SHIFT_RIGHT),
        (0, 4, 125, K_NEXT, OP_INSERT_SHIFT_RIGHT),
        (0, 7, 1, "Logo L", OP_INSERT_SHIFT_RIGHT),
        (0, 8, 3, "Logo R", OP_INSERT_SHIFT_RIGHT),
        (0, 11, 128, "Profile", OP_INSERT_SHIFT_RIGHT),
        (0, 12, 113, "Light", OP_INSERT_SHIFT_RIGHT),
        (0, 13, 114, "Lock", OP_INSERT_SHIFT_RIGHT),
        (0, 14, 102, K_MUTE, OP_INSERT_SHIFT_RIGHT),
    ],
)
V2_LAYOUT_K70_CORE_TKL = (
    SIZE_TKL,
    V2_TKL_VALUES,
    [(0, 14, 70, K_MUTE, OP_SWAP_ONLY), (0, 15, 71, UNUSED, OP_SWAP_ONLY), (0, 16, 72, K_VOLUP, OP_SWAP_ONLY)],
)
V2_LAYOUT_K95 = (
    SIZE_FULL,
    V2_FULL_VALUES,
    [
        (5, 11, 112, K_RWIN, OP_SWAP_ONLY),
        *_MEDIA_INS,
        (0, 4, 128, "Profile", OP_INSERT_ROW),
        (0, 5, 113, "Light", OP_INSERT_SHIFT_RIGHT),
        (0, 6, 114, "Lock", OP_INSERT_SHIFT_RIGHT),
        (0, 18, 102, K_MUTE, OP_INSERT_SHIFT_RIGHT),
        *_g_keys(),
        (0, 0, 137, "Top Bar 1", OP_INSERT_ROW),
        *[(0, c, 137 + c, f"Top Bar {c + 1}", OP_INSERT_SHIFT_RIGHT) for c in range(1, 19)],
    ],
)
V2_LAYOUT_K100 = (
    SIZE_FULL,
    V2_FULL_VALUES,
    [
        *_MEDIA_INS,
        (0, 1, 128, "Profile", OP_INSERT_ROW),
        (0, 2, 137, "iCue", OP_INSERT_SHIFT_RIGHT),
        (0, 3, 114, "Lock", OP_INSERT_SHIFT_RIGHT),
        (0, 10, 190, "Logo L", OP_INSERT_SHIFT_RIGHT),
        (0, 11, 191, "Logo M", OP_INSERT_SHIFT_RIGHT),
        (0, 12, 192, "Logo R", OP_INSERT_SHIFT_RIGHT),
        (0, 18, 102, K_MUTE, OP_INSERT_SHIFT_RIGHT),
        *_g_keys(),
        (0, 0, 138, "Underglow 1", OP_INSERT_ROW),
        *[(0, c, 138 + c, f"Underglow {c + 1}", OP_INSERT_SHIFT_RIGHT) for c in range(1, 22)],
        *[(r, 0, 160 + r, f"Underglow {23 + r}", OP_INSERT_SHIFT_RIGHT) for r in range(11)],
        *[(r, 23, 171 + r, f"Underglow {34 + r}", OP_INSERT_SHIFT_RIGHT) for r in range(11)],
        (0, 0, 182, "Wheel 1", OP_INSERT_ROW),
        *[(0, c, 182 + c, f"Wheel {c + 1}", OP_INSERT_SHIFT_RIGHT) for c in range(1, 8)],
    ],
)


@dataclass
class V2Zone:
    name: str
    type: int
    rows: int
    cols: int


@dataclass
class V2Dev:
    name: str
    type_name: str
    rows: int
    cols: int
    zones: List[V2Zone]
    layout: Optional[Tuple[int, List[int], List[Edit]]] = None


def _single(name: str) -> V2Zone:
    return V2Zone(name, ZONE_SINGLE, 1, 1)


def _linear(name: str, n: int) -> V2Zone:
    return V2Zone(name, ZONE_LINEAR, 1, n)


def _kb(name: str, rows: int, cols: int, layout) -> V2Dev:
    return V2Dev(name, TYPE_KEYBOARD, rows, cols, [V2Zone("Keyboard", ZONE_MATRIX, rows, cols)], layout)


_DARK_CORE_ZONES = [
    _single("Scroll Wheel"),
    _linear("Side Buttons", 4),
    _single("Rear Left"),
    _single("Logo"),
    _single("Rear Right"),
    _linear("DPI & Indicator Zone", 4),
]
_IRONCLAW_ZONES = [_single("Logo"), _single("Scroll Wheel"), _single("Buttons"), _linear("Side Zone", 3)]
_KATAR_ZONES = [_single("Scroll Wheel"), _single("DPI")]
_MM700_ZONES = [_single("Left"), _single("Right"), _single("Logo")]
V2_DEVICES: Dict[int, V2Dev] = {  # CorsairPeripheralV2Devices.cpp, keyed by the pid the device *reports*
    0x1BA4: V2Dev("Corsair K55 RGB PRO", TYPE_KEYBOARD, 1, 6, [_linear("Keyboard", 6)]),
    0x1B6E: _kb("Corsair K57 RGB (Wired)", 7, 22, V2_LAYOUT_K57),
    0x1BA0: _kb("Corsair K60 RGB PRO", 6, 21, V2_LAYOUT_K60),
    0x1B8D: _kb("Corsair K60 RGB PRO SE", 6, 21, V2_LAYOUT_K60),
    0x1BAD: _kb("Corsair K60 RGB PRO Low Profile", 6, 21, V2_LAYOUT_K60),
    0x1BC7: _kb("Corsair K60 RGB PRO TKL Black", 6, 21, V2_LAYOUT_K60_TKL),
    0x1BED: _kb("Corsair K60 RGB PRO TKL White", 6, 21, V2_LAYOUT_K60_TKL),
    0x1BFD: _kb("Corsair K70 Core RGB", 6, 21, V2_LAYOUT_K70),
    0x2B01: _kb("Corsair K70 Core RGB TKL", 6, 17, V2_LAYOUT_K70_CORE_TKL),
    0x1BC4: _kb("Corsair K70 RGB PRO", 7, 21, V2_LAYOUT_K70_PRO),
    0x1BB3: _kb("Corsair K70 RGB PRO V2", 7, 21, V2_LAYOUT_K70_PRO),
    0x1B73: _kb("Corsair K70 RGB TKL", 7, 17, V2_LAYOUT_K70_TKL_CS),
    0x1BB9: _kb("Corsair K70 RGB TKL Champion Series", 7, 17, V2_LAYOUT_K70_TKL_CS),
    0x1B89: _kb("Corsair K95 RGB PLATINUM XT", 8, 22, V2_LAYOUT_K95),
    0x1B7D: _kb("Corsair K100 MX Red", 12, 24, V2_LAYOUT_K100),
    0x1B7C: _kb("Corsair K100 RGB Optical", 12, 24, V2_LAYOUT_K100),
    0x1BC5: _kb("Corsair K100 RGB Optical", 12, 24, V2_LAYOUT_K100),
    0x1B4B: V2Dev("Corsair Dark Core RGB SE", TYPE_MOUSE, 1, 12, _DARK_CORE_ZONES),
    0x1B7E: V2Dev("Corsair Dark Core RGB Pro SE", TYPE_MOUSE, 1, 12, _DARK_CORE_ZONES),
    0x1B5E: V2Dev("Corsair Harpoon Wireless", TYPE_MOUSE, 1, 2, [_single("Indicator"), _single("Logo")]),
    0x1B4C: V2Dev("Corsair Ironclaw Wireless", TYPE_MOUSE, 1, 6, _IRONCLAW_ZONES),
    0x2B32: V2Dev("Corsair Ironclaw Wireless SE", TYPE_MOUSE, 1, 6, _IRONCLAW_ZONES),
    0x1B93: V2Dev("Corsair Katar Pro", TYPE_MOUSE, 1, 2, _KATAR_ZONES),
    0x1BBA: V2Dev("Corsair Katar Pro V2", TYPE_MOUSE, 1, 2, _KATAR_ZONES),
    0x1BAC: V2Dev("Corsair Katar Pro XT", TYPE_MOUSE, 1, 2, _KATAR_ZONES),
    0x1B70: V2Dev("Corsair M55 RGB PRO", TYPE_MOUSE, 1, 2, [_single("Middle Button"), _single("Logo")]),
    0x1B9E: V2Dev(
        "Corsair M65 RGB Ultra Wired",
        TYPE_MOUSE,
        1,
        3,
        [_single("Logo"), _single("Scroll Wheel"), _single("Indicator")],
    ),
    0x1BB5: V2Dev("Corsair M65 RGB Ultra Wireless", TYPE_MOUSE, 1, 2, [_single("Logo"), _single("DPI")]),
    0x1BF0: V2Dev("Corsair M75 Gaming Mouse", TYPE_MOUSE, 1, 2, [_single("Logo"), _single("Scroll Wheel")]),
    0x1BF1: V2Dev("Corsair M75 Wireless", TYPE_MOUSE, 1, 2, [_single("Logo"), _single("Bottom")]),
    0x1BE3: V2Dev(
        "Corsair Scimitar Elite RGB",
        TYPE_MOUSE,
        1,
        5,
        [
            _single("Logo"),
            _single("Scroll Wheel"),
            _single("Front Right"),
            _single("Side Buttons"),
            _single("Front Left"),
        ],
    ),
    0x1B9B: V2Dev("Corsair MM700", TYPE_MOUSEMAT, 1, 3, _MM700_ZONES),
    0x1BC9: V2Dev("Corsair MM700 3XL", TYPE_MOUSEMAT, 1, 3, _MM700_ZONES),
}


class KeyboardLayout:
    """Port of OpenRGB's KeyboardLayoutManager: a sorted (row, col) key list with insert/swap/remove edits."""

    def __init__(self, layout: int, size: int, values: Sequence[int]):
        self.keys: List[List] = []  # [row, col, value, name]
        self.rows = self.cols = 0
        for flag, block in (
            (SIZE_MAIN, _ZONE_MAIN),
            (SIZE_FN_ROW, _ZONE_FN_ROW),
            (SIZE_EXTRA, _ZONE_EXTRAS),
            (SIZE_NUMPAD, _ZONE_NUMPAD),
        ):
            if size & flag:
                for row, col, name in block:
                    self.insert_key(row, col, 0, name)
                self.update_dimensions()
        for i, v in enumerate(values[: len(self.keys)]):
            self.keys[i][2] = v
        self.apply(_REGIONAL.get(layout, _REGIONAL[V2_KB_ANSI]))

    def update_dimensions(self):
        self.rows = max((k[0] for k in self.keys), default=-1) + 1
        self.cols = max((k[1] for k in self.keys), default=-1) + 1

    def apply(self, edits: Sequence[Edit]):
        for row, col, value, name, op in edits:
            if op == OP_INSERT_SHIFT_RIGHT:
                self.insert_key(row, col, value, name)
            elif op == OP_SWAP_ONLY:
                self.swap_key(row, col, value, name)
            elif op == OP_REMOVE_SHIFT_LEFT:
                self.remove_key(row, col)
            elif op == OP_INSERT_ROW:
                if self.insert_row(row):
                    self.swap_key(row, col, value, name)
            elif op == OP_REMOVE_ROW:
                self.remove_row(row)
            # OP_ADD_ALT_NAME / OP_INS_SHFT_ADJACENT change nothing we use
        self.update_dimensions()

    def insert_key(self, row: int, col: int, value: int, name: str):
        idx = 0
        while idx < len(self.keys) and not (
            row < self.keys[idx][0] or (row == self.keys[idx][0] and col <= self.keys[idx][1])
        ):
            idx += 1
        update_row = True
        if idx == len(self.keys):
            self.keys.append([row, col, value, name])
            update_row = False
        elif name != UNUSED:
            self.keys.insert(idx, [row, col, value, name])
            idx += 1
        if update_row:
            for k in self.keys[idx:]:
                if k[0] == row and k[1] >= col:
                    k[1] += 1
                if k[0] > row:
                    break

    def swap_key(self, row: int, col: int, value: int, name: str):
        if not self.keys:
            self.keys.append([row, col, value, name])
            return
        for idx, k in enumerate(self.keys):
            if k[0] == row and k[1] == col:
                if name == UNUSED:
                    del self.keys[idx]
                else:
                    k[2], k[3] = value, name
                break
            if k[0] > row or (k[0] == row and k[1] > col):
                if name != UNUSED:  # upstream inserts at idx-1 here; kept for identical LED order
                    self.keys.insert(0 if idx == 0 else idx - 1, [row, col, value, name])
                break

    def remove_key(self, row: int, col: int):
        for idx, k in enumerate(self.keys):
            if k[0] == row and k[1] == col:
                del self.keys[idx]
                for kk in self.keys[idx:]:
                    if kk[0] != row:
                        break
                    kk[1] -= 1
                break
            if k[0] == row and col < k[1]:
                for kk in self.keys[idx:]:
                    if kk[0] != row:
                        break
                    kk[1] -= 1
                break

    def insert_row(self, row: int) -> bool:
        if row >= self.rows:
            return False
        idx = 0
        while idx < len(self.keys) and self.keys[idx][0] < row:
            idx += 1
        if idx < len(self.keys) and row <= self.keys[idx][0]:
            for k in self.keys[idx:]:
                k[0] += 1
        return True

    def remove_row(self, row: int):
        if row >= self.rows:
            return
        self.keys = [k for k in self.keys if k[0] != row]
        for k in self.keys:
            if k[0] > row:
                k[0] -= 1

    def matrix(self, height: int, width: int) -> List[List[int]]:
        """GetKeyMap(KEYBOARD_MAP_FILL_TYPE_COUNT, height, width): cell = LED index."""
        m = [[NA] * width for _ in range(height)]
        for i, (row, col, _v, _n) in enumerate(self.keys):
            if row < height and col < width:
                m[row][col] = i
        return m


class CorsairV2:
    """CorsairPeripheralV2Controller + V2HW/V2SW controllers: block-write direct mode."""

    def __init__(self, path: str, pid: int, name: str, hid: Optional[HidDevice] = None, flavour: str = "hw"):
        self.hid = hid or HidDevice(path)
        self.usb_pid = pid
        self.name = name
        self.flavour = flavour
        self.write_cmd = V2_WRITE_WIRED
        self.light_ctrl = V2_LIGHT_CTRL2
        self.skip_reads = False
        self.last_update = time.monotonic()
        try:
            self.dev = self._identify()
            self._lighting_endpoint()
            self.hw_layout = self._get_address(0x41) if any(z.type == ZONE_MATRIX for z in self.dev.zones) else 0
            # CorsairPeripheralV2HWController / SWController constructors
            self._set_render_mode(V2_MODE_SW)
            self._lighting_control(0x5F)
        except DeviceError:
            self.hid.close()
            raise
        self.device, self.values = self._build(path)

    # ---- transport ---------------------------------------------------------------------------
    def _xfer(self, cmd: int, a: int = 0, b: int = 0, read: bool = True, timeout: float = V2_TIMEOUT) -> bytes:
        buf = _pkt(0x00, self.write_cmd, cmd, a, b)
        self.hid.write(bytes(buf))
        if read and not self.skip_reads:
            r = self.hid.read(PKT, timeout)
            return bytes(r or b"") + bytes(PKT)
        return bytes(PKT)

    def _get_address(self, address: int) -> int:
        r = self._xfer(V2_CMD_GET, address)
        if r[2] > 0:
            log.debug("%s: GetAddress %02X failed (%d)", self.name, address, r[2])
            return NA
        return int.from_bytes(r[3:7], "little")

    def _get_address_retry(self, address: int) -> int:
        value = NA
        for attempt in range(V2_PID_RETRIES):
            value = self._get_address(address)
            if value not in (NA, 0):
                return value
            if attempt + 1 < V2_PID_RETRIES:
                time.sleep(V2_PID_RETRY_DELAY)
        return value

    def _identify(self) -> V2Dev:
        pid = self._get_address_retry(0x12)
        if pid in V2_WIRELESS_RECEIVERS:
            self.write_cmd = V2_WRITE_WIRELESS
            pid = self._get_address_retry(0x12)
        elif pid == V2_K57_WIRED:
            self.write_cmd, self.light_ctrl, self.skip_reads = 0x80, V2_LIGHT_CTRL1, True
        self.hid.write(bytes(_pkt(0x00, self.write_cmd, V2_CMD_GET, 0x11)))  # VID query, sets packet capabilities
        self.hid.read(1024, V2_TIMEOUT)
        dev = V2_DEVICES.get(pid & 0xFFFF)
        if dev is None:
            raise DeviceError(f"{self.name}: reported PID {pid:04X} is not in the Corsair V2 device list")
        self.reported_pid = pid & 0xFFFF
        return dev

    def _lighting_endpoint(self):
        """Endpoint 0x22 unless the device rejects a 0x0D start on it (K100 is pinned to 0x22)."""
        if self.light_ctrl != V2_LIGHT_CTRL2 or self.reported_pid in V2_K100:
            return
        if self._start_transaction(0) > 0:
            self.light_ctrl = V2_LIGHT_CTRL1
            self._start_transaction(0)
        self._stop_transaction(0)

    def _set_render_mode(self, mode: int):
        buf = _pkt(0x00, self.write_cmd, V2_CMD_SET, V2_VALUE_MODE, 0x00, mode)
        self.hid.write(bytes(buf))
        if not self.skip_reads:
            self.hid.read(PKT, V2_TIMEOUT)

    def _lighting_control(self, opt1: int):
        self._xfer(V2_CMD_GET, opt1)

    def _start_transaction(self, opt1: int) -> int:
        return self._xfer(V2_CMD_START_TX, opt1, self.light_ctrl)[2]

    def _stop_transaction(self, opt1: int):
        self._xfer(V2_CMD_STOP_TX, 0x01, opt1)

    def _send_block(self, data: bytes):
        """SetLEDs(): 0x06 header packet (57 data bytes) then 0x07 continuation packets (61 bytes), 2 ms pacing."""
        if not self.skip_reads:
            self.hid.drain()
        self._start_transaction(0)
        size = len(data)
        buf = _pkt(0x00, self.write_cmd, V2_CMD_BLK_W1, 0x00, size & 0xFF, (size >> 8) & 0xFF)
        first = min(PKT - 8, size)
        buf[8 : 8 + first] = data[:first]
        self.hid.write(bytes(buf))
        if not self.skip_reads:
            if not self.hid.read(PKT, V2_TIMEOUT_SHORT):
                time.sleep(V2_WRITE_PACE)
        pos = first
        while pos < size:
            chunk = data[pos : pos + PKT - 4]
            buf = _pkt(0x00, self.write_cmd, V2_CMD_BLK_WN, 0x00)
            buf[4 : 4 + len(chunk)] = chunk
            self.hid.write(bytes(buf))
            time.sleep(V2_WRITE_PACE)
            pos += len(chunk)
        self._stop_transaction(0)

    # ---- model ------------------------------------------------------------------------------
    def _build(self, path: str) -> Tuple[Device, List[int]]:
        zones, names, values, start = [], [], [], 0
        max_value = 0
        for zi, z in enumerate(self.dev.zones):
            if z.type == ZONE_MATRIX and self.dev.layout is not None:
                size, defaults, edits = self.dev.layout
                layout = {V2_KB_ISO: V2_KB_ISO, V2_KB_JIS: V2_KB_JIS}.get(self.hw_layout, V2_KB_ANSI)
                kb = KeyboardLayout(layout, size, defaults)
                kb.apply(edits)
                matrix = kb.matrix(z.rows, z.cols)
                for _r, _c, v, n in kb.keys:
                    names.append(n)
                    values.append(v)
                    max_value = max(max_value, v)
                count = len(kb.keys)
                max_value += 1
                zones.append(Zone(zi, z.name, ZONE_MATRIX, count, count, count, matrix, start))
            else:
                count = z.rows * z.cols
                for i in range(count):
                    names.append(f"{z.name} {i}")
                    values.append(len(values))
                max_value = max(max_value, len(values))
                zones.append(Zone(zi, z.name, z.type, count, count, count, None, start))
            start += count
        self.buffer_len = max_value
        dev = Device(
            0,
            self.dev.type_name,
            self.dev.name,
            "Corsair",
            f"Corsair Peripheral V2 {self.flavour.upper()} Device (direct hidraw)",
            "",
            "",
            "HID: " + path,
            0,
            [Mode(0, "Direct", V2_MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)],
            zones,
            names,
            [0] * start,
        )
        return dev, values

    # ---- colors -----------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        colors = list(colors)[: len(self.values)]
        ordered = [0] * self.buffer_len
        for i, c in enumerate(colors):
            if self.values[i] < self.buffer_len:
                ordered[self.values[i]] = c
        if self.flavour == "hw" and self.light_ctrl == V2_LIGHT_CTRL2:
            # SetLedsDirectTriplets(): 0x12 0x00 header then RGB triplets
            data = bytearray([V2_MODE_DIRECT & 0xFF, V2_MODE_DIRECT >> 8])
            for c in ordered:
                data += bytes(u32_to_rgb(c))
        else:
            # SetLedsDirectColourBlocks(): all reds, all greens, all blues
            rgb = [u32_to_rgb(c) for c in ordered]
            data = bytearray(bytes(p[0] for p in rgb) + bytes(p[1] for p in rgb) + bytes(p[2] for p in rgb))
        self._send_block(bytes(data))
        self.device.colors[: len(colors)] = colors
        self.last_update = time.monotonic()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == V2_MODE_DIRECT:
            self._set_render_mode(V2_MODE_SW)

    def keepalive(self):
        if self.device.active_mode == 0 and time.monotonic() - self.last_update > V2_UPDATE_PERIOD_S:
            self.set_colors(self.device.colors)

    def close(self):
        try:
            self._set_render_mode(V2_MODE_HW)  # ~CorsairPeripheralV2Controller(): give the firmware back control
        except DeviceError:
            pass
        self.hid.close()


# ---------------------------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------------------------
def _has_usage_page(info: HidInfo, page: int) -> bool:
    if not info.descriptor:
        return True
    return bytes([0x06, page & 0xFF, (page >> 8) & 0xFF]) in info.descriptor


def _iface_ok(info: HidInfo, iface: int) -> bool:
    return info.interface in (-1, iface)


def probe() -> list:
    found = []
    for info in enumerate_hid(VID):
        pid = info.pid
        try:
            if pid in V1_PAGE_ONLY:
                if _has_usage_page(info, 0xFFC2):
                    found.append(CorsairLegacy(info.path, pid, V1_PIDS[pid]))
            elif pid in V1_PIDS:
                if _iface_ok(info, 1) and _has_usage_page(info, 0xFFC2):
                    found.append(CorsairLegacy(info.path, pid, V1_PIDS[pid]))
            elif pid == PID_K55_RGB_PRO_XT:
                if _iface_ok(info, 1) and _has_usage_page(info, 0xFF42):
                    found.append(CorsairK55ProXT(info.path, pid, "Corsair K55 RGB PRO XT"))
            elif pid == PID_K65_MINI:
                if _iface_ok(info, 1):
                    found.append(CorsairK65Mini(info.path, pid, "Corsair K65 Mini"))
            elif pid in V2_PIDS:
                if _iface_ok(info, 1) and _has_usage_page(info, 0xFF42):
                    name, flavour = V2_PIDS[pid]
                    found.append(CorsairV2(info.path, pid, name, flavour=flavour))
        except DeviceError as e:
            log.info("Corsair %04X at %s skipped: %s", pid, info.path, e)
        except Exception as e:  # noqa: BLE001 - never let one device break the probe
            log.info("Corsair %04X at %s skipped: %s: %s", pid, info.path, type(e).__name__, e)
    return found


__all__ = ["USB_IDS", "probe", "CorsairLegacy", "CorsairK55ProXT", "CorsairK65Mini", "CorsairV2", "KeyboardLayout"]
