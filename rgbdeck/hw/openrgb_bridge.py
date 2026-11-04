"""
Optional bridge: devices RGB Deck has no native driver for can be driven through an OpenRGB SDK
server. Off unless enabled in Settings. When used, OpenRGB is started with a *private* config
directory in which the detectors for natively-driven families are disabled, so the two never
touch the same hardware.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from rgbdeck.models import Device, DeviceError, Mode, Zone

log = logging.getLogger("rgbdeck.openrgb")

PROTOCOL_VERSION = 4
HOST, PORT = "127.0.0.1", 6742
# detectors handled natively by rgbdeck.hw.* — kept off in the bridge's OpenRGB config
NATIVE_DETECTORS = ["Logitech HID++ 2.0", "Glorious Model O / O- Wireless", "Glorious Model D / D- Wireless",
                    "ASUS Aura Motherboard"]

PKT_COUNT, PKT_DATA, PKT_PROTO, PKT_NAME = 0, 1, 40, 50
PKT_LIST_UPDATED = 100
PKT_UPDATELEDS, PKT_SETCUSTOM, PKT_UPDATEMODE, PKT_SAVEMODE = 1050, 1100, 1101, 1102
TYPE_NAMES = ["Motherboard", "DRAM", "GPU", "Cooler", "LED Strip", "Keyboard", "Mouse", "Mousemat", "Headset",
              "Headset Stand", "Gamepad", "Light", "Speaker", "Virtual", "Storage", "Case", "Microphone",
              "Accessory", "Keypad", "Unknown"]


class _Reader:
    def __init__(self, d: bytes):
        self.d, self.p = d, 0

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def string(self):
        n = self.take("H")
        s = self.d[self.p:self.p + n]
        self.p += n
        return s.split(b"\0", 1)[0].decode(errors="replace")


def _pack_mode(m: Mode) -> bytes:
    name = m.name.encode() + b"\0"
    out = struct.pack("<H", len(name)) + name
    out += struct.pack("<iIIIIIIIIIII", m.value, m.flags, m.speed_min, m.speed_max, m.brightness_min,
                       m.brightness_max, m.colors_min, m.colors_max, m.speed, m.brightness, m.direction, m.color_mode)
    out += struct.pack("<H", len(m.colors)) + b"".join(struct.pack("<I", c) for c in m.colors)
    return out


class OpenRGBClient:
    def __init__(self, host=HOST, port=PORT):
        self.host, self.port = host, port
        self.sock: Optional[socket.socket] = None
        self.version = 0

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send(0, PKT_PROTO, struct.pack("<I", PROTOCOL_VERSION))
        self.version = min(struct.unpack("<I", self._expect(PKT_PROTO))[0], PROTOCOL_VERSION)
        self._send(0, PKT_NAME, b"RGB Deck bridge\0")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _send(self, dev, pkt, payload=b""):
        if not self.sock:
            raise DeviceError("OpenRGB bridge not connected")
        try:
            self.sock.sendall(b"ORGB" + struct.pack("<III", dev, pkt, len(payload)) + payload)
        except OSError as e:
            self.close()
            raise DeviceError(f"OpenRGB bridge: {e}") from e

    def _recv(self):
        hdr = b""
        while len(hdr) < 16:
            c = self.sock.recv(16 - len(hdr))
            if not c:
                self.close()
                raise DeviceError("OpenRGB server closed the connection")
            hdr += c
        _dev, pkt, size = struct.unpack("<III", hdr[4:])
        body = b""
        while len(body) < size:
            c = self.sock.recv(size - len(body))
            if not c:
                self.close()
                raise DeviceError("OpenRGB server closed the connection")
            body += c
        return pkt, body

    def _expect(self, want):
        for _ in range(64):
            pkt, body = self._recv()
            if pkt == want:
                return body
        raise DeviceError(f"OpenRGB: no reply to packet {want}")

    def count(self) -> int:
        self._send(0, PKT_COUNT)
        return struct.unpack("<I", self._expect(PKT_COUNT))[0]

    def device(self, idx: int) -> Device:
        self._send(idx, PKT_DATA, struct.pack("<I", self.version))
        r = _Reader(self._expect(PKT_DATA))
        r.take("I")
        dtype = r.take("i")
        name, vendor, desc, ver, serial, loc = (r.string() for _ in range(6))
        nmodes, active = r.take("H"), r.take("i")
        modes = []
        for mi in range(nmodes):
            mname = r.string()
            vals = r.take("iIIIIIIIIIII")
            cols = [r.take("I") for _ in range(r.take("H"))]
            modes.append(Mode(mi, mname, *vals, cols))
        zones, cursor = [], 0
        for zi in range(r.take("H")):
            zname = r.string()
            ztype = r.take("i")
            lmin, lmax, lcount = r.take("III")
            matrix = None
            if r.take("H"):
                h, w = r.take("II")
                flat = [r.take("I") for _ in range(h * w)]
                matrix = [flat[row * w:(row + 1) * w] for row in range(h)]
            if self.version >= 4:
                for _ in range(r.take("H")):
                    r.string(); r.take("i"); r.take("II")
            zones.append(Zone(zi, zname, ztype, lmin, lmax, lcount, matrix, cursor))
            cursor += lcount
        leds = []
        for _ in range(r.take("H")):
            leds.append(r.string())
            r.take("I")
        colors = [r.take("I") for _ in range(r.take("H"))]
        tname = TYPE_NAMES[dtype] if 0 <= dtype < len(TYPE_NAMES) else "Unknown"
        return Device(idx, tname, name, vendor, f"{desc} (via OpenRGB)", ver, serial, loc, active, modes, zones,
                      leds, colors)

    def update_leds(self, idx: int, colors: List[int]):
        body = struct.pack("<H", len(colors)) + b"".join(struct.pack("<I", c) for c in colors)
        self._send(idx, PKT_UPDATELEDS, struct.pack("<I", 4 + len(body)) + body)

    def update_mode(self, idx: int, mode: Mode, save=False):
        body = struct.pack("<i", mode.index) + _pack_mode(mode)
        self._send(idx, PKT_SAVEMODE if save else PKT_UPDATEMODE, struct.pack("<I", 4 + len(body)) + body)


class BridgedDevice:
    """Driver-shaped wrapper so the backend treats OpenRGB devices like native ones."""

    def __init__(self, client: OpenRGBClient, remote_index: int, device: Device):
        self.client, self.remote_index, self.device = client, remote_index, device

    def set_colors(self, colors: List[int]):
        self.client.update_leds(self.remote_index, colors)
        self.device.colors = list(colors)

    def set_mode(self, mode: Mode, save: bool = False):
        self.client.update_mode(self.remote_index, mode, save)
        self.device.active_mode = mode.index

    def close(self):
        pass


def _server_running() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.4):
            return True
    except OSError:
        return False


def _private_config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rgbdeck" / "openrgb"
    base.mkdir(parents=True, exist_ok=True)
    cfg = base / "OpenRGB.json"
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
        except ValueError:
            data = {}
    det = data.setdefault("Detectors", {}).setdefault("detectors", {})
    for name in NATIVE_DETECTORS:
        det[name] = False
    data.setdefault("Server", {"all_controllers": False, "default_host": HOST, "default_port": PORT})
    cfg.write_text(json.dumps(data, indent=2))
    return base


_started_proc: Optional[subprocess.Popen] = None


def ensure_server(start: bool = True) -> bool:
    global _started_proc
    if _server_running():
        return True
    if not start:
        return False
    exe = shutil.which("openrgb")
    if not exe:
        return False
    cfg = _private_config_dir()
    log.info("starting OpenRGB SDK server for bridged devices (config %s)", cfg)
    _started_proc = subprocess.Popen([exe, "--server", "--noautoconnect", "--config", str(cfg)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    deadline = time.time() + 25
    while time.time() < deadline:
        if _server_running():
            time.sleep(0.5)
            return True
        time.sleep(0.25)
    return False


def available() -> bool:
    return shutil.which("openrgb") is not None


def probe(enabled: bool) -> List[BridgedDevice]:
    """Bridge devices (only when enabled). Native families are excluded by the private config."""
    if not enabled or not ensure_server():
        return []
    client = OpenRGBClient()
    try:
        client.connect()
        out = []
        for i in range(client.count()):
            d = client.device(i)
            out.append(BridgedDevice(client, i, d))
        return out
    except (DeviceError, OSError) as e:
        log.warning("OpenRGB bridge unavailable: %s", e)
        client.close()
        return []
