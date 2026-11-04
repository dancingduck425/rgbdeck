"""
Razer Chroma driver -- generic, table driven (direct /dev/hidraw).

EXPERIMENTAL: ported from OpenRGB sources, not yet verified on hardware.

Covers the devices OpenRGB's RazerController handles: BlackWidow / Huntsman / Ornata / Cynosa / DeathStalker
keyboards, Blade laptop keyboards, DeathAdder / Basilisk / Viper / Naga / Mamba / Lancehead / Cobra mice,
Firefly / Goliathus / Strider mousemats, Tiamat and Kraken Kitty headsets, Tartarus / Orbweaver keypads and
Chroma accessories (Base Station, Mouse Dock, Nommo and Leviathan speakers, Core, laptop stands, docks...).
Kraken 7.1/V3/V4 headsets, the Hanbo cooler and the Chroma Addressable RGB Controller use different
protocols in OpenRGB and are not handled here.

Protocol (ported from OpenRGB, GPL-2.0-or-later, Controllers/RazerController/):
  * RazerController/RazerController.cpp -- the 90-byte `razer_report` feature report (status, transaction id,
    remaining packets, protocol type, data size, command class, command id, 80 argument bytes, XOR crc) sent
    with HIDIOCSFEATURE on report id 0 (0x07 on the Leviathan V2) and read back with HIDIOCGFEATURE; the
    "standard", "extended" and "linear" matrix command variants, the per-LED commands used by the oldest
    Chroma mice/keypads, brightness, device mode, firmware/serial/keyboard-info queries, the firmware
    effect list (0x0F:0x80/0x81) and the wireless receiver pairing list (0x00:0xBF).
  * RazerController/RGBController_Razer.cpp -- mode list, flags, speed/brightness ranges, wave direction,
    zone/LED construction and the "write colors switches to custom frame" behaviour.
  * RazerControllerDetect.cpp -- which USB interface / HID usage each PID is opened on, and the receiver
    pairing relay (commands sent through a dock with the paired device's transaction id).
  * RazerDevices.cpp + KeyboardLayoutManager/KeyboardLayoutManager.cpp -- device table and the keyboard
    key-name generator (base key blocks + regional overlay + per-device edit opcodes), both ported as data
    into razer_devices.py by tools/gen_razer_table.py; the layout algorithm is ported below.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

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
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_DIRECTION_LR,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_HAS_RANDOM_COLOR,
    MODE_FLAG_HAS_SPEED,
    rgb_to_u32,
    u32_to_rgb,
)
from rgbdeck.hw.hidraw import HidDevice, HidInfo, enumerate_hid
from rgbdeck.hw.razer_devices import (
    BRIGHTNESS_100_PIDS,
    DEVICES,
    KEEPALIVE_PIDS,
    KLM_BASE,
    KLM_REGIONAL,
    LAYOUTS,
    LED_ID_ZERO_PIDS,
    NO_BREATHING_PIDS,
    PIDS,
    REPORT_INDEX_07_PIDS,
    SOFTWARE_MODE_PIDS,
    VID,
    WAVE_PIDS,
)

log = logging.getLogger("rgbdeck.razer")

USB_IDS: List[Tuple[int, int]] = [(VID, pid) for pid in DEVICES]

# razer_report is 90 bytes; hidraw wants the report id in front -> 91 bytes on the ioctl
PKT = 91
ARG = 9  # offset of arguments[0]
MIN_GAP = 0.001  # minimum spacing between two feature reports

# RazerController.h enums
DEVICE_MODE_HARDWARE, DEVICE_MODE_SOFTWARE = 0x00, 0x03
CMD_SET_DEVICE_MODE, CMD_GET_FIRMWARE, CMD_GET_SERIAL, CMD_GET_DEVICE_MODE = 0x04, 0x81, 0x82, 0x84
CMD_GET_KEYBOARD_INFO, CMD_GET_WIRELESS_CONNECTIONS = 0x86, 0xBF
STORAGE_NO_SAVE = 0x00
LED_ZERO, LED_SCROLL_WHEEL, LED_LOGO, LED_BACKLIGHT = 0x00, 0x01, 0x04, 0x05
MATRIX_NONE, MATRIX_STANDARD, MATRIX_EXTENDED, MATRIX_LINEAR, MATRIX_EXTENDED_ARGB, MATRIX_CUSTOM = range(6)
LAYOUT_TYPE_ANSI, LAYOUT_TYPE_ISO, LAYOUT_TYPE_JIS, LAYOUT_TYPE_QWERTZ, LAYOUT_TYPE_AZERTY = (
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
)
(
    EFF_OFF,
    EFF_STATIC,
    EFF_BREATHING,
    EFF_SPECTRUM,
    EFF_WAVE,
    EFF_REACTIVE,
    EFF_RIPPLE,
    EFF_STARLIGHT,
    EFF_CUSTOM,
    EFF_FIRE,
) = range(10)
DURATION_SHORT, DURATION_MEDIUM, DURATION_LONG = 1, 2, 3
STATUS_NEW, STATUS_BUSY, STATUS_SUCCESS = 0x00, 0x01, 0x02

# RGBController_Razer.h mode values
M_DIRECT, M_OFF, M_STATIC, M_BREATHING, M_SPECTRUM, M_WAVE, M_REACTIVE, M_STARLIGHT, M_RIPPLE, M_FIRE = range(10)

# Razer keyboard layout code (0x86 reply) -> (KeyboardLayoutManager layout, description) [RazerController.cpp
# GetKeyboardLayoutType / GetKeyboardLayoutString]
KLM_ANSI, KLM_ISO, KLM_QWERTZ, KLM_AZERTY = 1, 2, 3, 4
KEYBOARD_LAYOUTS: Dict[int, Tuple[int, str]] = {
    1: (KLM_ANSI, "US (ANSI)"),
    2: (KLM_ISO, "Greek (ISO)"),
    3: (KLM_QWERTZ, "German (QWERTZ)"),
    4: (KLM_AZERTY, "French (ISO)"),
    5: (KLM_ANSI, "Russian (ANSI)"),
    6: (KLM_ISO, "UK (ISO)"),
    7: (KLM_ISO, "Nordic (ISO)"),
    8: (KLM_ANSI, "Chinese Traditional (ANSI)"),
    9: (KLM_ISO, "Korean (ISO)"),
    10: (KLM_ANSI, "Turkish (ANSI)"),
    11: (KLM_ANSI, "Thai (ANSI)"),
    12: (KLM_ANSI, "Japanese (JIS)"),
    13: (KLM_ISO, "Portugese (Brazil) (ISO)"),
    14: (KLM_ISO, "Spanish (Latin america) (ISO)"),
    15: (KLM_ISO, "Swiss (ISO)"),
    16: (KLM_ISO, "Spanish (Europe) (ISO)"),
    17: (KLM_ISO, "Italian (ISO)"),
    18: (KLM_ISO, "Portugese (Portugal) (ISO)"),
    19: (KLM_ISO, "Hebrew (ISO)"),
    20: (KLM_ANSI, "Arabic (ANSI)"),
}
VARIANTS = {0x00: "Black", 0x80: "Quartz", 0x82: "Mercury"}

# Devices with RAZER_MATRIX_TYPE_CUSTOM that are driven through individual LED commands
# (RazerController.cpp razer_set_custom_frame / razer_set_mode_*): pid -> LED ids in color order
CUSTOM_LEDS: Dict[int, List[int]] = {
    PIDS["TARTARUS_CHROMA"]: [LED_BACKLIGHT],
    PIDS["DEATHADDER_CHROMA"]: [LED_SCROLL_WHEEL, LED_LOGO],
    PIDS["NAGA_EPIC_CHROMA"]: [LED_SCROLL_WHEEL, LED_BACKLIGHT],
    PIDS["MAMBA_2012_WIRED"]: [LED_SCROLL_WHEEL],
    PIDS["MAMBA_2012_WIRELESS"]: [LED_SCROLL_WHEEL],
}
PID_ORBWEAVER = PIDS["ORBWEAVER_CHROMA"]
DEFAULT_COLOR = rgb_to_u32(0, 255, 0)


# ---------------------------------------------------------------------------------------------------------------------
# KeyboardLayoutManager port (OpenRGB KeyboardLayoutManager/KeyboardLayoutManager.cpp)
# ---------------------------------------------------------------------------------------------------------------------
(
    OP_INSERT_SHIFT_RIGHT,
    OP_SWAP_ONLY,
    OP_REMOVE_SHIFT_LEFT,
    OP_INS_SHFT_ADJACENT,
    OP_INSERT_ROW,
    OP_REMOVE_ROW,
    OP_ADD_ALT_NAME,
) = range(7)
SIZE_EMPTY, SIZE_SIXTY = 0, 1
ZONE_MAIN, ZONE_FN_ROW, ZONE_EXTRA, ZONE_NUMPAD = 1, 2, 4, 8


@dataclass
class _Key:
    row: int
    col: int
    name: str
    alt: str = ""


class KeyboardLayout:
    """Builds the key grid of a Razer keyboard the way OpenRGB does: base blocks for the keyboard size, the
    regional overlay for the layout the keyboard reports, then the device's own edit list."""

    def __init__(self, layout: int, size: int, regional: Optional[Dict[int, list]] = None):
        self.keys: List[_Key] = []
        self.rows = self.cols = 0
        if size == SIZE_EMPTY:
            return
        for bit, block in (
            (ZONE_MAIN, "main"),
            (ZONE_FN_ROW, "fn_row"),
            (ZONE_EXTRA, "extras"),
            (ZONE_NUMPAD, "numpad"),
        ):
            if size & bit:
                self.insert_keys(KLM_BASE[block])
        if layout in KLM_REGIONAL:
            self.change_keys(KLM_REGIONAL[layout])
        if regional and layout in regional:
            for e in regional[layout]:
                self.swap_key(_Key(e[0], e[1], e[2], e[3]))
        if size == SIZE_SIXTY:  # drop the empty function row, keep Escape
            self.remove_row(0)
            esc = KLM_BASE["fn_row"][0]
            self.swap_key(_Key(esc[0], esc[1], esc[2], esc[3]))
        self.update_dimensions()

    # -- public ----------------------------------------------------------------------------------------------
    def change_keys(self, edits: list):
        for row, col, name, alt, op in edits:
            key = _Key(row, col, name, alt)
            if op == OP_INSERT_SHIFT_RIGHT:
                self.insert_key(key)
            elif op == OP_SWAP_ONLY:
                self.swap_key(key)
            elif op == OP_REMOVE_SHIFT_LEFT:
                self.remove_key(key)
            elif op == OP_INSERT_ROW:
                if self.insert_row(row):
                    self.swap_key(key)
            elif op == OP_REMOVE_ROW:
                self.remove_row(row)
            elif op == OP_ADD_ALT_NAME:
                for k in self.keys:
                    if k.row == row and k.col == col:
                        k.alt = alt
                        break
            # OP_INS_SHFT_ADJACENT is a TODO in OpenRGB too
        self.update_dimensions()

    def name_at(self, row: int, col: int) -> str:
        for k in self.keys:
            if k.row == row and k.col == col:
                return k.name
        return ""

    def update_dimensions(self):
        self.rows = max((k.row for k in self.keys), default=0) + 1
        self.cols = max((k.col for k in self.keys), default=0) + 1

    # -- primitives (same quirks as the C++ so the generated grids match OpenRGB) --------------------------------
    def insert_keys(self, rows: list):
        for row, col, name, alt, _op in rows:
            self.insert_key(_Key(row, col, name, alt))
        self.update_dimensions()

    def insert_key(self, ins: _Key):
        km = self.keys
        idx = 0
        while idx < len(km) and not (ins.row < km[idx].row or (ins.row == km[idx].row and ins.col <= km[idx].col)):
            idx += 1
        update_row = True
        if idx == len(km):
            km.append(ins)
            update_row = False
        elif ins.name:  # an unused key only shifts the row
            km.insert(idx, ins)
            idx += 1
        if update_row:
            while idx < len(km):
                if km[idx].row == ins.row and km[idx].col >= ins.col:
                    km[idx].col += 1
                if km[idx].row > ins.row:
                    break
                idx += 1

    def swap_key(self, swp: _Key):
        km = self.keys
        if not km:
            km.append(swp)
            return
        for idx, k in enumerate(km):
            if k.row == swp.row and k.col == swp.col:
                if swp.name:
                    k.name = swp.name
                else:
                    del km[idx]
                break
            if k.row > swp.row or (k.row == swp.row and k.col > swp.col):
                if swp.name:
                    km.insert(0 if idx == 0 else idx - 1, swp)
                break

    def remove_key(self, rmv: _Key):
        km = self.keys
        for idx, k in enumerate(km):
            hit = k.row == rmv.row and k.col == rmv.col
            if hit:
                del km[idx]
            if hit or (k.row == rmv.row and rmv.col < k.col):
                while idx < len(km) and km[idx].row == rmv.row:
                    km[idx].col -= 1
                    idx += 1
                break

    def insert_row(self, ins_row: int) -> bool:
        if ins_row >= self.rows:
            return False
        km = self.keys
        idx = next((i for i, k in enumerate(km) if ins_row <= k.row), len(km))
        for k in km[idx:]:
            k.row += 1
        return True

    def remove_row(self, rmv_row: int):
        if rmv_row >= self.rows:
            return
        km = self.keys
        idx = 0
        while idx < len(km) and rmv_row >= km[idx].row:
            if km[idx].row == rmv_row:
                del km[idx]
            else:
                idx += 1
        if idx < len(km) and rmv_row < km[idx].row:
            for k in km[idx:]:
                k.row -= 1


def build_keyboard_layout(layout_name: str, klm_layout: int) -> KeyboardLayout:
    """RGBController_Razer::SetupZones: base layout + regional overlay, then the device's edit keys."""
    lay = LAYOUTS[layout_name]
    kb = KeyboardLayout(klm_layout, lay["size"], lay["regional"])
    if lay["size"] != SIZE_EMPTY or lay["edits"]:
        kb.change_keys(lay["edits"])
    return kb


# ---------------------------------------------------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------------------------------------------------
def razer_crc(report: bytes) -> int:
    """XOR of remaining_packets .. arguments (bytes 3..88 of the 91-byte buffer) [RazerController.cpp
    razer_calculate_crc]."""
    crc = 0
    for b in report[3:89]:
        crc ^= b
    return crc


class RazerDevice:
    def __init__(
        self,
        hid: HidDevice,
        pid: int,
        path: str,
        entry: Optional[dict] = None,
        wait_for_response: bool = False,
        name: Optional[str] = None,
    ):
        self.hid = hid
        self.pid = pid
        self.path = path
        self.entry = entry or DEVICES[pid]
        self.transaction_id = self.entry["transaction_id"]
        self.report_index = 0x07 if pid in REPORT_INDEX_07_PIDS else 0x00
        self.led_id = LED_ZERO if pid in LED_ID_ZERO_PIDS else LED_BACKLIGHT
        self.matrix_type = self.entry["matrix_type"]
        self.rows, self.cols = self.entry["rows"], self.entry["cols"]
        self.max_brightness = 100 if pid in BRIGHTNESS_100_PIDS else 255
        self.wait_for_response = wait_for_response  # device reached through a wireless receiver
        self.effects: Optional[Set[int]] = None  # firmware effect list, None = not available
        self._last_send = 0.0
        self._led_pos: List[int] = []  # device-matrix cell (row*cols+col) of each LED
        if pid in KEEPALIVE_PIDS:
            self.keepalive = self._keepalive  # picked up by the backend's keepalive timer
        try:
            if pid in SOFTWARE_MODE_PIDS:
                self._set_device_mode(DEVICE_MODE_SOFTWARE)
            self._read_supported_effects()
            version = self._get_firmware()
            serial = self._get_serial()
            layout_type, description = KLM_ANSI, "Razer device"
            if self.entry["device_type"] in ("KEYBOARD", "LAPTOP", "KEYPAD"):
                layout_type, layout_name, variant = self._get_keyboard_info()
                description = f"Razer keyboard, {layout_name}, {variant}"
        except DeviceError:
            self.hid.close()
            raise
        self.device = self._build_device(name or self.entry["name"], description, version, serial, layout_type)

    # ---- report plumbing -------------------------------------------------------------------------------------
    def _report(self, command_class: int, command_id: int, data_size: int) -> bytearray:
        """razer_create_report: zeroed report with our report index and transaction id."""
        r = bytearray(PKT)
        r[0] = self.report_index
        r[2] = self.transaction_id
        r[6] = data_size
        r[7] = command_class
        r[8] = command_id
        return r

    def _matches(self, resp: bytes, req: bytes) -> bool:
        return resp[2] == req[2] and resp[7] == req[7] and resp[8] == req[8]

    def _send(self, report: bytearray):
        """razer_usb_send: fill in the crc, send the feature report, and when talking through a receiver poll
        until the relayed command has completed (the receiver drops anything sent while it is busy)."""
        report[89] = razer_crc(report)
        gap = MIN_GAP - (time.monotonic() - self._last_send)
        if gap > 0:
            time.sleep(gap)
        self.hid.send_feature(bytes(report))
        self._last_send = time.monotonic()
        if self.wait_for_response:
            for _attempt in range(50):
                time.sleep(0.002)
                try:
                    resp = self.hid.get_feature(self.report_index, PKT)
                except DeviceError:
                    break
                if self._matches(resp, report) and resp[1] not in (STATUS_NEW, STATUS_BUSY):
                    break

    def _receive(self) -> bytes:
        return self.hid.get_feature(self.report_index, PKT)

    def _query(self, report: bytearray) -> Optional[bytes]:
        """razer_usb_query: send up to 3 times, accept only a finished reply to this very command."""
        for _send_attempt in range(3):
            time.sleep(0.002)
            self._send(report)
            for _read_attempt in range(25):
                time.sleep(0.002)
                resp = self._receive()
                if self._matches(resp, report) and resp[1] not in (STATUS_NEW, STATUS_BUSY):
                    return resp
        return None

    def _get(self, report: bytearray, settle: float = 0.005) -> bytes:
        """The simple get pattern of razer_get_firmware & co (send, short wait, read back), falling back to the
        matching query when another program's reply was in the buffer."""
        time.sleep(0.002)
        self._send(report)
        time.sleep(settle)
        resp = self._receive()
        if not self._matches(resp, report):
            resp = self._query(report) or resp
        return resp

    # ---- queries ---------------------------------------------------------------------------------------------
    def _get_firmware(self) -> str:
        resp = self._get(self._report(0x00, CMD_GET_FIRMWARE, 0x02))
        return f"v{resp[ARG]}.{resp[ARG + 1]}"

    def _get_serial(self) -> str:
        resp = self._get(self._report(0x00, CMD_GET_SERIAL, 0x16))
        return "".join(chr(b) if 30 <= b <= 126 else " " for b in resp[ARG : ARG + 22]).strip()

    def _get_device_mode(self) -> int:
        return self._get(self._report(0x00, CMD_GET_DEVICE_MODE, 0x02))[ARG]

    def _get_keyboard_info(self) -> Tuple[int, str, str]:
        """0x00:0x86 -> (KeyboardLayoutManager layout, layout description, colour variant)."""
        resp = self._get(self._report(0x00, CMD_GET_KEYBOARD_INFO, 0x00), settle=0.001)
        layout, variant = resp[ARG], resp[ARG + 1]
        klm, name = KEYBOARD_LAYOUTS.get(layout, (KLM_ANSI, f"Unknown layout {layout}"))
        return klm, name, VARIANTS.get(variant, "Unknown variant")

    def _read_supported_effects(self):
        """razer_get_supported_effects: extended-matrix devices list their regions (0x0F:0x80) and the effects
        each supports (0x0F:0x81); only effects every region supports count."""
        if self.matrix_type not in (MATRIX_EXTENDED, MATRIX_EXTENDED_ARGB):
            return
        regions: List[int] = []
        if self.led_id != LED_ZERO:
            regions.append(self.led_id)
        else:
            resp = self._query(self._report(0x0F, 0x80, 0x50))
            if resp is None or resp[1] != STATUS_SUCCESS:
                return
            for off in range(0, 76, 5):
                if resp[ARG + off] == 0 or len(regions) >= 16:
                    break
                regions.append(resp[ARG + off])
        effects: Optional[Set[int]] = None
        for region in regions:
            req = self._report(0x0F, 0x81, 0x50)
            req[ARG] = region
            resp = self._query(req)
            if resp is None or resp[1] != STATUS_SUCCESS:
                return
            found = {resp[ARG + i] for i in range(1, min(resp[6], 80))}
            effects = found if effects is None else effects & found
        self.effects = effects

    def paired_pids(self) -> List[int]:
        """GetPairedDevicePIDs: receivers list their pairing slots (count, then status + big-endian PID per slot)."""
        try:
            resp = self._query(self._report(0x00, CMD_GET_WIRELESS_CONNECTIONS, 0x50))
        except DeviceError:
            return []
        if resp is None or resp[1] != STATUS_SUCCESS:
            return []
        out = []
        for slot in range(resp[ARG]):
            if slot * 3 + 3 >= 80:
                break
            pid = (resp[ARG + slot * 3 + 2] << 8) | resp[ARG + slot * 3 + 3]
            if pid not in (0x0000, 0xFFFF):
                out.append(pid)
        return out

    def _keepalive(self):
        """Blade 14 (2021/2022) lighting times out unless something talks to it regularly; a device-mode query
        is enough (RazerController::KeepaliveThreadFunction)."""
        self._get_device_mode()

    # ---- device model ----------------------------------------------------------------------------------------
    def _supports(self, effect: int) -> bool:
        return self.effects is not None and effect in self.effects

    def _has_input(self) -> bool:
        return self.entry["device_type"] in ("KEYBOARD", "MOUSE", "KEYPAD")

    def _can_direct(self) -> bool:
        if self.matrix_type in (MATRIX_STANDARD, MATRIX_EXTENDED, MATRIX_LINEAR):
            return True
        return self.matrix_type == MATRIX_CUSTOM and (self.pid in CUSTOM_LEDS or self.pid == PID_ORBWEAVER)

    def _build_device(self, name: str, description: str, version: str, serial: str, layout_type: int) -> Device:
        zones, led_names = self._build_zones(layout_type)
        B = MODE_FLAG_HAS_BRIGHTNESS
        bmax = self.max_brightness
        modes: List[Mode] = []

        def add(mname: str, value: int, flags: int, **kw):
            kw.setdefault("brightness_max", bmax if flags & B else 0)
            kw.setdefault("brightness", bmax if flags & B else 0)
            modes.append(Mode(len(modes), mname, value, flags, **kw))

        if self._can_direct():
            add("Direct", M_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR | B, color_mode=MODE_COLORS_PER_LED)
        add("Off", M_OFF, 0, color_mode=MODE_COLORS_NONE)
        add(
            "Static",
            M_STATIC,
            MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | B,
            colors_min=1,
            colors_max=1,
            color_mode=MODE_COLORS_MODE_SPECIFIC,
            colors=[DEFAULT_COLOR],
        )
        if self._supports(EFF_BREATHING) or self.pid not in NO_BREATHING_PIDS:
            add(
                "Breathing",
                M_BREATHING,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_RANDOM_COLOR | B,
                colors_min=1,
                colors_max=2,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[DEFAULT_COLOR],
            )
        add("Spectrum Cycle", M_SPECTRUM, B, color_mode=MODE_COLORS_NONE)
        if self._supports(EFF_WAVE) or self.pid in WAVE_PIDS:
            add("Wave", M_WAVE, MODE_FLAG_HAS_DIRECTION_LR | B, direction=1, color_mode=MODE_COLORS_NONE)
        if self._supports(EFF_REACTIVE) and self._has_input():
            add(
                "Reactive",
                M_REACTIVE,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_SPEED | B,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[DEFAULT_COLOR],
                speed_min=DURATION_LONG,
                speed_max=DURATION_SHORT,
                speed=DURATION_MEDIUM,
            )
        if self._supports(EFF_STARLIGHT):
            add(
                "Starlight",
                M_STARLIGHT,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_RANDOM_COLOR | MODE_FLAG_HAS_SPEED | B,
                colors_min=1,
                colors_max=2,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[DEFAULT_COLOR],
                speed_min=DURATION_LONG,
                speed_max=DURATION_SHORT,
                speed=DURATION_MEDIUM,
            )
        if self._supports(EFF_RIPPLE) and self._has_input():
            add(
                "Ripple",
                M_RIPPLE,
                MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | B,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[DEFAULT_COLOR],
            )
        if self._supports(EFF_FIRE):
            add("Fire", M_FIRE, B, color_mode=MODE_COLORS_NONE)
        return Device(
            0,
            self.entry["type"],
            name,
            "Razer",
            description + " (direct hidraw, ported from OpenRGB)",
            version,
            serial,
            "HID: " + self.path,
            0,
            modes,
            zones,
            led_names,
            [0] * len(led_names),
        )

    def _build_zones(self, layout_type: int) -> Tuple[List[Zone], List[str]]:
        """RGBController_Razer::SetupZones.  The device's custom-frame buffer is rows x cols cells and
        OpenRGB's flat LED list maps onto it in order, zone after zone; keyboard zones with a layout table get
        their key names from the layout generator, every other LED is "<zone>" or "<zone> <n>"."""
        zones: List[Zone] = []
        names: List[str] = []
        self._led_pos = []
        base = 0
        for zi, z in enumerate(self.entry["zones"]):
            start = len(names)
            matrix: Optional[List[List[int]]] = None
            rows, cols = z["rows"], z["cols"]
            if z["type"] == 2 and self.entry["layout"] and z["name"] in ("Keyboard", "Keypad"):
                kb = build_keyboard_layout(self.entry["layout"], layout_type)
                matrix = []
                for r in range(rows):
                    line = []
                    for c in range(cols):
                        key = kb.name_at(r, c)
                        if key:
                            line.append(len(names) - start)
                            names.append(key)
                            self._led_pos.append(base + r * cols + c)
                        else:
                            line.append(NO_LED)
                    matrix.append(line)
            else:
                n = rows * cols
                for i in range(n):
                    names.append(z["name"] if n == 1 else f"{z['name']} {i + 1}")
                    self._led_pos.append(base + i)
                if z["type"] == 2:
                    matrix = [[r * cols + c for c in range(cols)] for r in range(rows)]
            count = len(names) - start
            zones.append(Zone(zi, z["name"], z["type"], count, count, count, matrix, start))
            base += rows * cols
        return zones, names

    # ---- colors ---------------------------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        self.device.colors = list(colors)
        # writing a frame switches the device to its custom-frame effect, so only do it in Direct mode
        if self.device.current_mode.color_mode != MODE_COLORS_PER_LED:
            return
        self._write_frame(colors)

    def _write_frame(self, colors: List[int]):
        """RazerController::SetLEDs: one custom-frame row per report, then select the custom effect."""
        frame = [0] * (self.rows * self.cols)
        for pos, c in zip(self._led_pos, colors):
            if pos < len(frame):
                frame[pos] = c
        for row in range(self.rows):
            rgb = bytearray()
            for col in range(self.cols):
                rgb += bytes(u32_to_rgb(frame[row * self.cols + col]))
            time.sleep(0.001)
            self._set_custom_frame(row, 0, self.cols - 1, bytes(rgb))
        time.sleep(0.001)
        self._set_mode_custom()

    def _set_custom_frame(self, row: int, start: int, stop: int, rgb: bytes):
        n = (stop + 1 - start) * 3
        if self.matrix_type == MATRIX_STANDARD:
            r = self._report(0x03, 0x0B, n + 4)
            r[ARG], r[ARG + 1], r[ARG + 2], r[ARG + 3] = 0xFF, row, start, stop
            r[ARG + 4 : ARG + 4 + n] = rgb[:n]
            self._send(r)
        elif self.matrix_type == MATRIX_EXTENDED:
            r = self._report(0x0F, 0x03, n + 5)
            r[ARG + 2], r[ARG + 3], r[ARG + 4] = row, start, stop
            r[ARG + 5 : ARG + 5 + n] = rgb[:n]
            self._send(r)
        elif self.matrix_type == MATRIX_LINEAR:
            r = self._report(0x03, 0x0C, 0x32)
            r[ARG], r[ARG + 1] = start, stop
            r[ARG + 2 : ARG + 2 + n] = rgb[:n]
            self._send(r)
        elif self.matrix_type == MATRIX_CUSTOM:
            if self.pid in CUSTOM_LEDS:
                for i, led in enumerate(CUSTOM_LEDS[self.pid]):
                    self._send(self._led_rgb_report(led, rgb[3 * i : 3 * i + 3]))
            elif self.pid == PID_ORBWEAVER:
                # 5x5 hardware grid presented as 4x5: rows/cols shifted by one, row 3 has a hole at column 2
                if row != 3:
                    self._send(self._standard_frame_report(row + 1, start + 1, stop + 1, rgb[:n]))
                else:
                    adj = rgb[0:3] + bytes(3) + rgb[3:15]
                    self._send(self._standard_frame_report(row + 1, start + 1, stop + 2, adj))

    def _standard_frame_report(self, row: int, start: int, stop: int, rgb: bytes) -> bytearray:
        n = (stop + 1 - start) * 3
        r = self._report(0x03, 0x0B, n + 4)
        r[ARG], r[ARG + 1], r[ARG + 2], r[ARG + 3] = 0xFF, row, start, stop
        r[ARG + 4 : ARG + 4 + n] = rgb[:n].ljust(n, b"\0")
        return r

    # ---- effect reports (razer_create_mode_* in RazerController.cpp) --------------------------------------------
    def _ext(self, data_size: int, effect: int, *args: int) -> bytearray:
        """Extended matrix effect report 0x0F:0x02 [storage, led, effect, ...]."""
        r = self._report(0x0F, 0x02, data_size)
        r[ARG], r[ARG + 1], r[ARG + 2] = STORAGE_NO_SAVE, self.led_id, effect
        r[ARG + 3 : ARG + 3 + len(args)] = bytes(args)
        return r

    def _std(self, data_size: int, *args: int) -> bytearray:
        """Standard matrix effect report 0x03:0x0A."""
        r = self._report(0x03, 0x0A, data_size)
        r[ARG : ARG + len(args)] = bytes(args)
        return r

    def _led_rgb_report(self, led: int, rgb: bytes) -> bytearray:
        r = self._report(0x03, 0x01, 0x05)
        r[ARG], r[ARG + 1] = STORAGE_NO_SAVE, led
        r[ARG + 2 : ARG + 5] = bytes(rgb[:3]).ljust(3, b"\0")
        return r

    def _led_effect_report(self, led: int, effect: int) -> bytearray:
        r = self._report(0x03, 0x02, 0x03)
        r[ARG], r[ARG + 1], r[ARG + 2] = STORAGE_NO_SAVE, led, min(effect, 5)
        return r

    def _effect_report(self, effect: int, flags: int, rate: int, colors: List[Tuple[int, int, int]]) -> bytearray:
        """razer_create_mode_effect_extended_matrix_report: effect, flags (sub-mode/direction), rate, colour count."""
        r = self._ext(0x06 + 3 * len(colors), effect, flags, rate, len(colors))
        for i, (red, grn, blu) in enumerate(colors):
            r[ARG + 6 + 3 * i : ARG + 9 + 3 * i] = bytes((red, grn, blu))
        return r

    def _device_mode_report(self, mode: int) -> bytearray:
        r = self._report(0x00, CMD_SET_DEVICE_MODE, 0x02)
        r[ARG], r[ARG + 1] = mode, 0x00
        return r

    def _set_device_mode(self, mode: int):
        self._send(self._device_mode_report(mode))

    def _is_standard(self) -> bool:
        return self.matrix_type in (MATRIX_STANDARD, MATRIX_LINEAR) or (
            self.matrix_type == MATRIX_CUSTOM and self.pid == PID_ORBWEAVER
        )

    def _is_extended(self) -> bool:
        return self.matrix_type in (MATRIX_EXTENDED, MATRIX_EXTENDED_ARGB)

    def _custom_leds(self) -> List[int]:
        return CUSTOM_LEDS.get(self.pid, []) if self.matrix_type == MATRIX_CUSTOM else []

    def _set_brightness(self, brightness: int):
        brightness = max(0, min(self.max_brightness, brightness))
        if self._is_standard():
            r = self._report(0x03, 0x03, 0x03)
        elif self.matrix_type == MATRIX_EXTENDED:
            r = self._report(0x0F, 0x04, 0x03)
        else:
            return
        r[ARG], r[ARG + 1], r[ARG + 2] = STORAGE_NO_SAVE, self.led_id, brightness
        self._send(r)

    def _set_mode_custom(self):
        if self._is_standard():
            self._send(self._std(0x02, 0x05, STORAGE_NO_SAVE))
        elif self.matrix_type == MATRIX_EXTENDED:
            r = self._report(0x0F, 0x02, 0x0C)
            r[ARG + 2] = EFF_CUSTOM
            self._send(r)
        else:
            for led in self._custom_leds():
                self._send(self._led_effect_report(led, 0))

    def _set_mode_none(self):
        if self._is_standard():
            self._send(self._std(0x01, 0x00))
        elif self._is_extended():
            self._send(self._ext(0x06, EFF_OFF))
        else:
            for led in self._custom_leds():
                self._send(self._led_rgb_report(led, bytes(3)))
                self._send(self._led_effect_report(led, 0))

    def _set_mode_static(self, red: int, grn: int, blu: int):
        if self._is_standard():
            self._send(self._std(0x04, 0x06, red, grn, blu))
        elif self._is_extended():
            self._send(self._ext(0x09, EFF_STATIC, 0x00, 0x00, 0x01, red, grn, blu))
        else:
            for led in self._custom_leds():
                self._send(self._led_rgb_report(led, bytes((red, grn, blu))))
                self._send(self._led_effect_report(led, 0))

    def _set_mode_spectrum_cycle(self):
        if self._is_standard():
            self._send(self._std(0x01, 0x04))
        elif self._is_extended():
            self._send(self._ext(0x06, EFF_SPECTRUM))
        else:
            for led in self._custom_leds():
                self._send(self._led_effect_report(led, 4))

    def _set_mode_wave(self, direction: int):
        if self._is_standard():
            self._send(self._std(0x02, 0x01, direction))
        elif self._is_extended():
            self._send(self._ext(0x06, EFF_WAVE, direction, 0x28))

    def _set_mode_breathing(self, colors: List[Tuple[int, int, int]]):
        """0, 1 or 2 colours (random / one colour / two colours)."""
        flat = [c for rgb in colors for c in rgb]
        if self._is_standard():
            self._send(self._std(0x08, 0x03, {0: 0x03, 1: 0x01, 2: 0x02}[len(colors)], *flat))
        elif self._is_extended():
            if not colors:
                self._send(self._ext(0x06, EFF_BREATHING))
            else:
                n = len(colors)
                self._send(self._ext(0x09 if n == 1 else 0x0C, EFF_BREATHING, n, 0x00, n, *flat))
        elif len(colors) == 1:
            for led in self._custom_leds():
                self._send(self._led_rgb_report(led, bytes(flat)))
                self._send(self._led_effect_report(led, 2))

    # ---- modes ------------------------------------------------------------------------------------------------
    def set_mode(self, mode: Mode, save: bool = False):
        """RGBController_Razer::DeviceUpdateMode.  Razer's software-mode effects are never written to flash
        (OpenRGB always sends RAZER_STORAGE_NO_SAVE), so `save` is ignored."""
        self.device.active_mode = mode.index
        rgb = [u32_to_rgb(c) for c in mode.colors]
        first = rgb[0] if rgb else (0, 0, 0)
        speed = max(DURATION_SHORT, min(DURATION_LONG, mode.speed or DURATION_MEDIUM))
        random = mode.color_mode == MODE_COLORS_RANDOM
        v = mode.value
        if v == M_DIRECT:
            self._set_brightness(mode.brightness)
            self._write_frame(self.device.colors)
            return
        if v == M_OFF:
            self._set_mode_none()
        elif v == M_STATIC:
            self._set_mode_static(*first)
        elif v == M_BREATHING:
            self._set_mode_breathing([] if random else rgb[:2])
        elif v == M_SPECTRUM:
            self._set_mode_spectrum_cycle()
        elif v == M_WAVE:
            self._set_mode_wave(2 if mode.direction == 0 else 1)
        elif v == M_REACTIVE:
            self._send(self._effect_report(EFF_REACTIVE, 0x00, speed, [first]))
        elif v == M_STARLIGHT:
            cols = [] if random else rgb[:2]
            self._send(self._effect_report(EFF_STARLIGHT, len(cols), speed, cols))
        elif v == M_RIPPLE:
            self._send(self._effect_report(EFF_RIPPLE, 0x00, 0x1E, [first]))
        elif v == M_FIRE:
            # firmware rejects fire without a colour; Synapse always sends red
            self._send(self._effect_report(EFF_FIRE, 0x00, 0x00, [(0xFF, 0x00, 0x00)]))
        self._set_brightness(mode.brightness if mode.has_brightness else 255)

    def close(self):
        self.hid.close()


# ---------------------------------------------------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------------------------------------------------
def descriptor_usages(desc: bytes) -> Set[Tuple[int, int]]:
    """(usage page, usage) of every top-level application collection in a HID report descriptor."""
    pairs: Set[Tuple[int, int]] = set()
    page = usage = 0
    i = 0
    while i < len(desc):
        b = desc[i]
        i += 1
        if b == 0xFE:  # long item
            i += 2 + (desc[i] if i < len(desc) else 0)
            continue
        size = (0, 1, 2, 4)[b & 3]
        val = int.from_bytes(desc[i : i + size], "little")
        i += size
        tag = b & 0xFC
        if tag == 0x04:  # Usage Page
            page = val
        elif tag == 0x08:  # Usage
            if size == 4:
                page, usage = val >> 16, val & 0xFFFF
            else:
                usage = val
        elif tag == 0xA0 and val == 0x01:  # Collection (Application)
            pairs.add((page, usage))
    return pairs


def node_matches(info: HidInfo, rules: list) -> bool:
    """RazerControllerDetect.cpp REGISTER_HID_DETECTOR_IPU/PU/I: interface and/or usage page + usage."""
    usages = descriptor_usages(info.descriptor) if info.descriptor else None
    for iface, page, usage in rules:
        if iface is not None and info.interface not in (-1, iface):
            continue
        if page is not None and usages is not None and (page, usage) not in usages:
            continue
        return True
    return False


def probe() -> List[RazerDevice]:
    found: List[RazerDevice] = []
    try:
        infos = enumerate_hid(VID)
    except Exception as e:  # noqa: BLE001 - enumeration must never take the backend down
        log.warning("hidraw enumeration failed: %s", e)
        return found
    for info in infos:
        entry = DEVICES.get(info.pid)
        if entry is None or not node_matches(info, entry["detect"]):
            continue
        try:
            drv = RazerDevice(HidDevice(info.path), info.pid, info.path, entry)
        except DeviceError as e:
            log.info("skipping %s (%s): %s", info.path, entry["name"], e)
            continue
        except Exception:  # noqa: BLE001
            log.exception("probe of %s (%s) crashed", info.path, entry["name"])
            continue
        found.append(drv)
        log.info(
            "%s on %s: %d LEDs, %s",
            drv.device.name,
            info.path,
            len(drv.device.led_names),
            ", ".join(m.name for m in drv.device.modes),
        )
        # wireless receivers relay commands carrying a paired device's transaction id to that device
        for ppid in drv.paired_pids():
            if ppid == info.pid or ppid not in DEVICES:
                continue
            paired = DEVICES[ppid]
            try:
                found.append(
                    RazerDevice(
                        HidDevice(info.path),
                        ppid,
                        info.path,
                        paired,
                        wait_for_response=True,
                        name=f"{paired['name']} ({entry['name']})",
                    )
                )
            except DeviceError as e:
                log.info("skipping paired %s via %s: %s", paired["name"], info.path, e)
            except Exception:  # noqa: BLE001
                log.exception("probe of paired %s via %s crashed", paired["name"], info.path)
    return found
