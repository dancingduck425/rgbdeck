# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
# Register layout and access sequence derived from OpenRGB (https://gitlab.com/CalcProgrammer1/OpenRGB),
# Copyright (C) the OpenRGB contributors, GPL-2.0-or-later.
"""
Lights on ASUS graphics cards with an NVIDIA chip (ROG Strix, TUF, KO... of the RTX 30, 40 and 50 series).

The card carries a small lighting controller (ENE) on an I2C line that belongs to the graphics chip itself, so
the only way to it is through the graphics driver:

    Windows   nvapi64.dll, which comes with the NVIDIA driver (I2C port 1 of the card); nothing to install
    Linux     /dev/i2c-N of the "NVIDIA i2c adapter 1" (needs the i2c-dev module and access to that file)

How careful this is, because an I2C line is shared with other parts of the card:

  * only cards whose maker is ASUS (PCI subsystem vendor 1043) on an NVIDIA chip are looked at;
  * only address 0x67 is spoken to, the one ASUS uses for this controller;
  * first come sixteen plain reads that must return the controller's fixed pattern (0, 1, ... 15);
  * then its version text is read and must be one of the versions known from graphics cards.

Only after all four is anything about lights written. Nothing else on the card is touched.

Not tried on a real card yet: experimental. Armoury Crate (its "LightingService") writes to the same controller;
with both running, whoever wrote last wins, so close Armoury Crate's lighting or set it to a static look.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from rgbdeck.models import (
    Device,
    DeviceError,
    Mode,
    Zone,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_MANUAL_SAVE,
    u32_to_rgb,
)

log = logging.getLogger("rgbdeck.asusgpu")

EXPERIMENTAL = True
USB_IDS: list = []  # not a USB device

NVIDIA, ASUS = 0x10DE, 0x1043
ADDRESS = 0x67
PORT = 1  # the card's I2C port the lighting controller sits on

REG_NAME = 0x1000  # 16 characters
REG_CONFIG = 0x1C00  # 64 bytes
REG_DIRECT = 0x8020  # 1: colors come from REG_COLORS_DIRECT
REG_MODE = 0x8021
REG_APPLY = 0x80A0
REG_COLORS_DIRECT = 0x8100
REG_COLORS_EFFECT = 0x8160
APPLY, SAVE = 0x01, 0xAA
CONFIG_LED_COUNT = 0x03
MAX_LEDS = 32  # room in each color table
BLOCK = 3  # bytes per block write

# Controller versions seen on graphics cards (all use the same registers)
VERSIONS = {
    "AUMA0-E6K5-0107",
    "AUMA0-E6K5-1107",
    "AUMA0-E6K5-1110",
    "AUMA0-E6K5-1111",
    "AUMA0-E6K5-1113",
    "AUMA0-E6K5-1114",
    "AUMA0-E6K5-0008",
}
MODE_DIRECT = 0xFFFF
EFFECTS = [
    ("Off", 0),
    ("Static", 1),
    ("Breathing", 2),
    ("Flashing", 3),
    ("Spectrum Cycle", 4),
    ("Rainbow", 5),
    ("Spectrum Cycle Breathing", 6),
    ("Chase Fade", 7),
    ("Spectrum Cycle Chase Fade", 8),
    ("Chase", 9),
    ("Spectrum Cycle Chase", 10),
    ("Spectrum Cycle Wave", 11),
    ("Chase Rainbow Pulse", 12),
    ("Random Flicker", 13),
]
COLOR_EFFECTS = {1, 2, 3, 7, 9}


# ----------------------------------------------------------------------------- the I2C line
class Bus:
    """One card's I2C line. Every method raises DeviceError when the card does not answer."""

    key = ""  # stable for this card while the program runs
    label = ""  # for the log and the device's location
    name = ""  # the card's name, if the system knows it
    ids = (0, 0, 0, 0)  # PCI vendor, device, subsystem vendor, subsystem device

    def read_byte_data(self, addr: int, cmd: int) -> int:
        raise NotImplementedError

    def write_byte_data(self, addr: int, cmd: int, value: int):
        raise NotImplementedError

    def write_word_data(self, addr: int, cmd: int, value: int):
        raise NotImplementedError

    def write_block_data(self, addr: int, cmd: int, data: bytes):
        raise NotImplementedError

    def close(self):
        pass


class _NvI2CInfo(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("display_mask", ctypes.c_uint32),
        ("is_ddc_port", ctypes.c_uint8),
        ("dev_address", ctypes.c_uint8),
        ("reg_address", ctypes.POINTER(ctypes.c_uint8)),
        ("reg_addr_size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("size", ctypes.c_uint32),
        ("speed", ctypes.c_uint32),
        ("speed_khz", ctypes.c_uint32),
        ("port_id", ctypes.c_uint8),
        ("is_port_id_set", ctypes.c_uint32),
    ]


class _Nvapi:
    """The few calls needed out of the NVIDIA driver's library (looked up by their numbers, as the library has
    no named exports)."""

    def __init__(self):
        lib = ctypes.WinDLL("nvapi64.dll" if ctypes.sizeof(ctypes.c_void_p) == 8 else "nvapi.dll")  # type: ignore[attr-defined]
        query = lib.nvapi_QueryInterface
        query.restype = ctypes.c_void_p
        query.argtypes = [ctypes.c_uint32]

        def fn(number: int, *args):
            address = query(number)
            if not address:
                raise OSError(f"the NVIDIA driver has no function {number:#x}")
            return ctypes.CFUNCTYPE(ctypes.c_int32, *args)(address)

        u32p = ctypes.POINTER(ctypes.c_uint32)
        self._lib = lib
        self.initialize = fn(0x0150E828)
        self.enum_gpus = fn(0xE5AC921F, ctypes.POINTER(ctypes.c_void_p * 64), ctypes.POINTER(ctypes.c_int32))
        self.pci_ids = fn(0x2DDFB66E, ctypes.c_void_p, u32p, u32p, u32p, u32p)
        self.full_name = fn(0xCEEE8E9F, ctypes.c_void_p, ctypes.c_char_p)
        self.i2c_write = fn(0x283AC65A, ctypes.c_void_p, ctypes.POINTER(_NvI2CInfo), u32p)
        self.i2c_read = fn(0x4D7B0709, ctypes.c_void_p, ctypes.POINTER(_NvI2CInfo), u32p)
        status = self.initialize()
        if status != 0:
            raise OSError(f"the NVIDIA driver did not start its interface (status {status})")


class NvapiBus(Bus):
    def __init__(self, api: _Nvapi, handle, index: int):
        self.api, self.handle = api, handle
        self.key = f"nvapi:{index}"
        self.label = f"NVIDIA driver, graphics card {index + 1}"
        dev, sub, rev, ext = (ctypes.c_uint32() for _ in range(4))
        if api.pci_ids(handle, dev, sub, rev, ext) == 0:
            self.ids = (dev.value & 0xFFFF, dev.value >> 16, sub.value & 0xFFFF, sub.value >> 16)
        text = ctypes.create_string_buffer(64)
        if api.full_name(handle, text) == 0:
            self.name = text.value.decode(errors="replace").strip()

    def _transfer(self, write: bool, addr: int, cmd: int, payload: bytes, count: int) -> bytes:
        reg = (ctypes.c_uint8 * 1)(cmd)
        buf = (ctypes.c_uint8 * 40)(*payload)
        info = _NvI2CInfo()
        info.version = ctypes.sizeof(_NvI2CInfo) | (3 << 16)
        info.dev_address = (addr << 1) & 0xFF
        info.reg_address = ctypes.cast(reg, ctypes.POINTER(ctypes.c_uint8))
        info.reg_addr_size = 1
        info.data = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        info.size = count
        info.speed = 0xFFFF  # "use speed_khz", which stays at the driver's default
        info.port_id = PORT
        info.is_port_id_set = 1
        unknown = ctypes.c_uint32(0)
        status = (self.api.i2c_write if write else self.api.i2c_read)(self.handle, info, unknown)
        if status != 0:
            raise DeviceError(f"the graphics card did not answer on its lighting line (status {status})")
        return bytes(buf[:count])

    def read_byte_data(self, addr, cmd):
        return self._transfer(False, addr, cmd, b"", 1)[0]

    def write_byte_data(self, addr, cmd, value):
        self._transfer(True, addr, cmd, bytes([value & 0xFF]), 1)

    def write_word_data(self, addr, cmd, value):
        self._transfer(True, addr, cmd, bytes([value & 0xFF, (value >> 8) & 0xFF]), 2)

    def write_block_data(self, addr, cmd, data):
        self._transfer(True, addr, cmd, bytes([len(data)]) + bytes(data), len(data) + 1)


class _SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _SmbusIoctl(ctypes.Structure):
    _fields_ = [
        ("read_write", ctypes.c_uint8),
        ("command", ctypes.c_uint8),
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(_SmbusData)),
    ]


class LinuxBus(Bus):
    I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
    BYTE_DATA, WORD_DATA, BLOCK_DATA = 2, 3, 5

    def __init__(self, number: int, ids, label: str):
        self.key = f"i2c-{number}"
        self.label = f"/dev/i2c-{number} ({label})"
        self.ids = ids
        try:
            self.fd = os.open(f"/dev/i2c-{number}", os.O_RDWR)
        except OSError as e:
            raise DeviceError(f"/dev/i2c-{number} cannot be opened ({e.strerror})") from e
        self._addr = None

    def _call(self, addr: int, read: bool, cmd: int, size: int, data: _SmbusData):
        import fcntl

        try:
            if self._addr != addr:
                fcntl.ioctl(self.fd, self.I2C_SLAVE, addr)
                self._addr = addr
            fcntl.ioctl(self.fd, self.I2C_SMBUS, _SmbusIoctl(1 if read else 0, cmd, size, ctypes.pointer(data)))
        except OSError as e:
            raise DeviceError(f"the graphics card did not answer on its lighting line ({e.strerror})") from e

    def read_byte_data(self, addr, cmd):
        data = _SmbusData()
        self._call(addr, True, cmd, self.BYTE_DATA, data)
        return data.byte

    def write_byte_data(self, addr, cmd, value):
        data = _SmbusData()
        data.byte = value & 0xFF
        self._call(addr, False, cmd, self.BYTE_DATA, data)

    def write_word_data(self, addr, cmd, value):
        data = _SmbusData()
        data.word = value & 0xFFFF
        self._call(addr, False, cmd, self.WORD_DATA, data)

    def write_block_data(self, addr, cmd, data):
        block = _SmbusData()
        block.block[0] = len(data)
        for i, b in enumerate(bytes(data)):
            block.block[1 + i] = b
        self._call(addr, False, cmd, self.BLOCK_DATA, block)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def _wanted(ids) -> bool:
    return ids[0] == NVIDIA and ids[2] == ASUS


def _windows_buses() -> List[Bus]:
    try:
        api = _Nvapi()
    except (OSError, AttributeError) as e:
        log.debug("no NVIDIA driver interface: %s", e)
        return []
    handles = (ctypes.c_void_p * 64)()
    count = ctypes.c_int32(0)
    if api.enum_gpus(handles, count) != 0:
        return []
    return [NvapiBus(api, handles[i], i) for i in range(min(count.value, 64))]


def _linux_buses(sys_root: str = "/sys/bus/i2c/devices") -> List[Bus]:
    out: List[Bus] = []

    def number(path: Path) -> int:
        try:
            return int(path.name[4:])
        except ValueError:
            return -1

    for entry in sorted(Path(sys_root).glob("i2c-*"), key=number):
        try:
            label = (entry / "name").read_text().strip()
            if not label.startswith(f"NVIDIA i2c adapter {PORT} "):
                continue
            pci = (entry / "device").resolve()
            ids = tuple(
                int((pci / f).read_text().strip(), 16)
                for f in ("vendor", "device", "subsystem_vendor", "subsystem_device")
            )
        except (OSError, ValueError):
            continue
        if not _wanted(ids):
            continue  # before the device file is even opened
        try:
            out.append(LinuxBus(number(entry), ids, label))
        except DeviceError as e:
            log.info("ASUS graphics card %04x:%04x found, but its lighting line is out of reach: %s", ids[1], ids[3], e)
    return out


def buses() -> List[Bus]:
    if os.environ.get("RGBDECK_NO_GPU"):
        return []
    if sys.platform == "win32":
        return _windows_buses()
    if sys.platform.startswith("linux"):
        return _linux_buses()
    return []


# ----------------------------------------------------------------------------- the lighting controller
def _select(bus: Bus, reg: int):
    bus.write_word_data(ADDRESS, 0x00, ((reg << 8) & 0xFF00) | ((reg >> 8) & 0x00FF))


def reg_read(bus: Bus, reg: int) -> int:
    _select(bus, reg)
    return bus.read_byte_data(ADDRESS, 0x81)


def reg_write(bus: Bus, reg: int, value: int):
    _select(bus, reg)
    bus.write_byte_data(ADDRESS, 0x01, value)


def reg_write_block(bus: Bus, reg: int, data: bytes):
    _select(bus, reg)
    bus.write_block_data(ADDRESS, 0x03, data)


def identify(bus: Bus) -> Optional[dict]:
    """Is the lighting controller there? Reads only, until its fixed pattern has been seen. Returns its
    version text and number of lights, or None (with the reason in the log)."""
    card = "ASUS graphics card %04x:%04x" % (bus.ids[1], bus.ids[3])
    try:
        for i in range(16):
            got = bus.read_byte_data(ADDRESS, 0xA0 + i)
            if got != i:
                log.info("%s: no lighting controller at 0x%02x (read %d of 16 gave %#x)", card, ADDRESS, i + 1, got)
                return None
        version = bytes(reg_read(bus, REG_NAME + i) for i in range(16)).split(b"\0", 1)[0].decode("ascii", "replace")
        if version not in VERSIONS:
            log.info("%s: lighting controller %r is not a known one, left alone", card, version)
            return None
        config = bytes(reg_read(bus, REG_CONFIG + i) for i in range(64))
    except DeviceError as e:
        log.info("%s: %s", card, e)
        return None
    count = config[CONFIG_LED_COUNT]
    log.info("%s: lighting controller %s, %d lights, config %s", card, version, count, config.hex())
    if not 1 <= count <= MAX_LEDS:
        log.info("%s: %d lights makes no sense, left alone", card, count)
        return None
    return {"version": version, "leds": count}


class AsusGpu:
    def __init__(self, bus: Bus, found: dict):
        self.bus = bus
        self.num_leds = int(found["leds"])
        self._sent: List[Optional[int]] = [None] * self.num_leds  # what the card was last told, per light
        self._direct = False
        self._line = threading.Lock()  # one conversation with the card at a time
        self._cv = threading.Condition()
        self._target: Optional[List[int]] = None
        self._thread: Optional[threading.Thread] = None
        self._busy = self._stop = self._failing = False
        self.device = self._build(found["version"])

    def _build(self, version: str) -> Device:
        modes = [Mode(0, "Direct", MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)]
        for name, value in EFFECTS:
            if value in COLOR_EFFECTS:
                modes.append(
                    Mode(
                        len(modes),
                        name,
                        value,
                        MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_MANUAL_SAVE,
                        colors_min=1,
                        colors_max=1,
                        color_mode=MODE_COLORS_MODE_SPECIFIC,
                        colors=[0x00B4FF],
                    )
                )
            else:
                modes.append(Mode(len(modes), name, value, MODE_FLAG_MANUAL_SAVE))
        n = self.num_leds
        name = self.bus.name or "graphics card"
        return Device(
            0,
            "GPU",
            name if name.upper().startswith("ASUS") else f"ASUS {name}",
            "ASUS",
            "ASUS graphics card lighting (through the graphics driver)",
            version,
            "",
            "I2C: " + self.bus.label,
            0,
            modes,
            [Zone(0, "Graphics card", 1 if n > 1 else 0, n, n, n, None, 0)],
            [f"Graphics card, LED {i + 1}" for i in range(n)],
            [0] * n,
        )

    def _table(self, base: int, colors: List[int], sent: Optional[List[Optional[int]]]):
        """Write colors into one of the two color tables, one light per block (red, blue, green). With `sent`,
        lights that already have their color are skipped: an animation that moves a few lights costs a few
        writes, not the whole card."""
        for i, color in enumerate(colors[: self.num_leds]):
            if sent is not None and sent[i] == color:
                continue
            r, g, b = u32_to_rgb(color)
            reg_write_block(self.bus, base + BLOCK * i, bytes([r, b, g]))
            if sent is not None:
                sent[i] = color

    # Colors are written by a thread of this card's own. The line through the graphics driver is slow (two
    # transfers per light), and the program's one writer thread also serves the keyboard: an animation on the
    # card must not hold that up. The thread always writes the newest colors asked for and skips what it missed.
    def set_colors(self, colors: List[int]):
        colors = list(colors)[: self.num_leds]
        self.device.colors[: len(colors)] = colors
        with self._cv:
            self._target = colors
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rgbdeck-gpu", daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def _run(self):
        while True:
            with self._cv:
                while self._target is None and not self._stop:
                    self._cv.wait()
                if self._target is None:
                    return  # stopping, and nothing left to write
                colors, self._target = self._target, None
                self._busy = True
            try:
                with self._line:
                    self._write_direct(colors)
                self._failing = False
            except DeviceError as e:
                if not self._failing:
                    log.warning("%s: %s", self.device.name, e)
                self._failing = True
            except Exception:  # noqa: BLE001 - never let this thread die quietly
                log.exception("graphics card lights")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def wait(self, timeout: float = 3.0) -> bool:
        """Until the colors asked for are on the card."""
        with self._cv:
            return self._cv.wait_for(lambda: self._target is None and not self._busy, timeout)

    def _write_direct(self, colors: List[int]):
        try:
            if not self._direct:
                reg_write(self.bus, REG_DIRECT, 1)
                reg_write(self.bus, REG_APPLY, APPLY)
                self._direct = True
                self._sent = [None] * self.num_leds
            self._table(REG_COLORS_DIRECT, colors, self._sent)
        except DeviceError:
            self._direct = False
            raise

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == MODE_DIRECT:
            with self._line:
                self._direct = False  # say so again: another program may have switched it away
            self.set_colors(self.device.colors)
            self.wait()
            return
        with self._cv:
            self._target = None  # colors still waiting belong to the look that is being replaced
        color = mode.colors[0] if mode.colors else 0
        with self._line:
            self._direct = False
            reg_write(self.bus, REG_DIRECT, 0)
            reg_write(self.bus, REG_MODE, mode.value)
            self._table(REG_COLORS_EFFECT, [color] * self.num_leds, None)
            reg_write(self.bus, REG_APPLY, APPLY)
            if save:
                reg_write(self.bus, REG_APPLY, SAVE)  # the card keeps it without any program running

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(3.0)  # it writes what is still waiting, then ends
        with self._line:
            self.bus.close()


# What was found on each card, so that the questions are asked once and not at every rescan (a graphics card
# is not plugged in or out while the program runs). None: looked, nothing there.
_known: Dict[str, Optional[dict]] = {}


def probe(found_buses: Optional[List[Bus]] = None) -> List[AsusGpu]:
    out = []
    for bus in buses() if found_buses is None else found_buses:
        if not _wanted(bus.ids):
            bus.close()
            continue
        if bus.key not in _known:
            _known[bus.key] = identify(bus)
        found = _known[bus.key]
        if found is None:
            bus.close()
            continue
        out.append(AsusGpu(bus, found))
    return out
