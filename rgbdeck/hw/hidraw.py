"""Small /dev/hidraw helper: enumeration by VID/PID, output reports, feature reports."""

from __future__ import annotations

import fcntl
import glob
import os
import select
from dataclasses import dataclass
from typing import List, Optional

from rgbdeck.models import DeviceError

# linux/hidraw.h: HIDIOCSFEATURE(len) = _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, typ: int, nr: int, size: int) -> int:
    return (direction << _IOC_DIRSHIFT) | (typ << _IOC_TYPESHIFT) | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT)


def HIDIOCSFEATURE(n: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, ord("H"), 0x06, n)


def HIDIOCGFEATURE(n: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, ord("H"), 0x07, n)


@dataclass
class HidInfo:
    path: str  # /dev/hidrawN
    vid: int
    pid: int
    name: str
    interface: int  # USB interface number, -1 if unknown
    descriptor: bytes


def enumerate_hid(vid: Optional[int] = None, pid: Optional[int] = None) -> List[HidInfo]:
    out = []
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(sysdir, "device", "uevent")).read()
        except OSError:
            continue
        fields = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        hid_id = fields.get("HID_ID", "")
        try:
            _bus, v, p = hid_id.split(":")
            v, p = int(v, 16), int(p, 16)
        except ValueError:
            continue
        if (vid is not None and v != vid) or (pid is not None and p != pid):
            continue
        name = fields.get("HID_NAME", "")
        iface = -1
        try:
            real = os.path.realpath(os.path.join(sysdir, "device"))
            # .../usb1/1-3/1-3:1.1/0003:046D:C33C.0005  -> interface 1
            parent = os.path.basename(os.path.dirname(real))
            if ":" in parent and "." in parent:
                iface = int(parent.rsplit(".", 1)[1])
        except (OSError, ValueError):
            pass
        try:
            desc = open(os.path.join(sysdir, "device", "report_descriptor"), "rb").read()
        except OSError:
            desc = b""
        out.append(HidInfo("/dev/" + os.path.basename(sysdir), v, p, name, iface, desc))
    return out


class HidDevice:
    def __init__(self, path: str):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise DeviceError(
                f"can't open {path}: {e.strerror}. Your user needs access to this device "
                f"(install the udev rule from the RGB Deck folder)."
            ) from e

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write(self, data: bytes) -> int:
        try:
            return os.write(self.fd, data)
        except OSError as e:
            raise DeviceError(f"write to {self.path} failed: {e.strerror}") from e

    def read(self, size: int = 64, timeout: float = 0.5) -> Optional[bytes]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return None
        except OSError as e:
            raise DeviceError(f"read from {self.path} failed: {e.strerror}") from e

    def drain(self):
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return
            try:
                if not os.read(self.fd, 256):
                    return
            except (BlockingIOError, OSError):
                return

    def send_feature(self, data: bytes):
        buf = bytearray(data)
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as e:
            raise DeviceError(f"feature report to {self.path} failed: {e.strerror}") from e

    def get_feature(self, report_id: int, size: int) -> bytes:
        buf = bytearray(size)
        buf[0] = report_id
        try:
            fcntl.ioctl(self.fd, HIDIOCGFEATURE(size), buf, True)
        except OSError as e:
            raise DeviceError(f"feature read from {self.path} failed: {e.strerror}") from e
        return bytes(buf)
