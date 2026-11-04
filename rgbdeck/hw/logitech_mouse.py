"""
Logitech HID++ 2.0 RGB mice, mousemats and headsets — wired, or through Lightspeed / Unifying receivers.

EXPERIMENTAL: ported from OpenRGB sources, not yet verified on hardware.

Supported devices (anything Logitech that answers HID++ 2.0 and exposes feature 0x8071 RGB_EFFECTS or
0x8070 COLOR_LED_EFFECTS cluster lighting; keyboards are left to rgbdeck.hw.logitech_hidpp):
  mice       G203 Prodigy / Lightsync, G303 Daedalus Apex, G403 Prodigy / HERO / Lightspeed, G502 Proteus Spectrum /
             HERO / Lightspeed / X PLUS, G703 (+HERO), G900, G903 (+HERO), G Pro / Pro HERO / Pro Wireless
  mousemat   G Powerplay (through its own receiver 046d:c53a)
  headsets   G633, G635, G733, G933, G935 (HID++ interface on usage page 0xFF43)
  receivers  Lightspeed 046d:c539, Powerplay c53a, G915-family c541 / c545 / c547, Unifying c52b / c532:
             every paired slot (device index 1..6) becomes its own device; sleeping slots are skipped.

Protocol ported from OpenRGB (GPL-2.0; read as reference only — never linked or run), under
OpenRGB Controllers/LogitechController/:
  LogitechProtocolCommon.{h,cpp}                 RAP receiver pairing enumeration (getWirelessDevice,
                                                 getWirelessDeviceName/Serial), 0x8070/0x8071 function numbers,
                                                 legacy logitech_device::setMode / set8071Effects packets
  LogitechHIDPP20Controller/LogitechHIDPP20Controller.{h,cpp}
                                                 feature discovery (Probe, DiscoverRGBEffects), SetSWControl,
                                                 SetRGBPowerMode, WritePowerConfig, SetHostMode,
                                                 ClaimSWControlIfNeeded, UpgradeSwControlAfterFirstPaint,
                                                 SetZoneEffect payload layouts, Shutdown, QueryExternalPower
  LogitechHIDPP20Controller/RGBController_LogitechHIDPP20.cpp
                                                 mode table per effect id, zone_location_name, Direct rides
                                                 Static per cluster, persist rules (0x8070 ephemeral / 0x8071 always)
  LogitechLightspeedController/*, LogitechGLightsyncController/*, LogitechG203LController/*
                                                 legacy mouse packets, cross-checked against the above
  LogitechControllerDetect.cpp                   VID/PID table, HID++ interface selection (usage page 0xFF00 usage 2 /
                                                 0xFF43 usage 0x0202), receiver handling
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from rgbdeck.models import (Device, DeviceError, Mode, Zone, MODE_COLORS_MODE_SPECIFIC, MODE_COLORS_NONE,
                            MODE_COLORS_PER_LED, MODE_FLAG_AUTOMATIC_SAVE, MODE_FLAG_HAS_BRIGHTNESS,
                            MODE_FLAG_HAS_DIRECTION_HV, MODE_FLAG_HAS_DIRECTION_LR, MODE_FLAG_HAS_DIRECTION_UD,
                            MODE_FLAG_HAS_MODE_SPECIFIC_COLOR, MODE_FLAG_HAS_PER_LED_COLOR, MODE_FLAG_HAS_SPEED,
                            MODE_FLAG_MANUAL_SAVE, u32_to_rgb)
from rgbdeck.hw.hidraw import HidDevice, HidInfo, enumerate_hid
# Shared HID++ 2.0 constants with the keyboard driver (same transport, same software id 7, same period mapping).
from rgbdeck.hw.logitech_hidpp import (ERR_BUSY, PERIOD_MAX, PERIOD_MIN, REPORT_LONG, REPORT_SHORT, REPORT_VLONG,
                                       RIPPLE_MAX, RIPPLE_MIN, SWID, VID_LOGITECH, WAVE_DIR_WIRE, _slider_to_period)

log = logging.getLogger("rgbdeck.logitech_mouse")

T_MOUSE, T_MOUSEMAT, T_HEADSET = "Mouse", "Mousemat", "Headset"

# USB (cable) product ids -> (name, type).  LogitechControllerDetect.cpp "Mouse product IDs" / "Headset product IDs"
# and the REGISTER_HID_DETECTOR_* table (DetectLogitechWired, DetectLogitechMouseG203/G303/G403/GPRO, G203L).
WIRED: Dict[int, Tuple[str, str]] = {
    0xC084: ("Logitech G203 Prodigy", T_MOUSE),
    0xC092: ("Logitech G203 Lightsync", T_MOUSE),
    0xC09D: ("Logitech G203 Lightsync", T_MOUSE),
    0xC080: ("Logitech G303 Daedalus Apex", T_MOUSE),
    0xC083: ("Logitech G403 Prodigy", T_MOUSE),
    0xC08F: ("Logitech G403 HERO", T_MOUSE),
    0xC082: ("Logitech G403 Lightspeed (wired)", T_MOUSE),
    0xC332: ("Logitech G502 Proteus Spectrum", T_MOUSE),
    0xC08B: ("Logitech G502 HERO", T_MOUSE),
    0xC08D: ("Logitech G502 Lightspeed (wired)", T_MOUSE),
    0xC095: ("Logitech G502 X PLUS (wired)", T_MOUSE),
    0xC087: ("Logitech G703 Lightspeed (wired)", T_MOUSE),
    0xC090: ("Logitech G703 HERO Lightspeed (wired)", T_MOUSE),
    0xC081: ("Logitech G900 Lightspeed (wired)", T_MOUSE),
    0xC086: ("Logitech G903 Lightspeed (wired)", T_MOUSE),
    0xC091: ("Logitech G903 HERO Lightspeed (wired)", T_MOUSE),
    0xC085: ("Logitech G Pro", T_MOUSE),
    0xC08C: ("Logitech G Pro HERO", T_MOUSE),
    0xC088: ("Logitech G Pro Wireless (wired)", T_MOUSE),
    0x0A5C: ("Logitech G633 Headset", T_HEADSET),
    0x0A89: ("Logitech G635 Headset", T_HEADSET),
    0x0AB5: ("Logitech G733 Headset", T_HEADSET),
    0x0AFE: ("Logitech G733 Headset", T_HEADSET),
    0x0B1F: ("Logitech G733 Headset", T_HEADSET),
    0x0A5B: ("Logitech G933 Headset", T_HEADSET),
    0x0A87: ("Logitech G935 Headset", T_HEADSET),
}
# Receivers: the paired devices are enumerated over the receiver's HID++ node (LogitechControllerDetect.cpp
# "Unifying Device IDs (Including Lightspeed receivers)", LOGITECH_G915_RECEIVER*_PID).
RECEIVERS: Dict[int, str] = {
    0xC539: "Logitech Lightspeed receiver",
    0xC53A: "Logitech G Powerplay receiver",
    0xC541: "Logitech G915 Lightspeed receiver",
    0xC545: "Logitech G915 TKL Lightspeed receiver",
    0xC547: "Logitech Lightspeed receiver",
    0xC52B: "Logitech Unifying receiver",
    0xC532: "Logitech Unifying receiver",
}
USB_IDS: List[Tuple[int, int]] = [(VID_LOGITECH, pid) for pid in list(WIRED) + list(RECEIVERS)]

# Wireless (virtual) pids the kernel's hid-logitech-dj driver gives the per-slot child nodes; names used when the
# device is asleep and cannot say its own name (LogitechControllerDetect.cpp hidpp20_legacy_wireless_pids).
WIRELESS_PID_NAMES: Dict[int, str] = {
    0x4053: "Logitech G900", 0x405D: "Logitech G403 Lightspeed", 0x405F: "Logitech Powerplay Mat",
    0x4067: "Logitech G903", 0x4070: "Logitech G703", 0x4079: "Logitech G Pro Wireless", 0x407F: "Logitech G502 Lightspeed",
    0x4086: "Logitech G703 HERO", 0x4087: "Logitech G903 HERO", 0x4099: "Logitech G502 X PLUS",
}

# LogitechProtocolCommon.h enum LOGITECH_DEVICE_TYPE (0x0005 GetDeviceType)
DEV_TYPE_NAMES: Dict[int, str] = {3: T_MOUSE, 5: T_MOUSE, 4: T_MOUSEMAT, 8: T_HEADSET, 14: "Speaker", 16: "Light"}
SKIP_DEV_TYPES = {0, 2}                          # keyboard / numpad: rgbdeck.hw.logitech_hidpp owns those

# HID++ 2.0 feature ids (LogitechHIDPP20Controller.h HIDPP20_FEAT_*)
FEAT_FEATURE_SET = 0x0001
FEAT_DEVICE_NAME = 0x0005
FEAT_BATTERY_STATUS = 0x1000
FEAT_UNIFIED_BATTERY = 0x1004
FEAT_COLOR_LED_EFFECTS = 0x8070
FEAT_RGB_EFFECTS = 0x8071
FEAT_PER_KEY_8080 = 0x8080
FEAT_PER_KEY_8081 = 0x8081
FEAT_ONBOARD_PROFILES = 0x8100
FEAT_PROFILE_MANAGEMENT = 0x8101

# 0x8070 functions (LogitechProtocolCommon.h enum LOGITECH_FP8070)
FN_8070_INFO, FN_8070_ZONE_INFO, FN_8070_EFFECT_INFO, FN_8070_SET_EFFECT, FN_8070_SET_SW_CTL = 0x00, 0x10, 0x20, 0x30, 0x80
# 0x8071 functions (LogitechHIDPP20Controller.h FN_8071_*)
FN_8071_GET_INFO, FN_8071_SET_EFFECT, FN_8071_SW_CONTROL, FN_8071_PWR_CONFIG, FN_8071_PWR_MODE = 0x00, 0x10, 0x50, 0x70, 0x80
FN_8100_SET_ONBOARD_MODE = 0x10                  # LogitechHIDPP20Controller.h FN_8100_SET_ONBOARD_MODE
FN_8101_GET_SET_MODE = 0x60                      # LogitechHIDPP20Controller.h FN_8101_GET_SET_MODE
FN_0005_GET_COUNT, FN_0005_GET_NAME, FN_0005_GET_TYPE = 0x00, 0x10, 0x20
FN_1004_GET_STATUS = 0x10
FN_1000_GET_LEVEL_STATUS = 0x00

# HID++ 1.0 register access to the receiver itself (LogitechProtocolCommon.h LOGITECH_*_REGISTER_REQUEST)
DEV_IDX_WIRED = 0xFF
DEV_IDX_RECEIVER = 0xFF
RAP_SET_REGISTER, RAP_GET_REGISTER, RAP_GET_LONG_REGISTER = 0x80, 0x81, 0x83
REG_NOTIFICATIONS, REG_CONNECTION_STATE, REG_PAIRING_INFO = 0x00, 0x02, 0xB5
NOTIF_DEVICE_CONNECTION = 0x41
ERR_HIDPP10 = 0x8F                               # receiver error frame for a slot (sub id 0x8F)
ERR_RESOURCE = 0x09                              # ERR_RESOURCE_ERROR: paired device not connected
RECEIVER_SLOTS = range(1, 7)

# Effect type ids (LogitechProtocolCommon.h LOGITECH_DEVICE_MODE + RGBController_LogitechHIDPP20.cpp switch)
EFF_OFF, EFF_STATIC, EFF_CYCLE, EFF_WAVE, EFF_VISUALIZER = 0x0000, 0x0001, 0x0003, 0x0004, 0x0007
EFF_BREATHING, EFF_RIPPLE, EFF_DECOMPOSITION, EFF_SIGNATURE1, EFF_SIGNATURE2 = 0x000A, 0x000B, 0x000E, 0x000F, 0x0010
EFF_CYCLE_SAT, EFF_WAVE_SAT, EFF_RIPPLE_SAT = 0x0015, 0x0016, 0x0017

# RGBController_LogitechHIDPP20.cpp zone_location_name()
ZONE_LOCATION_NAMES: Dict[int, str] = {
    0x0001: "Primary", 0x0002: "Logo", 0x0003: "Left Side", 0x0004: "Right Side", 0x0005: "Combined",
    0x0006: "Primary 1", 0x0007: "Primary 2", 0x0008: "Primary 3", 0x0009: "Primary 4", 0x000A: "Primary 5",
    0x000B: "Primary 6",
}

# LogitechHIDPP20Controller.cpp constructor defaults for WritePowerConfig(idle_timeout_s, sleep_timeout_s)
POWER_IDLE_S, POWER_SLEEP_S = 60, 300
DEFAULT_COLOR = 0x00B4FF


class DeviceOffline(DeviceError):
    """The receiver answered for the slot: the paired device is asleep, off or away on its cable."""


@dataclass
class Effect:
    index: int          # position in the cluster's effect list = what SetEffect wants in data[1]
    effect_id: int
    capabilities: int = 0
    default_period: int = 0


@dataclass
class Cluster:
    index: int
    location: int
    effects: List[Effect] = field(default_factory=list)

    def find(self, effect_id: int) -> Optional[Effect]:
        for e in self.effects:
            if e.effect_id == effect_id:
                return e
        return None


def _hidpp_error(code: int, dev_idx: int, feat_idx: int, fn: int) -> DeviceError:
    return DeviceError(f"HID++ error 0x{code:02X} (device index 0x{dev_idx:02X}, feature idx 0x{feat_idx:02X}, fn 0x{fn:02X})")


class HidppLink:
    """One /dev/hidraw node carrying HID++ frames: a wired device, or a receiver shared by all its paired slots.

    FAP (HID++ 2.0 feature) calls always use the 20-byte long report 0x11 — OpenRGB does the same on Windows and
    notes that several mice ignore the 7-byte short frame for 0x8071 (LogitechProtocolCommon.cpp set8071Effects).
    RAP (HID++ 1.0 register) calls to the receiver use the 7-byte short report 0x10 like getWirelessDevice().
    """

    def __init__(self, hid: HidDevice, wireless: bool = False):
        self.hid = hid
        self.path = hid.path
        self.wireless = wireless
        self.min_interval = 0.008 if wireless else 0.0     # CONTRIBUTING: rate-limit anything over a wireless link
        self._last_write = 0.0
        self.users = 0

    def write_raw(self, pkt: bytes):
        if self.min_interval:
            wait = self._last_write + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self.hid.write(pkt)
        self._last_write = time.monotonic()

    def fap(self, dev_idx: int, feat_idx: int, fn: int, data: bytes = b"", timeout: float = 0.3,
            attempts: int = 4) -> bytes:
        """Send one feature call and wait for its answer. Returns the 16 payload bytes.

        Frames for other device indexes (sibling slots, receiver notifications) and other requests are skipped.
        BUSY (0x08) resends after a short pause (LogitechHIDPP20Controller.cpp SendAcked); a receiver 0x8F frame
        with ERR_RESOURCE_ERROR raises DeviceOffline so a sleeping mouse never hangs the caller.
        """
        if len(data) > 16:
            raise ValueError("HID++ long report carries at most 16 payload bytes")
        fn_sw = (fn & 0xF0) | SWID
        pkt = bytes([REPORT_LONG, dev_idx, feat_idx, fn_sw]) + data
        pkt += bytes(20 - len(pkt))
        for _attempt in range(attempts):
            self.write_raw(pkt)
            deadline = time.monotonic() + timeout
            busy = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                buf = self.hid.read(64, remaining)
                if not buf or len(buf) < 6 or buf[0] not in (REPORT_SHORT, REPORT_LONG, REPORT_VLONG):
                    continue
                if buf[1] != dev_idx:
                    continue
                if buf[2] == 0xFF and buf[3] == feat_idx and buf[4] == fn_sw:         # HID++ 2.0 error frame
                    if buf[5] == ERR_BUSY:
                        busy = True
                        break
                    raise _hidpp_error(buf[5], dev_idx, feat_idx, fn)
                if buf[2] == ERR_HIDPP10 and buf[3] == feat_idx and buf[4] == fn_sw:  # receiver answers for the slot
                    if buf[5] == ERR_RESOURCE:
                        raise DeviceOffline(f"device index {dev_idx} is not connected (asleep or switched off)")
                    raise _hidpp_error(buf[5], dev_idx, feat_idx, fn)
                if buf[2] == feat_idx and buf[3] == fn_sw:
                    payload = bytes(buf[4:20])
                    return payload + bytes(16 - len(payload))
            if busy:
                time.sleep(0.03)
        raise DeviceError(f"device index 0x{dev_idx:02X} did not answer feature idx 0x{feat_idx:02X} fn 0x{fn:02X}")

    def rap(self, sub_id: int, address: int, data: bytes = b"", data0: Optional[int] = None,
            timeout: float = 0.3) -> Optional[bytes]:
        """HID++ 1.0 register access to the receiver (device index 0xFF). Returns the whole reply frame or None.

        LogitechProtocolCommon.cpp getWirelessDevice() / getWirelessDeviceName(): the answer echoes sub id and
        register address; `data0` additionally matches the first data byte (the 0xB5 sub-register)."""
        pkt = bytes([REPORT_SHORT, DEV_IDX_RECEIVER, sub_id, address]) + data
        pkt += bytes(7 - len(pkt))
        self.write_raw(pkt)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            buf = self.hid.read(64, remaining)
            if not buf or len(buf) < 6 or buf[0] not in (REPORT_SHORT, REPORT_LONG) or buf[1] != DEV_IDX_RECEIVER:
                continue
            if buf[2] == ERR_HIDPP10 and buf[3] == sub_id and buf[4] == address:
                raise DeviceError(f"receiver register 0x{address:02X} error 0x{buf[5]:02X}")
            if buf[2] == sub_id and buf[3] == address and (data0 is None or buf[4] == data0):
                return bytes(buf)

    def acquire(self):
        self.users += 1

    def release(self):
        self.users -= 1
        if self.users <= 0:
            self.hid.close()


# ---- receiver (RAP) side --------------------------------------------------------------------------
def enumerate_receiver(link: HidppLink) -> List[Tuple[int, int, bool]]:
    """Paired slots of a receiver as (device index, wireless pid, link established).

    Port of LogitechProtocolCommon.cpp getWirelessDevice(): make sure wireless notifications are on (register 0x00),
    read the connected-device count (register 0x02), then write 0x02 to register 0x02 so the receiver replays one
    0x41 device-connection notification per paired slot. In the notification, data byte 0 bit 0x40 set means
    "paired but link not established" (asleep / off), bytes 1-2 are the wireless pid (little endian)."""
    link.hid.drain()
    r = link.rap(RAP_GET_REGISTER, REG_NOTIFICATIONS)
    if r is None:
        raise DeviceError("receiver did not answer the notification-register read")
    if not (r[5] & 0x01):                                      # response.data[1] & 1 = wireless notifications flag
        link.rap(RAP_SET_REGISTER, REG_NOTIFICATIONS, bytes([0x00, 0x01, 0x00]))
    r = link.rap(RAP_GET_REGISTER, REG_CONNECTION_STATE)
    count = r[5] if r else 0                                   # response.data[1] = number of connected devices
    if count == 0:
        return []
    link.write_raw(bytes([REPORT_SHORT, DEV_IDX_RECEIVER, RAP_SET_REGISTER, REG_CONNECTION_STATE, 0x02, 0x00, 0x00]))
    slots: Dict[int, Tuple[int, bool]] = {}
    deadline = time.monotonic() + 1.5
    while len(slots) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        buf = link.hid.read(64, min(0.3, remaining))
        if not buf or len(buf) < 7 or buf[0] != REPORT_SHORT:
            continue
        if buf[1] == DEV_IDX_RECEIVER:                         # the receiver's own ack to the register write
            continue
        if buf[2] == NOTIF_DEVICE_CONNECTION and buf[1] in RECEIVER_SLOTS:
            slots[buf[1]] = ((buf[6] << 8) | buf[5], not (buf[4] & 0x40))
    return [(idx, pid, online) for idx, (pid, online) in sorted(slots.items())]


def receiver_slot_name(link: HidppLink, idx: int) -> str:
    """Codename the receiver stores for a paired slot (works while the device sleeps).
    LogitechProtocolCommon.cpp getWirelessDeviceName(): GET_LONG_REGISTER 0xB5, sub 0x40 + index - 1."""
    sub = 0x40 + idx - 1
    try:
        r = link.rap(RAP_GET_LONG_REGISTER, REG_PAIRING_INFO, bytes([sub]), data0=sub)
    except DeviceError:
        return ""
    if r is None or len(r) < 7:
        return ""
    n = r[5]
    if n == 0 or n > 14:
        return ""
    name = r[6:6 + n].rstrip(b"\0 ").decode("ascii", errors="replace")
    return name if all(0x20 <= ord(c) <= 0x7E for c in name) else ""


def receiver_slot_serial(link: HidppLink, idx: int) -> str:
    """LogitechProtocolCommon.cpp getWirelessDeviceSerial(): GET_LONG_REGISTER 0xB5, sub 0x30 + index - 1, bytes 1..4."""
    sub = 0x30 + idx - 1
    try:
        r = link.rap(RAP_GET_LONG_REGISTER, REG_PAIRING_INFO, bytes([sub]), data0=sub)
    except DeviceError:
        return ""
    if r is None or len(r) < 9:
        return ""
    serial = r[5:9].hex().upper()
    return "" if serial == "00000000" else serial


# ---- the device ------------------------------------------------------------------------------------
class LogitechHidppDevice:
    """One HID++ 2.0 device with cluster lighting (0x8071 preferred, else 0x8070): a mouse, mat or headset."""

    def __init__(self, link: HidppLink, dev_idx: int, usb_pid: int, fallback_name: str, fallback_type: str,
                 location: str, serial: str = ""):
        self.link = link
        self.dev_idx = dev_idx
        self.pid = usb_pid
        self.wireless = dev_idx != DEV_IDX_WIRED
        self.feat: Dict[int, int] = {}
        self.clusters: List[Cluster] = []
        self.mode_effect: Dict[int, int] = {}          # mode index -> effect id
        self.claimed = False
        self.upgrade_pending = False
        self.sent: List[int] = []
        link.acquire()
        try:
            link.hid.drain()
            # Probe(): a device is there iff IRoot answers for FeatureSet (short budget, sleeping slots answer 0x8F)
            if not self._get_feature_index(FEAT_FEATURE_SET, timeout=0.3, attempts=2):
                raise DeviceError("no HID++ 2.0 answer (not a HID++ device, or asleep)")
            for fid in (FEAT_DEVICE_NAME, FEAT_PER_KEY_8080, FEAT_PER_KEY_8081, FEAT_RGB_EFFECTS,
                        FEAT_COLOR_LED_EFFECTS, FEAT_ONBOARD_PROFILES, FEAT_PROFILE_MANAGEMENT,
                        FEAT_UNIFIED_BATTERY, FEAT_BATTERY_STATUS):
                self.feat[fid] = self._get_feature_index(fid)
            if self.feat[FEAT_PER_KEY_8080]:
                raise DeviceError("per-key keyboard (feature 0x8080): handled by rgbdeck.hw.logitech_hidpp")
            name, dev_type = self._read_name_type()
            name = name or fallback_name
            if dev_type in SKIP_DEV_TYPES:
                raise DeviceError(f"{name}: keyboard-class device, left to rgbdeck.hw.logitech_hidpp")
            type_name = DEV_TYPE_NAMES.get(dev_type, fallback_type)
            # DiscoverRGBEffects(): 0x8071 first, then 0x8070 (0x0600 Centurion is not ported)
            if self.feat[FEAT_RGB_EFFECTS]:
                self.page, self.rgb_idx = FEAT_RGB_EFFECTS, self.feat[FEAT_RGB_EFFECTS]
            elif self.feat[FEAT_COLOR_LED_EFFECTS]:
                self.page, self.rgb_idx = FEAT_COLOR_LED_EFFECTS, self.feat[FEAT_COLOR_LED_EFFECTS]
            else:
                raise DeviceError(f"{name}: no 0x8071 / 0x8070 lighting feature")
            self._discover_clusters()
            if not self.clusters:
                raise DeviceError(f"{name}: lighting feature reports no clusters")
            if self.feat[FEAT_PER_KEY_8081]:
                log.info("%s also exposes per-LED lighting (0x8081), not implemented: using 0x%04X clusters only",
                         name, self.page)
        except DeviceError:
            link.release()
            raise
        self.device = self._build_device(name, type_name, location, serial)
        log.info("%s: %d cluster(s) on feature 0x%04X, effects %s", name, len(self.clusters), self.page,
                 ["0x%04X" % e.effect_id for e in self.clusters[0].effects])

    # ---- transport helpers -----------------------------------------------------------------------
    def _fap(self, feat_idx: int, fn: int, data: bytes = b"", **kw) -> bytes:
        return self.link.fap(self.dev_idx, feat_idx, fn, data, **kw)

    def _get_feature_index(self, feature_id: int, **kw) -> int:
        # IRoot GetFeature: feature index 0x00 fn 0, payload = feature id big endian (LogitechProtocolCommon.cpp
        # logitech_device::getFeatureIndex); reply data[0] = index, 0 = not present
        return self._fap(0x00, 0x00, bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF]), **kw)[0]

    # ---- discovery -----------------------------------------------------------------------------
    def _read_name_type(self) -> Tuple[str, int]:
        """Feature 0x0005 DeviceNameType (LogitechHIDPP20Controller.cpp DiscoverDeviceName / DiscoverDeviceType)."""
        idx = self.feat.get(FEAT_DEVICE_NAME)
        if not idx:
            return "", 3                                         # OpenRGB assumes a mouse without 0x0005
        name, dev_type = "", 3
        try:
            n = self._fap(idx, FN_0005_GET_COUNT)[0]
            out = b""
            while 0 < n <= 64 and len(out) < n:
                out += self._fap(idx, FN_0005_GET_NAME, bytes([len(out)]))[:16]
            name = out[:n].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
            if not all(0x20 <= ord(c) <= 0x7E for c in name):
                name = ""
            dev_type = self._fap(idx, FN_0005_GET_TYPE)[0]
        except DeviceOffline:
            raise
        except DeviceError as e:
            log.debug("0x0005 name/type read failed: %s", e)
        return name, dev_type

    def _discover_clusters(self):
        """LogitechHIDPP20Controller.cpp DiscoverRGBEffects(): GetInfo -> per-cluster info -> per-effect info."""
        idx = self.rgb_idx
        if self.page == FEAT_RGB_EFFECTS:
            info = self._fap(idx, FN_8071_GET_INFO, bytes([0xFF, 0xFF, 0x00]))      # 0x8071 GetInfo(0xFF,0xFF,0)
            count = info[2]                                                           # numRgbZones
        else:
            info = self._fap(idx, FN_8070_INFO)                                       # 0x8070 getInfo()
            count = info[0]                                                           # zone count
        for i in range(min(count, 32)):
            try:
                if self.page == FEAT_RGB_EFFECTS:
                    r = self._fap(idx, FN_8071_GET_INFO, bytes([i, 0xFF]))            # GetRgbClusterInfo
                    location, n = (r[2] << 8) | r[3], r[4]
                else:
                    r = self._fap(idx, FN_8070_ZONE_INFO, bytes([i, 0x00]))           # getZoneInfo
                    location, n = (r[1] << 8) | r[2], r[3]
            except DeviceOffline:
                raise
            except DeviceError as e:
                log.warning("cluster %d info failed (%s), skipping it", i, e)
                continue
            cluster = Cluster(i, location)
            eff_fn = FN_8071_GET_INFO if self.page == FEAT_RGB_EFFECTS else FN_8070_EFFECT_INFO
            for j in range(min(n, 32)):
                try:
                    e = self._fap(idx, eff_fn, bytes([i, j, 0x00, 0x00]))             # GetEffectInfo(cluster, effect)
                except DeviceOffline:
                    raise
                except DeviceError as err:
                    log.warning("cluster %d effect %d info failed (%s)", i, j, err)
                    continue
                # [0..1] echo, [2..3] effect type id, [4..5] capabilities, [6..7] default period (ms)
                cluster.effects.append(Effect(j, (e[2] << 8) | e[3], (e[4] << 8) | e[5], (e[6] << 8) | e[7]))
            self.clusters.append(cluster)

    def _build_device(self, name: str, type_name: str, location: str, serial: str) -> Device:
        """Mode table per RGBController_LogitechHIDPP20.cpp constructor (effects of cluster 0; one LED per cluster;
        per-LED colors when there is more than one cluster)."""
        per_led = len(self.clusters) > 1
        is_8070 = self.page == FEAT_COLOR_LED_EFFECTS
        # 0x8070 writes are ephemeral unless saved (MANUAL_SAVE); 0x8071 writes persist on every mode set
        save_flag = MODE_FLAG_MANUAL_SAVE if is_8070 else MODE_FLAG_AUTOMATIC_SAVE
        modes: List[Mode] = []

        def add(mname: str, eff_id: int, flags: int, **kw):
            m = Mode(len(modes), mname, eff_id, flags, **kw)
            self.mode_effect[m.index] = eff_id
            modes.append(m)

        def colored(flags: int) -> dict:
            if per_led:
                return dict(flags=flags | MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)
            return dict(flags=flags | MODE_FLAG_HAS_MODE_SPECIFIC_COLOR, color_mode=MODE_COLORS_MODE_SPECIFIC,
                        colors_min=1, colors_max=1, colors=[DEFAULT_COLOR])

        speed = dict(speed_min=1, speed_max=100)
        bright = dict(brightness_min=1, brightness_max=100, brightness=100)
        direction = MODE_FLAG_HAS_DIRECTION_LR | MODE_FLAG_HAS_DIRECTION_UD | MODE_FLAG_HAS_DIRECTION_HV

        add("Direct", EFF_STATIC, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)
        add("Off", EFF_OFF, save_flag, color_mode=MODE_COLORS_NONE)
        for fx in self.clusters[0].effects:
            eid = fx.effect_id
            if eid == EFF_STATIC:
                c = colored(save_flag)
                add("Static", eid, c.pop("flags"), **c)
            elif eid in (EFF_CYCLE, EFF_CYCLE_SAT):
                add("Spectrum Cycle", eid, MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | save_flag, speed=80,
                    color_mode=MODE_COLORS_NONE, **speed, **bright)
            elif eid == EFF_BREATHING:
                c = colored(MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | save_flag)
                add("Breathing", eid, c.pop("flags"), speed=70, **speed, **bright, **c)
            elif eid == EFF_VISUALIZER:
                c = colored(MODE_FLAG_HAS_SPEED | save_flag)
                add("Audio Visualizer", eid, c.pop("flags"), speed=80, **speed, **c)
            elif eid in (EFF_WAVE, EFF_WAVE_SAT):
                add("Color Wave", eid, MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | direction | save_flag,
                    speed=80, direction=1, color_mode=MODE_COLORS_NONE, **speed, **bright)
            elif eid == EFF_RIPPLE:
                add("Ripple", eid, MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS
                    | save_flag, speed=70, colors_min=1, colors_max=1, color_mode=MODE_COLORS_MODE_SPECIFIC,
                    colors=[DEFAULT_COLOR], **speed, **bright)
            elif eid == EFF_RIPPLE_SAT:
                add("Ripple", eid, MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_SPEED | save_flag, speed=70,
                    colors_min=1, colors_max=1, color_mode=MODE_COLORS_MODE_SPECIFIC, colors=[DEFAULT_COLOR], **speed)
            elif eid in (EFF_DECOMPOSITION, EFF_SIGNATURE1, EFF_SIGNATURE2):
                label = {EFF_DECOMPOSITION: "Decomposition", EFF_SIGNATURE1: "Signature 1",
                         EFF_SIGNATURE2: "Signature 2"}[eid]
                add(label, eid, MODE_FLAG_HAS_SPEED | MODE_FLAG_HAS_BRIGHTNESS | save_flag, speed=80,
                    color_mode=MODE_COLORS_NONE, **speed, **bright)
            elif eid != EFF_OFF:
                log.info("%s: effect 0x%04X not mapped, ignored", name, eid)
        zones = [Zone(i, ZONE_LOCATION_NAMES.get(c.location, f"Zone 0x{c.location:04X}"), 0, 1, 1, 1, None, i)
                 for i, c in enumerate(self.clusters)]
        led_names = [z.name for z in zones]
        desc = f"Logitech HID++ 2.0 {type_name.lower()} (feature 0x{self.page:04X}, direct hidraw"
        desc += ", via receiver)" if self.wireless else ")"
        return Device(0, type_name, name, "Logitech", desc, "", serial, location, 0, modes, zones, led_names,
                      [0] * len(zones))

    # ---- software control claim / release ---------------------------------------------------------
    def _claim(self):
        """ClaimSWControlIfNeeded() (LogitechHIDPP20Controller.cpp).
        0x8070 zone-only devices: SetSWControl(1,1) and nothing else.
        0x8071: SetSWControl(3, flags 6) so the firmware engine keeps rendering until we paint, SetRGBPowerMode(1),
        WritePowerConfig(idle, sleep) so the lighting does not time out, then host mode; flags go 6 -> 5 after the
        first paint (UpgradeSwControlAfterFirstPaint)."""
        if self.claimed:
            return
        if self.page == FEAT_COLOR_LED_EFFECTS:
            self._fap(self.rgb_idx, FN_8070_SET_SW_CTL, bytes([0x01, 0x01]))                     # [enabled, persist]
        else:
            self._fap(self.rgb_idx, FN_8071_SW_CONTROL, bytes([0x01, 0x03, 0x06]))               # [set, mode 3, flags 6]
            self._fap(self.rgb_idx, FN_8071_PWR_MODE, bytes([0x01, 0x01]))                       # SetRGBPowerMode(1)
            self._fap(self.rgb_idx, FN_8071_PWR_CONFIG, _power_config(POWER_IDLE_S, POWER_SLEEP_S))
            self._set_host_mode(True)
            self.upgrade_pending = True
        self.claimed = True

    def _upgrade(self):
        """UpgradeSwControlAfterFirstPaint(): SetSWControl(3, 5) once the clusters show our colors."""
        if not self.upgrade_pending:
            return
        try:
            self._fap(self.rgb_idx, FN_8071_SW_CONTROL, bytes([0x01, 0x03, 0x05]))
            self.upgrade_pending = False
        except DeviceError as e:
            log.debug("SW control upgrade to flags=5 failed (%s), will retry on the next frame", e)

    def _set_host_mode(self, host: bool):
        """SetHostMode() / Shutdown(): 0x8101 setMode(5 host / 3 onboard) or 0x8100 setOnboardMode(2 host / 1 onboard).
        Best effort — profile features on some firmware reject the switch and lighting still works."""
        try:
            if self.feat.get(FEAT_PROFILE_MANAGEMENT):
                self._fap(self.feat[FEAT_PROFILE_MANAGEMENT], FN_8101_GET_SET_MODE, bytes([0x05 if host else 0x03]))
            elif self.feat.get(FEAT_ONBOARD_PROFILES):
                self._fap(self.feat[FEAT_ONBOARD_PROFILES], FN_8100_SET_ONBOARD_MODE, bytes([0x02 if host else 0x01]))
        except DeviceOffline:
            raise
        except DeviceError as e:
            log.debug("host/onboard mode switch failed: %s", e)

    def _release(self):
        """Shutdown(): SetSWControl(0,0) and back to onboard profiles."""
        if not self.claimed:
            return
        self.claimed = False
        try:
            if self.page == FEAT_COLOR_LED_EFFECTS:
                self._fap(self.rgb_idx, FN_8070_SET_SW_CTL, bytes([0x00, 0x00]))
            else:
                self._fap(self.rgb_idx, FN_8071_SW_CONTROL, bytes([0x01, 0x00, 0x00]))
                self._set_host_mode(False)
        except DeviceError as e:
            log.debug("release failed: %s", e)

    # ---- effect packet -----------------------------------------------------------------------------
    def _set_effect(self, cluster_idx: int, eff_idx: int, eff_id: int, color: int, period: int, brightness: int,
                    direction: int, persist: bool):
        payload = effect_payload(cluster_idx, eff_idx, eff_id, color, period, brightness, direction, persist)
        fn = FN_8071_SET_EFFECT if self.page == FEAT_RGB_EFFECTS else FN_8070_SET_EFFECT
        self._fap(self.rgb_idx, fn, payload)

    # ---- driver contract -----------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        """Per-zone colors. Direct rides the Static/Fixed effect per cluster (RGBController_LogitechHIDPP20.cpp
        DeviceUpdateLEDs zone path); other per-LED modes (Static/Breathing on multi-cluster devices) re-issue the
        active effect with the cluster's color. Only clusters whose color changed are written."""
        colors = list(colors)[:len(self.clusters)]
        colors += [0] * (len(self.clusters) - len(colors))
        mode = self.device.current_mode
        if mode.color_mode != MODE_COLORS_PER_LED:
            self.device.colors = colors                  # an effect without per-zone colors is running: nothing to push
            return
        self._claim()
        eff_id = self.mode_effect.get(mode.index, EFF_STATIC)
        period = _period_for(eff_id, mode.speed)
        brightness = _brightness_for(mode)
        full = len(self.sent) != len(colors)
        for i, cluster in enumerate(self.clusters):
            if not full and self.sent[i] == colors[i]:
                continue
            fx = cluster.find(eff_id)
            if fx is None:
                continue
            self._set_effect(cluster.index, fx.index, eff_id, colors[i], period, brightness,
                             WAVE_DIR_WIRE.get(mode.direction, 1), False)
        self.sent = list(colors)
        self.device.colors = list(colors)
        self._upgrade()

    def set_mode(self, mode: Mode, save: bool = False):
        """RGBController_LogitechHIDPP20.cpp DeviceUpdateMode(): Off = Static black on every cluster; effects are
        applied to every cluster with the effect's index in that cluster. Persist byte: 0x8070 only when saving,
        0x8071 always (what the vendor app sends; persist=0 effects were not visibly applied)."""
        self.device.active_mode = mode.index
        self.sent = []
        self._claim()
        if mode.index == 0:                            # Direct: push the zone buffer through Static
            self.set_colors(self.device.colors)
            return
        is_8070 = self.page == FEAT_COLOR_LED_EFFECTS
        eff_id = self.mode_effect.get(mode.index, EFF_OFF)
        if eff_id == EFF_OFF:
            persist = save if is_8070 else False
            for cluster in self.clusters:
                fx = cluster.find(EFF_STATIC)
                if fx is not None:
                    self._set_effect(cluster.index, fx.index, EFF_STATIC, 0, 0, 100, 0, persist)
            self._upgrade()
            return
        period = _period_for(eff_id, mode.speed)
        brightness = _brightness_for(mode)
        persist = save if is_8070 else True
        for i, cluster in enumerate(self.clusters):
            fx = cluster.find(eff_id)
            if fx is None:
                log.debug("cluster %d has no effect 0x%04X", cluster.index, eff_id)
                continue
            if mode.color_mode == MODE_COLORS_PER_LED:
                color = self.device.colors[i] if i < len(self.device.colors) else 0
            elif mode.color_mode == MODE_COLORS_MODE_SPECIFIC and mode.colors:
                color = mode.colors[0]
            else:
                color = 0
            self._set_effect(cluster.index, fx.index, eff_id, color, period, brightness,
                             WAVE_DIR_WIRE.get(mode.direction, 1), persist)
        if mode.color_mode == MODE_COLORS_PER_LED:
            self.sent = list(self.device.colors)
        self._upgrade()

    def battery(self) -> Optional[Tuple[int, bool]]:
        """(percent, on external power) or None. Feature 0x1004 UnifiedBattery GetStatus as read by
        LogitechHIDPP20Controller.cpp QueryExternalPower(): [state of charge %, level flags, charging status,
        external power]; external if either of the last two is non-zero. Fallback 0x1000 BatteryLevelStatus
        [level %, next level, status] with status 1..4 = charging (public HID++ 2.0 spec, not in OpenRGB)."""
        try:
            idx = self.feat.get(FEAT_UNIFIED_BATTERY)
            if idx:
                r = self._fap(idx, FN_1004_GET_STATUS)
                return r[0], bool(r[2] or r[3])
            idx = self.feat.get(FEAT_BATTERY_STATUS)
            if idx:
                r = self._fap(idx, FN_1000_GET_LEVEL_STATUS)
                return r[0], r[2] in (1, 2, 3, 4)
        except DeviceError as e:
            log.debug("battery read failed: %s", e)
        return None

    def close(self):
        try:
            self._release()
        finally:
            self.link.release()


# ---- packet builders (module level so the unit test can check them byte for byte) -----------------------
def effect_payload(cluster_idx: int, eff_idx: int, eff_id: int, color: int, period: int, brightness: int,
                   direction: int, persist: bool) -> bytes:
    """16-byte SetEffect payload, verbatim port of LogitechHIDPP20Controller.cpp SetZoneEffect():
    [cluster, effect index, 10 bytes of effect params at data[2..11], persist at data[12]]."""
    r, g, b = u32_to_rgb(color)
    d = bytearray(16)
    d[0], d[1] = cluster_idx & 0xFF, eff_idx & 0xFF
    hi, lo = (period >> 8) & 0xFF, period & 0xFF
    bright = brightness & 0xFF
    if eff_id == EFF_STATIC:
        d[2:5] = (r, g, b)
        if r or g or b:
            d[5] = 0x02                       # "fixed color" marker; all black stays 0 = off
    elif eff_id == EFF_BREATHING:
        d[2:5] = (r, g, b)
        d[5], d[6], d[8] = hi, lo, bright
    elif eff_id == EFF_CYCLE:
        d[7], d[8], d[9] = hi, lo, bright
    elif eff_id == EFF_VISUALIZER:
        if r or g or b:
            d[2:6] = (0x00, r, g, b)
        else:
            d[2] = 0x01
        d[7], d[8] = hi, lo
    elif eff_id == EFF_WAVE:
        d[8], d[9], d[10], d[11] = lo, direction & 0xFF, bright or 1, hi
    elif eff_id == EFF_RIPPLE:
        d[2:5] = (r, g, b)
        d[6], d[7] = hi, lo
    elif eff_id == EFF_DECOMPOSITION:
        d[8], d[9], d[10] = hi, lo, bright
    elif eff_id in (EFF_SIGNATURE1, EFF_SIGNATURE2):
        d[7], d[8], d[9] = hi, lo, bright
    elif eff_id == EFF_CYCLE_SAT:
        d[3], d[8], d[9], d[10] = 0xFF, hi, lo, bright
    elif eff_id == EFF_WAVE_SAT:
        d[3], d[8], d[9], d[10], d[11] = 0xFF, hi, lo, bright, direction & 0xFF
    elif eff_id == EFF_RIPPLE_SAT:
        d[2:5] = (r, g, b)
        d[5], d[8], d[9] = 0xFF, hi, lo
    else:                                     # unknown effect: best effort, color + period
        d[2:5] = (r, g, b)
        d[5], d[6] = hi, lo
    d[12] = 0x01 if persist else 0x00
    return bytes(d)


def _power_config(idle_s: int, sleep_s: int) -> bytes:
    """LogitechHIDPP20Controller.cpp WritePowerConfig(): SetRgbPowerModeConfig sub 0x01 (set),
    [0x01, 0, 0, idle_hi, idle_lo, sleep_hi, sleep_lo, 0 x 9]."""
    d = bytearray(16)
    d[0] = 0x01
    d[3], d[4] = (idle_s >> 8) & 0xFF, idle_s & 0xFF
    d[5], d[6] = (sleep_s >> 8) & 0xFF, sleep_s & 0xFF
    return bytes(d)


def _period_for(eff_id: int, slider: int) -> int:
    """Speed slider 1..100 -> period ms: 20000..1000, Ripple 200..2 (RGBController_LogitechHIDPP20.cpp
    SpeedSliderToPeriodMs / RippleSpeedSliderToPeriodMs)."""
    if eff_id in (EFF_RIPPLE, EFF_RIPPLE_SAT):
        return _slider_to_period(slider or 70, RIPPLE_MIN, RIPPLE_MAX)
    return _slider_to_period(slider or 80, PERIOD_MIN, PERIOD_MAX)


def _brightness_for(mode: Mode) -> int:
    return max(1, min(100, mode.brightness or 100)) if mode.has_brightness else 100


# ---- probing ---------------------------------------------------------------------------------------
def is_hidpp_interface(info: HidInfo) -> bool:
    """The HID++ collection: vendor usage page 0xFF00 (mice, receivers) or 0xFF43 (headsets) with the 0x11 long report.
    Matches what LogitechControllerDetect.cpp selects (0xFF00 usage 2 / 0xFF43 usage 0x0202). Nodes without a readable
    descriptor fall back to OpenRGB's interface numbers (1 mice, 2 receivers, 3 headsets)."""
    if info.descriptor:
        vendor_page = b"\x06\x00\xff" in info.descriptor or b"\x06\x43\xff" in info.descriptor
        return vendor_page and b"\x85\x11" in info.descriptor
    if info.pid in RECEIVERS:
        return info.interface == 2
    return info.interface == (3 if WIRED.get(info.pid, ("", ""))[1] == T_HEADSET else 1)


def open_wired(link: HidppLink, pid: int) -> LogitechHidppDevice:
    name, type_name = WIRED.get(pid, ("Logitech HID++ device", T_MOUSE))
    return LogitechHidppDevice(link, DEV_IDX_WIRED, pid, name, type_name, "HID: " + link.path)


def open_receiver(link: HidppLink, pid: int) -> List[LogitechHidppDevice]:
    """One driver per paired slot that is awake; sleeping or absent slots are logged and skipped (rescan wakes them)."""
    slots = enumerate_receiver(link)
    if not slots:
        log.info("%s at %s: no paired devices", RECEIVERS.get(pid, "receiver"), link.path)
        return []
    out = []
    for idx, wpid, online in slots:
        codename = receiver_slot_name(link, idx)
        fallback = WIRELESS_PID_NAMES.get(wpid) or (f"Logitech {codename}" if codename else "Logitech wireless device")
        if not online:
            log.info("%s slot %d (%s, wireless pid %04x) is asleep or off; skipped until the next rescan",
                     link.path, idx, fallback, wpid)
            continue
        fallback_type = T_MOUSEMAT if pid == 0xC53A or wpid == 0x405F else T_MOUSE
        try:
            dev = LogitechHidppDevice(link, idx, wpid, fallback, fallback_type,
                                      f"HID: {link.path} (receiver slot {idx})", receiver_slot_serial(link, idx))
        except DeviceOffline as e:
            log.info("%s slot %d (%s): %s", link.path, idx, fallback, e)
            continue
        except DeviceError as e:
            log.info("%s slot %d (%s) skipped: %s", link.path, idx, fallback, e)
            continue
        out.append(dev)
    return out


def probe() -> List[LogitechHidppDevice]:
    """Every supported Logitech mouse / mat / headset reachable right now. Never raises."""
    found: List[LogitechHidppDevice] = []
    done_pids: set = set()
    for info in enumerate_hid(VID_LOGITECH):
        try:
            if info.pid not in WIRED and info.pid not in RECEIVERS:
                continue
            if info.pid in done_pids or not is_hidpp_interface(info):
                continue
            try:
                hid = HidDevice(info.path)
            except DeviceError as e:
                log.info("skipping %s: %s", info.path, e)
                continue
            link = HidppLink(hid, wireless=info.pid in RECEIVERS)
            try:
                if info.pid in RECEIVERS:
                    found += open_receiver(link, info.pid)
                else:
                    found.append(open_wired(link, info.pid))
                    done_pids.add(info.pid)
            except DeviceError as e:
                log.info("skipping %s (%04x): %s", info.path, info.pid, e)
            finally:
                if link.users <= 0:
                    hid.close()
        except Exception:  # noqa: BLE001 - probe must never take the backend down
            log.exception("probe failed on %s", info.path)
    return found
