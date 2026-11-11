# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Corsair iCUE Commander Duo (1b1c:0c56): two +5 V ARGB ports (plus two fan headers and two temperature probes,
which this driver leaves alone).

The hub speaks Corsair's "Commander Core" protocol: 64-byte HID reports, every request answered by one report
that echoes the request's opcode.

    host   -> 00 08 <opcode> <arguments ...>            (65 bytes with the report id)
    device -> 00 <opcode> <error> <data type lo hi> <data ...>

    01 03 00 02 / 01 03 00 01     software mode (the host drives the LEDs) / hardware mode (the hub's own look)
    02 13                         firmware version: bytes 3, 4 and a little-endian word at 5
    05 01 <h> <endpoint>          close an endpoint on handle h
    0d <h> <endpoint>             open an endpoint on handle h
    08 <h>                        read the open endpoint
    06 <h> <len lo hi> 00 00 <data type lo hi> <data ...>   write it (len counts data type + data);
    07 <h> <more data>            ... continued, 61 bytes of payload per report
    15 01                         restart the LED ports after their configuration changed

    endpoint 20   LED ports as the hub detected them: from byte 6, per port <state lo hi> <LED count lo hi>,
                  state 2 = something Corsair was recognised
    endpoint 1e   data type 0d 00: <2 ports> then per port <01> <1 = LED count given by the host>
    endpoint 1d   data type 0c 00: <2 ports> then per port <LED count lo hi>
    endpoint 22   data type 12 00: r g b for every LED, port 1 first, then port 2 (kept open on handle 0)

A plain ARGB strip cannot tell the hub how long it is, so the LED count of each port is the user's to set
(the zone's LED count in the app); Corsair's own fans and strips are detected by the hub.

About 30 to 60 seconds without traffic and the hub drops the software session and shows its own lighting again:
keepalive() repeats the colours every few seconds. An answer with error 03 means the session is gone; the driver
then enters software mode again and repeats the request once.

Protocol facts from the OpenLinkHub project's Commander Duo support and the corsair-cduo Linux driver's notes
(which credit FanControl.CorsairLink); no code was taken from either. Written without access to the hardware:
experimental until someone confirms it. Fan speeds are never written here.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from rgbdeck.hw.hidinfo import parse_collections
from rgbdeck.hw.hidraw import HidDevice, enumerate_hid
from rgbdeck.models import (
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_PER_LED_COLOR,
    Device,
    DeviceError,
    Mode,
    Zone,
    u32_to_rgb,
)

log = logging.getLogger("rgbdeck.corsair.duo")

EXPERIMENTAL = True
VID = 0x1B1C
PIDS = {0x0C56: "Corsair Commander Duo"}
USB_IDS = [(VID, pid) for pid in sorted(PIDS)]

PORTS = 2
LEDS_MAX = 204  # per port, Corsair's stated limit
PACKET = 64
CHUNK = 61  # payload bytes of a write / write-more report
READ_TIMEOUT = 0.5
ZONE_LINEAR = 1

OP_MODE, OP_FIRMWARE, OP_CLOSE, OP_OPEN, OP_READ, OP_WRITE, OP_WRITE_MORE, OP_LED_RESTART = 1, 2, 5, 0x0D, 8, 6, 7, 0x15
EP_LED_PORTS, EP_PORT_MANUAL, EP_PORT_COUNT, EP_COLORS = 0x20, 0x1E, 0x1D, 0x22
H_COLOR, H_DATA = 0x00, 0x01
ERR_NO_SESSION = 0x03
PORT_DETECTED = 2


class CommanderDuo:
    keepalive_interval = 8.0  # the hub falls back to its own lighting after 30-60 s of silence

    def __init__(self, path: str, name: str, hid: Optional[HidDevice] = None):
        self.hid = hid or HidDevice(path)
        self.path = path
        self._last: Optional[bytes] = None  # colour data as last sent (repeated by keepalive)
        self._manual = [False] * PORTS  # LED count of a port given by the user
        try:
            self.hid.drain()
            fw = self._transfer(bytes([OP_FIRMWARE, 0x13]), recover=False)
            self.version = f"{fw[3]}.{fw[4]}.{fw[5] | fw[6] << 8}"
            self._software_mode()
            self.counts = self._detect()
            self._open_colors()
        except DeviceError:
            self.hid.close()
            raise
        self.device = Device(
            0,
            "LED Strip",
            name,
            "Corsair",
            "Corsair Commander Duo, two ARGB ports (set each port's LED count if a strip is not detected)",
            self.version,
            "",
            "HID: " + path,
            0,
            [Mode(0, "Direct", 0, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED)],
            [],
            [],
            [],
        )
        self._rebuild_zones([])
        log.info("%s firmware %s: LEDs per port %s", name, self.version, self.counts)

    # ---- transport ------------------------------------------------------------------
    def _transfer(self, request: bytes, recover: bool = True) -> bytes:
        """One request, its answer (matched by the echoed opcode). A dropped session is re-entered once."""
        packet = bytes([0x00, 0x08]) + request
        self.hid.write(packet + bytes(PACKET + 1 - len(packet)))
        for _ in range(4):  # a late answer to an earlier request may still be in the pipe
            reply = self.hid.read(PACKET, READ_TIMEOUT)
            if reply is None:
                break
            if len(reply) >= 7 and reply[1] == request[0]:
                if reply[2] == ERR_NO_SESSION and recover and request[0] != OP_MODE:
                    log.info("Commander Duo dropped the software session: entering it again")
                    self._software_mode()
                    self._transfer(bytes([OP_CLOSE, 0x01, H_COLOR, EP_COLORS]), recover=False)
                    self._transfer(bytes([OP_OPEN, H_COLOR, EP_COLORS]), recover=False)
                    return self._transfer(request, recover=False)
                return reply
        raise DeviceError("Corsair Commander Duo did not answer (is iCUE running? Close it first)")

    def _software_mode(self):
        self._transfer(bytes([OP_MODE, 0x03, 0x00, 0x02]), recover=False)

    def _read_endpoint(self, endpoint: int) -> bytes:
        self._transfer(bytes([OP_CLOSE, 0x01, H_DATA, endpoint]))
        self._transfer(bytes([OP_OPEN, H_DATA, endpoint]))
        try:
            return self._transfer(bytes([OP_READ, H_DATA]))
        finally:
            self._transfer(bytes([OP_CLOSE, 0x01, H_DATA, endpoint]))

    def _write_endpoint(self, endpoint: int, payload: bytes):
        """payload = data type (2 bytes) + data."""
        self._transfer(bytes([OP_CLOSE, 0x01, H_DATA, endpoint]))
        self._transfer(bytes([OP_OPEN, H_DATA, endpoint]))
        try:
            self._send(H_DATA, payload)
        finally:
            self._transfer(bytes([OP_CLOSE, 0x01, H_DATA, endpoint]))

    def _send(self, handle: int, payload: bytes):
        buf = bytes([len(payload) & 0xFF, len(payload) >> 8, 0x00, 0x00]) + payload
        self._transfer(bytes([OP_WRITE, handle]) + buf[:CHUNK])
        for i in range(CHUNK, len(buf), CHUNK):
            self._transfer(bytes([OP_WRITE_MORE, handle]) + buf[i : i + CHUNK])

    # ---- LED ports ------------------------------------------------------------------
    def _detect(self) -> List[int]:
        """LEDs per port as the hub recognised them (0 for an empty port or a strip it cannot identify)."""
        reply = self._read_endpoint(EP_LED_PORTS)
        counts = []
        for port in range(PORTS):
            at = 6 + 4 * port
            state = reply[at] | reply[at + 1] << 8
            leds = reply[at + 2] | reply[at + 3] << 8
            counts.append(min(LEDS_MAX, leds) if state == PORT_DETECTED else 0)
        return counts

    def _open_colors(self):
        self._transfer(bytes([OP_CLOSE, 0x01, H_COLOR, EP_COLORS]))
        self._transfer(bytes([OP_OPEN, H_COLOR, EP_COLORS]))

    def _configure_ports(self):
        """Tell the hub which ports have their LED count given by the host, and the counts."""
        manual = b"".join(bytes([0x01, int(self._manual[p])]) for p in range(PORTS))
        self._write_endpoint(EP_PORT_MANUAL, bytes([0x0D, 0x00, PORTS]) + manual)
        counts = b"".join(
            bytes([self.counts[p] & 0xFF, self.counts[p] >> 8]) if self._manual[p] else b"\x00\x00"
            for p in range(PORTS)
        )
        self._write_endpoint(EP_PORT_COUNT, bytes([0x0C, 0x00, PORTS]) + counts)
        time.sleep(0.1)
        self._transfer(bytes([OP_LED_RESTART, 0x01]))
        self._open_colors()

    def _rebuild_zones(self, keep: List[int]):
        zones, names, start = [], [], 0
        for port in range(PORTS):
            n = self.counts[port]
            zname = f"ARGB Port {port + 1}"
            zones.append(Zone(port, zname, ZONE_LINEAR, 0, LEDS_MAX, n, None, start))
            names += [f"{zname}, LED {i + 1}" for i in range(n)]
            start += n
        self.device.zones, self.device.led_names = zones, names
        self.device.colors = (list(keep) + [0] * start)[:start]

    def resize_zone(self, zone_idx: int, count: int):
        """Set how many LEDs hang on a port (0..204). The hub is told, so a strip it cannot detect lights up."""
        if not 0 <= zone_idx < PORTS:
            raise ValueError(f"zone {zone_idx} does not exist")
        count = max(0, min(LEDS_MAX, int(count)))
        old = [self.device.colors[z.start_idx : z.start_idx + z.leds_count] for z in self.device.zones]
        self.counts[zone_idx] = count
        self._manual[zone_idx] = True
        old[zone_idx] = (old[zone_idx] + [0] * count)[:count]
        self._rebuild_zones([c for chunk in old for c in chunk])
        self._configure_ports()
        self._last = None

    # ---- what the app asks for --------------------------------------------------------
    def set_colors(self, colors: List[int]):
        n = len(self.device.colors)
        colors = (list(colors) + [0] * n)[:n]
        self.device.colors = colors
        if not n:
            return
        data = b"".join(bytes(u32_to_rgb(c)) for c in colors)
        self._send(H_COLOR, bytes([0x12, 0x00]) + data)
        self._last = data

    def set_mode(self, mode: Mode, save: bool = False):  # noqa: ARG002 - the hub stores nothing from here
        self.device.active_mode = mode.index
        self.set_colors(self.device.colors)

    def keepalive(self):
        """Keep the software session (and with it the colours) alive."""
        if self._last:
            self._send(H_COLOR, bytes([0x12, 0x00]) + self._last)
        else:
            self._transfer(bytes([OP_FIRMWARE, 0x13]))

    def close(self):
        """Hand the LEDs back to the hub (its own stored lighting takes over)."""
        try:
            self._transfer(bytes([OP_MODE, 0x03, 0x00, 0x01]), recover=False)
        except DeviceError:
            pass
        self.hid.close()


def _candidates(infos) -> list:
    """Interfaces of one hub, most likely first: the vendor collection (usage page ff42), then interface 0."""

    def rank(info) -> int:
        try:
            if any(page == 0xFF42 for page, _usage, _ids in parse_collections(info.descriptor or b"")):
                return 0
        except Exception:  # noqa: BLE001 - a descriptor we cannot read just loses its head start
            pass
        return 1 if info.interface in (0, -1) else 2

    return sorted(infos, key=rank)


def probe() -> List[CommanderDuo]:
    found = []
    by_pid = {}
    for info in enumerate_hid(VID):
        if info.pid in PIDS:
            by_pid.setdefault(info.pid, []).append(info)
    for pid, infos in by_pid.items():
        for info in _candidates(infos):
            try:
                found.append(CommanderDuo(info.path, PIDS[pid]))
                break  # one hub: the first interface that speaks the protocol
            except DeviceError as e:
                log.info("%s at %s (interface %s) skipped: %s", PIDS[pid], info.path, info.interface, e)
            except Exception as e:  # noqa: BLE001
                log.info("%s at %s skipped: %s: %s", PIDS[pid], info.path, type(e).__name__, e)
    return found


__all__ = ["USB_IDS", "CommanderDuo", "probe"]
