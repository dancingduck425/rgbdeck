# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Logitech HID++ 2.0 RGB keyboard driver — generic.

Works with any Logitech keyboard exposing feature 0x8080 (per-key lighting) and optionally
0x8070 (onboard effects): G512, G513, G810, G815, G910, G915/TKL, PRO, ...  Keys are discovered
from the device itself (0x8080 GetKeyColors paging), named from the USB HID usage table and the
keyboard's layout code (feature 0x4540), so no per-model tables are needed. Talks to /dev/hidraw.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Tuple

from rgbdeck.models import (
    Device,
    DeviceError,
    Mode,
    Zone,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_DIRECTION_HV,
    MODE_FLAG_HAS_DIRECTION_LR,
    MODE_FLAG_HAS_DIRECTION_UD,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_HAS_SPEED,
    MODE_FLAG_MANUAL_SAVE,
    rgb_to_u32,
    u32_to_rgb,
)
from rgbdeck.hw.hidraw import HidDevice, enumerate_hid

log = logging.getLogger("rgbdeck.logitech")

VID_LOGITECH = 0x046D
# Known RGB keyboards (name only; anything else from Logitech with 0x8080 is probed too)
KNOWN = {
    0xC33C: "Logitech G513",
    0xC33E: "Logitech G512",
    0xC339: "Logitech G810",
    0xC337: "Logitech G810",
    0xC32B: "Logitech G910",
    0xC335: "Logitech G910",
    0xC33F: "Logitech G815",
    0xC541: "Logitech G915",
    0xC343: "Logitech G915 TKL",
    0xC545: "Logitech G915 TKL",
    0xC336: "Logitech G213",
    0xC342: "Logitech G512 Carbon",
    0xC33A: "Logitech G413",
    0xC54D: "Logitech PRO X 60",
}
USB_IDS = [(VID_LOGITECH, pid) for pid in KNOWN]  # udev rules; unknown Logitech boards are still probed

SWID = 0x07
DEV_IDX = 0xFF
REPORT_SHORT, REPORT_LONG, REPORT_VLONG = 0x10, 0x11, 0x12
FEAT_COLOR_LED_EFFECTS = 0x8070
FEAT_PER_KEY = 0x8080
FEAT_KEYBOARD_LAYOUT = 0x4540
FEAT_DEVICE_NAME = 0x0005
ERR_BUSY = 0x08
KEYS_PER_FRAME = 14

KT_KEYBOARD = 0x0001  # keyIds are USB HID keyboard usages
KT_INDICATOR = 0x0040  # logos / lock indicators / G-key clusters (device specific)

# USB HID keyboard usage -> key name (which keyboard_layout.py and effects.py understand)
USAGE_NAMES: Dict[int, str] = {
    0x28: "Enter",
    0x29: "Escape",
    0x2A: "Backspace",
    0x2B: "Tab",
    0x2C: "Space",
    0x2D: "-",
    0x2E: "=",
    0x2F: "[",
    0x30: "]",
    0x31: "\\ (ANSI)",
    0x32: "# (ISO)",
    0x33: ";",
    0x34: "'",
    0x35: "`",
    0x36: ",",
    0x37: ".",
    0x38: "/",
    0x39: "Caps Lock",
    0x46: "Print Screen",
    0x47: "Scroll Lock",
    0x48: "Pause/Break",
    0x49: "Insert",
    0x4A: "Home",
    0x4B: "Page Up",
    0x4C: "Delete",
    0x4D: "End",
    0x4E: "Page Down",
    0x4F: "Right Arrow",
    0x50: "Left Arrow",
    0x51: "Down Arrow",
    0x52: "Up Arrow",
    0x53: "Num Lock",
    0x54: "Number Pad /",
    0x55: "Number Pad *",
    0x56: "Number Pad -",
    0x57: "Number Pad +",
    0x58: "Number Pad Enter",
    0x62: "Number Pad 0",
    0x63: "Number Pad .",
    0x64: "\\ (ISO)",
    0x65: "Menu",
    0x67: "Number Pad =",
    0x85: "Number Pad ,",
    0x87: "Ro (JIS)",
    0x88: "Katakana/Hiragana (JIS)",
    0x89: "Yen (JIS)",
    0x8A: "Henkan (JIS)",
    0x8B: "Muhenkan (JIS)",
    0xE0: "Left Control",
    0xE1: "Left Shift",
    0xE2: "Left Alt",
    0xE3: "Left Windows",
    0xE4: "Right Control",
    0xE5: "Right Shift",
    0xE6: "Right Alt",
    0xE7: "Right Windows",
}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    USAGE_NAMES[0x04 + _i] = _c
for _i in range(1, 10):
    USAGE_NAMES[0x1D + _i] = str(_i)
USAGE_NAMES[0x27] = "0"
for _i in range(1, 13):
    USAGE_NAMES[0x39 + _i] = f"F{_i}"
for _i in range(13, 25):
    USAGE_NAMES[0x68 + _i - 13] = f"F{_i}"
for _i in range(1, 10):
    USAGE_NAMES[0x58 + _i] = f"Number Pad {_i}"

ISO_ONLY = {0x32, 0x64}
JIS_ONLY = {0x87, 0x88, 0x89, 0x8A, 0x8B}
# layout codes from feature 0x4540 (Logitech): 1 = US/ANSI, 2 = UK, 3 = DE, ... JIS boards report their own
ANSI_LAYOUTS = {1, 0}

# keyType 0x40 ids known per model (anything unknown is still exposed as "Extra LED n")
INDICATOR_NAMES: Dict[int, Dict[int, str]] = {
    0xC33C: {0x02: "Game Mode Indicator", 0x03: "Caps Lock Indicator"},  # G513
    0xC33E: {0x02: "Game Mode Indicator", 0x03: "Caps Lock Indicator"},  # G512
    0xC342: {0x02: "Game Mode Indicator", 0x03: "Caps Lock Indicator"},
    0xC339: {0x01: "Logo", 0x02: "Game Mode Indicator", 0x03: "Caps Lock Indicator"},  # G810
    0xC337: {0x01: "Logo", 0x02: "Game Mode Indicator", 0x03: "Caps Lock Indicator"},
}
# ids the firmware lists for keyType 0x40 but that have no LED on that model
INDICATOR_PHANTOMS: Dict[int, set] = {0xC33C: {1, 4, 5}, 0xC33E: {1, 4, 5}, 0xC342: {1, 4, 5}}

# 0x8070 effect ids
EFF_OFF, EFF_STATIC, EFF_CYCLE, EFF_WAVE, EFF_BREATHING, EFF_RIPPLE = 0x0000, 0x0001, 0x0003, 0x0004, 0x000A, 0x000B
PERIOD_MIN, PERIOD_MAX = 1000, 20000
RIPPLE_MIN, RIPPLE_MAX = 2, 200
WAVE_DIR_WIRE = {0: 6, 1: 1, 2: 7, 3: 2, 4: 3, 5: 8}


def _slider_to_period(slider: int, lo: int, hi: int) -> int:
    slider = max(1, min(100, slider))
    return int(hi - (slider - 1) * (hi - lo) / 99)


class LogitechKeyboard:
    def __init__(self, path: str, pid: int, name: str):
        self.hid = HidDevice(path)
        self.pid = pid
        self.feat: Dict[int, int] = {}
        self.effects: Dict[int, int] = {}
        self.mode_effect: Dict[int, int] = {}
        self.keys: List[Tuple[str, int, int]] = []  # (name, keyType, keyId) in display order
        self.hid.drain()
        try:
            self.feat[FEAT_PER_KEY] = self._get_feature_index(FEAT_PER_KEY)
            if not self.feat[FEAT_PER_KEY]:
                raise DeviceError(f"{name}: no per-key lighting feature (0x8080)")
            self.feat[FEAT_COLOR_LED_EFFECTS] = self._get_feature_index(FEAT_COLOR_LED_EFFECTS)
            self.feat[FEAT_KEYBOARD_LAYOUT] = self._get_feature_index(FEAT_KEYBOARD_LAYOUT)
            self.feat[FEAT_DEVICE_NAME] = self._get_feature_index(FEAT_DEVICE_NAME)
            self.layout = self._read_layout()
            dev_name = self._read_name() or name
            self._discover_effects()
            self._discover_keys()
        except DeviceError:
            self.hid.close()
            raise
        self.device = self._build_device(dev_name, path)
        try:
            self.device.colors = self.read_colors()
        except DeviceError:
            pass

    # ---- transport ---------------------------------------------------------------
    def _send(self, feat_idx: int, fn: int, data: bytes = b"", timeout: float = 0.6) -> bytes:
        report, size = (REPORT_LONG, 20) if len(data) <= 16 else (REPORT_VLONG, 64)
        pkt = bytes([report, DEV_IDX, feat_idx, (fn & 0xF0) | SWID]) + data
        pkt += bytes(size - len(pkt))
        for _attempt in range(12):
            self.hid.write(pkt)
            deadline = time.monotonic() + timeout
            busy = False
            while time.monotonic() < deadline:
                buf = self.hid.read(64, deadline - time.monotonic())
                if not buf or buf[0] not in (REPORT_SHORT, REPORT_LONG, REPORT_VLONG) or buf[1] != DEV_IDX:
                    continue
                if buf[2] == 0xFF and buf[3] == feat_idx and buf[4] == ((fn & 0xF0) | SWID):
                    if buf[5] == ERR_BUSY:
                        busy = True
                        break
                    raise DeviceError(f"HID++ error 0x{buf[5]:02X} (feature idx 0x{feat_idx:02X} fn 0x{fn:02X})")
                if buf[2] == feat_idx and buf[3] == ((fn & 0xF0) | SWID):
                    return buf[4:]
            if busy:
                time.sleep(0.03)
        raise DeviceError("keyboard did not answer (is another program using it?)")

    def _get_feature_index(self, feature_id: int) -> int:
        return self._send(0x00, 0x00, bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF]))[0]

    # ---- discovery -----------------------------------------------------------------
    def _read_layout(self) -> int:
        idx = self.feat.get(FEAT_KEYBOARD_LAYOUT)
        if not idx:
            return 1
        try:
            return self._send(idx, 0x00)[0]
        except DeviceError:
            return 1

    def _read_name(self) -> str:
        idx = self.feat.get(FEAT_DEVICE_NAME)
        if not idx:
            return ""
        try:
            n = self._send(idx, 0x00)[0]
            out = b""
            while len(out) < n:
                chunk = self._send(idx, 0x10, bytes([len(out)]))
                out += chunk[:16]
                if not chunk[:16].strip(b"\0"):
                    break
            return out[:n].split(b"\0", 1)[0].decode(errors="replace").strip()
        except DeviceError:
            return ""

    def _discover_effects(self):
        idx = self.feat.get(FEAT_COLOR_LED_EFFECTS)
        if not idx:
            return
        try:
            if self._send(idx, 0x00)[0] < 1:
                return
            count = self._send(idx, 0x10, bytes([0, 0]))[3]
            for j in range(count):
                e = self._send(idx, 0x20, bytes([0, j, 0, 0]))
                self.effects.setdefault((e[2] << 8) | e[3], j)
        except DeviceError as e:
            log.warning("0x8070 effect discovery failed (%s); onboard effects disabled", e)
            self.effects = {}

    def _enumerate_ids(self, key_type: int) -> List[int]:
        """All keyIds the firmware exposes for a keyType (via GetKeyColors paging)."""
        idx = self.feat[FEAT_PER_KEY]
        ids: List[int] = []
        start = 0
        for _page in range(32):
            resp = self._send(idx, 0x20, bytes([key_type >> 8, key_type & 0xFF, start >> 8, start & 0xFF, 0]))
            entries = resp[4:]
            n = 0
            for e in range(0, len(entries) - 3, 4):
                kid = entries[e]
                if kid == 0 and not any(entries[e + 1 : e + 4]) and (n or ids):
                    continue
                if kid == 0 and not any(entries[e + 1 : e + 4]):
                    continue
                if kid not in ids:
                    ids.append(kid)
                n += 1
            if n < KEYS_PER_FRAME:
                break
            start += KEYS_PER_FRAME
        return ids

    def _discover_keys(self):
        kb_ids = self._enumerate_ids(KT_KEYBOARD)
        if not kb_ids:
            raise DeviceError("keyboard reported no addressable keys")
        drop = set()
        if self.layout in ANSI_LAYOUTS:
            drop |= ISO_ONLY | JIS_ONLY
        elif self.layout not in (0x0B, 0x12):  # non-JIS layouts: keep ISO keys, drop JIS
            drop |= JIS_ONLY
        keys = [(USAGE_NAMES.get(k, f"Key 0x{k:02X}"), KT_KEYBOARD, k) for k in kb_ids if k not in drop]
        try:
            ind_ids = self._enumerate_ids(KT_INDICATOR)
        except DeviceError:
            ind_ids = []
        names = INDICATOR_NAMES.get(self.pid, {})
        phantoms = INDICATOR_PHANTOMS.get(self.pid, set())
        indicators = [(names.get(k, f"Extra LED {k}"), KT_INDICATOR, k) for k in ind_ids if k not in phantoms]
        # display order: follow the standard board reading order so the on-screen layout matches
        order = {name: i for i, name in enumerate(_display_order())}
        keys.sort(key=lambda t: order.get(t[0], 10_000 + t[2]))
        # indicators go after Pause/Break like the physical G-series boards
        ins = next((i + 1 for i, t in enumerate(keys) if t[0] == "Pause/Break"), len(keys))
        self.keys = keys[:ins] + indicators + keys[ins:]
        log.info("%s: %d keys (%d indicators), layout code %d", self.pid, len(self.keys), len(indicators), self.layout)

    def _build_device(self, name: str, path: str) -> Device:
        modes: List[Mode] = []

        def add(mname, eff_id, flags, **kw):
            m = Mode(len(modes), mname, eff_id, flags, **kw)
            self.mode_effect[m.index] = eff_id
            modes.append(m)

        add("Direct", -1, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)
        save = MODE_FLAG_MANUAL_SAVE
        if EFF_OFF in self.effects:
            add("Off", EFF_OFF, save)
        if EFF_STATIC in self.effects:
            add(
                "Static",
                EFF_STATIC,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | save,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[0x00B4FF],
            )
        if EFF_BREATHING in self.effects:
            add(
                "Breathing",
                EFF_BREATHING,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | save,
                speed_min=1,
                speed_max=100,
                speed=70,
                brightness_min=1,
                brightness_max=100,
                brightness=100,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[0x00B4FF],
            )
        if EFF_CYCLE in self.effects:
            add(
                "Spectrum Cycle",
                EFF_CYCLE,
                MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | save,
                speed_min=1,
                speed_max=100,
                speed=80,
                brightness_min=1,
                brightness_max=100,
                brightness=100,
            )
        if EFF_WAVE in self.effects:
            add(
                "Color Wave",
                EFF_WAVE,
                MODE_FLAG_HAS_SPEED
                | MODE_FLAG_HAS_BRIGHTNESS
                | MODE_FLAG_HAS_DIRECTION_LR
                | MODE_FLAG_HAS_DIRECTION_UD
                | MODE_FLAG_HAS_DIRECTION_HV
                | save,
                speed_min=1,
                speed_max=100,
                speed=80,
                brightness_min=1,
                brightness_max=100,
                brightness=100,
                direction=1,
            )
        if EFF_RIPPLE in self.effects:
            add(
                "Ripple",
                EFF_RIPPLE,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_SPEED | save,
                speed_min=1,
                speed_max=100,
                speed=70,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[0x00B4FF],
            )
        led_names = [("Key: " + n) if t == KT_KEYBOARD else n for n, t, _i in self.keys]
        zone = Zone(0, "Keyboard", 2, len(led_names), len(led_names), len(led_names), None, 0)
        return Device(
            0,
            "Keyboard",
            name,
            "Logitech",
            "Logitech HID++ 2.0 keyboard (direct hidraw)",
            "",
            "",
            "HID: " + path,
            0,
            modes,
            [zone],
            led_names,
            [0] * len(led_names),
        )

    # ---- per-key ------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        idx = self.feat[FEAT_PER_KEY]
        by_type: Dict[int, List[Tuple[int, int]]] = {}
        for (_n, ktype, kid), col in zip(self.keys, colors):
            by_type.setdefault(ktype, []).append((kid, col))
        self.hid.drain()
        for ktype, keys in by_type.items():
            for off in range(0, len(keys), KEYS_PER_FRAME):
                chunk = keys[off : off + KEYS_PER_FRAME]
                payload = bytes([ktype >> 8, ktype & 0xFF, 0, len(chunk)])
                for kid, col in chunk:
                    r, g, b = u32_to_rgb(col)
                    payload += bytes([kid, r, g, b])
                self._send(idx, 0x30, payload)  # one write in flight at a time (firmware drops pipelined ones)
        self._send(idx, 0x50)  # flush
        self.device.colors = list(colors)

    def read_colors(self) -> List[int]:
        idx = self.feat[FEAT_PER_KEY]
        got: Dict[Tuple[int, int], int] = {}
        for ktype in sorted({t for _n, t, _i in self.keys}):
            start = 0
            for _page in range(32):
                resp = self._send(idx, 0x20, bytes([ktype >> 8, ktype & 0xFF, start >> 8, start & 0xFF, 0]))
                entries = resp[4:]
                n = 0
                for e in range(0, len(entries) - 3, 4):
                    kid, r, g, b = entries[e : e + 4]
                    if kid == 0 and not (r or g or b):
                        continue
                    got[(ktype, kid)] = rgb_to_u32(r, g, b)
                    n += 1
                if n < KEYS_PER_FRAME:
                    break
                start += KEYS_PER_FRAME
        return [got.get((t, i), 0) for _n, t, i in self.keys]

    # ---- effects ----------------------------------------------------------------------------
    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.color_mode == MODE_COLORS_PER_LED:
            self.set_colors(self.device.colors)
            return
        eff_id = self.mode_effect[mode.index]
        idx = self.feat.get(FEAT_COLOR_LED_EFFECTS)
        if not idx or eff_id not in self.effects:
            raise DeviceError("effect not supported by this keyboard")
        r, g, b = u32_to_rgb(mode.colors[0]) if mode.colors else (0, 0, 0)
        d = bytearray(16)
        d[1] = self.effects[eff_id]
        period = (
            _slider_to_period(mode.speed, RIPPLE_MIN, RIPPLE_MAX)
            if eff_id == EFF_RIPPLE
            else _slider_to_period(mode.speed, PERIOD_MIN, PERIOD_MAX)
        )
        bright = max(1, min(100, mode.brightness or 100))
        if eff_id == EFF_STATIC:
            d[2:5] = (r, g, b)
            d[5] = 0x02 if (r or g or b) else 0
        elif eff_id == EFF_BREATHING:
            d[2:5] = (r, g, b)
            d[5], d[6], d[8] = period >> 8, period & 0xFF, bright
        elif eff_id == EFF_CYCLE:
            d[7], d[8], d[9] = period >> 8, period & 0xFF, bright
        elif eff_id == EFF_WAVE:
            d[8], d[9], d[10], d[11] = period & 0xFF, WAVE_DIR_WIRE.get(mode.direction, 1), bright, period >> 8
        elif eff_id == EFF_RIPPLE:
            d[2:5] = (r, g, b)
            d[6], d[7] = period >> 8, period & 0xFF
        d[12] = 1 if save else 0
        self._send(idx, 0x30, bytes(d))

    def close(self):
        self.hid.close()


def _display_order() -> List[str]:
    rows = [
        ["Escape"] + [f"F{i}" for i in range(1, 13)] + ["Print Screen", "Scroll Lock", "Pause/Break"],
        ["`"]
        + [str(i) for i in range(1, 10)]
        + [
            "0",
            "-",
            "=",
            "Yen (JIS)",
            "Backspace",
            "Insert",
            "Home",
            "Page Up",
            "Num Lock",
            "Number Pad /",
            "Number Pad *",
            "Number Pad -",
        ],
        [
            "Tab",
            "Q",
            "W",
            "E",
            "R",
            "T",
            "Y",
            "U",
            "I",
            "O",
            "P",
            "[",
            "]",
            "\\ (ANSI)",
            "Delete",
            "End",
            "Page Down",
            "Number Pad 7",
            "Number Pad 8",
            "Number Pad 9",
            "Number Pad +",
        ],
        [
            "Caps Lock",
            "A",
            "S",
            "D",
            "F",
            "G",
            "H",
            "J",
            "K",
            "L",
            ";",
            "'",
            "# (ISO)",
            "Enter",
            "Number Pad 4",
            "Number Pad 5",
            "Number Pad 6",
        ],
        [
            "Left Shift",
            "\\ (ISO)",
            "Z",
            "X",
            "C",
            "V",
            "B",
            "N",
            "M",
            ",",
            ".",
            "/",
            "Ro (JIS)",
            "Right Shift",
            "Up Arrow",
            "Number Pad 1",
            "Number Pad 2",
            "Number Pad 3",
            "Number Pad Enter",
        ],
        [
            "Left Control",
            "Left Windows",
            "Left Alt",
            "Muhenkan (JIS)",
            "Space",
            "Henkan (JIS)",
            "Katakana/Hiragana (JIS)",
            "Right Alt",
            "Right Windows",
            "Menu",
            "Right Control",
            "Left Arrow",
            "Down Arrow",
            "Right Arrow",
            "Number Pad 0",
            "Number Pad .",
        ],
    ]
    return [k for row in rows for k in row]


def probe() -> List[LogitechKeyboard]:
    found = []
    for info in enumerate_hid(VID_LOGITECH):
        # HID++ long reports live on the second interface of Logitech keyboards
        if info.interface != 1:
            continue
        if info.pid not in KNOWN and b"\x06\x43\xff" not in info.descriptor:  # vendor page FF43 = HID++
            continue
        try:
            found.append(LogitechKeyboard(info.path, info.pid, info.name or KNOWN.get(info.pid, "Logitech keyboard")))
        except DeviceError as e:
            log.info("skipping %s: %s", info.path, e)
    return found
