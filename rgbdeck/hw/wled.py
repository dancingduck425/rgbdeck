# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
WLED controllers (ESP8266 / ESP32 LED strips and matrices running the WLED firmware), over the local network.

    who is there   mDNS question for _wled._tcp.local (answers come straight back to the asking socket), or an
                   address the user typed; GET http://<ip>/json/info confirms it ("brand": "WLED") and tells
                   the name, MAC and LED count
    colours        WLED's UDP realtime protocol on port 21324:
                       DRGB   02 <timeout s> r g b r g b ...          (LED 0 onwards, up to 490 LEDs)
                       DNRGB  04 <timeout s> <start hi> <start lo> r g b ...   (489 LEDs per packet)
                   While packets keep coming the strip shows them; REALTIME_TIMEOUT seconds after the last
                   one it goes back to whatever WLED itself was doing, so nothing here can leave a strip stuck.
    lasting state  POST /json/state: on/off, and on close the look is written as WLED's own state
                   ({"seg": {"i": [...]}}), so it stays when RGB Deck is gone.

Built from the WLED documentation (kno.wled.ge: UDP realtime, JSON API) and tested against a simulated
controller only: experimental until someone confirms it on hardware.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import struct
import threading
import time
from typing import Dict, List, Optional

from rgbdeck import paths
from rgbdeck.models import (
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_PER_LED_COLOR,
    Device,
    DeviceError,
    Mode,
    Zone,
    u32_to_rgb,
)

log = logging.getLogger("rgbdeck.wled")

EXPERIMENTAL = True
USB_IDS: list = []  # nothing for the udev rule

HTTP_PORT = 80
REALTIME_PORT = 21324
REALTIME_TIMEOUT = 3  # seconds a strip keeps showing our colours after the last packet
HTTP_TIMEOUT = 0.8
MDNS_ADDR = ("224.0.0.251", 5353)
CHECK_INTERVAL = 30.0
DEFAULT_SHOWN = 30  # LEDs offered for painting on a long strip (the user can raise it to the real count)
V_DIRECT, V_OFF, V_OWN = 0, 1, 2


# ----------------------------------------------------------------------------- settings
_cfg_lock = threading.Lock()
_cfg: Optional[dict] = None


def _config_file():
    return paths.config_dir() / "wled.json"


def _valid_ip(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text.strip()), ipaddress.IPv4Address)
    except ValueError:
        return False


def _config() -> dict:
    """{"enabled": bool, "hosts": [ip, ...]} (cached)."""
    global _cfg
    with _cfg_lock:
        if _cfg is None:
            data: dict = {}
            try:
                data = json.loads(_config_file().read_text())
            except (OSError, ValueError):
                pass
            if not isinstance(data, dict):
                data = {}
            _cfg = {
                "enabled": bool(data.get("enabled", True)),
                "hosts": [str(h) for h in data.get("hosts", []) if _valid_ip(str(h))],
            }
        return _cfg


def _save_config():
    with _cfg_lock:
        if _cfg is None:
            return
        try:
            paths.config_dir().mkdir(parents=True, exist_ok=True)
            _config_file().write_text(json.dumps(_cfg, indent=1))
        except OSError as e:
            log.warning("could not save %s: %s", _config_file(), e)


# ----------------------------------------------------------------------------- talking to a controller
def _http(ip: str, path: str, body: Optional[dict] = None, port: Optional[int] = None) -> Optional[dict]:
    import urllib.error  # here, not at the top: urllib.request drags in ssl and friends (about 5 MB)
    import urllib.request

    url = f"http://{ip}:{port or HTTP_PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:  # noqa: S310 - plain http to a LAN device
            out = json.loads(r.read(65536) or b"{}")
            return out if isinstance(out, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return None


class _Info:
    def __init__(self, ip: str, info: dict):
        leds = info.get("leds") or {}
        self.ip = ip
        self.name = str(info.get("name") or "WLED")
        self.mac = str(info.get("mac") or ip)
        self.version = str(info.get("ver") or "")
        self.count = max(1, int(leds.get("count") or 1))
        self.udp_port = int(info.get("udpport") or REALTIME_PORT)


def check(ip: str) -> Optional[_Info]:
    """Is a WLED controller at this address? Its details if so."""
    info = _http(ip, "/json/info")
    if not info or str(info.get("brand", "")).upper() != "WLED":
        return None
    try:
        return _Info(ip, info)
    except (TypeError, ValueError):
        return None


def _mdns_question(name: str) -> bytes:
    """A one-question mDNS packet (PTR), asking for the answer by unicast."""
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + q + struct.pack(">HH", 12, 0x8001)


def discover(wait: float = 0.8) -> List[str]:
    """Addresses that answer the mDNS question for WLED's service (whether they are WLED is checked later)."""
    found: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.settimeout(0.2)
        s.sendto(_mdns_question("_wled._tcp.local"), MDNS_ADDR)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            if b"_wled" in data and addr[0] not in found:
                found.append(addr[0])
    except OSError as e:
        log.debug("mDNS search failed: %s", e)
    finally:
        s.close()
    return found


# ----------------------------------------------------------------------------- presence
_state_lock = threading.Lock()
_alive: Dict[str, _Info] = {}  # address -> details, as of the last check
_checked_at = 0.0
_checking = False
_probed: frozenset = frozenset()


def _refresh(search: bool = True, wait: float = 0.8):
    """Find out which controllers are there right now (blocks for up to a second or two)."""
    global _alive, _checked_at, _checking
    cfg = _config()
    hosts = list(cfg["hosts"])
    if search:
        for ip in discover(wait):
            if ip not in hosts:
                hosts.append(ip)
    alive: Dict[str, _Info] = {}
    seen_macs = set()
    for ip in hosts:
        info = check(ip)
        if info and info.mac not in seen_macs:
            seen_macs.add(info.mac)
            alive[ip] = info
            if ip not in cfg["hosts"]:
                cfg["hosts"].append(ip)  # remembered: found again even where mDNS does not get through
                _save_config()
                log.info("found WLED %s at %s (%d LEDs)", info.name, ip, info.count)
    with _state_lock:
        _alive, _checked_at, _checking = alive, time.monotonic(), False


def fingerprint() -> frozenset:
    """Controllers present right now (the backend rescans when this differs from probed())."""
    global _checking
    if not _config()["enabled"]:
        return frozenset()
    with _state_lock:
        due = time.monotonic() - _checked_at >= CHECK_INTERVAL and not _checking
        if due:
            _checking = True
        now = frozenset(i.mac for i in _alive.values())
    if due:  # in the background: the hotplug poll runs on the UI timer
        threading.Thread(target=_refresh, name="rgbdeck-wled-check", daemon=True).start()
    return now


def probed() -> frozenset:
    return _probed


def search() -> List[str]:
    """Look for controllers now (blocks for about a second); "name at address" of each one answering."""
    if not _config()["enabled"]:
        return []
    _refresh(search=True)
    with _state_lock:
        return sorted(f"WLED {i.name} at {ip}" for ip, i in _alive.items())


def get_settings() -> dict:
    cfg = _config()
    return {"enabled": cfg["enabled"], "hosts": list(cfg["hosts"])}


def add(ip: str) -> Optional[str]:
    """Remember a controller by address; its name when one answers there."""
    ip = ip.strip()
    if not _valid_ip(ip):
        return None
    info = check(ip)
    if info is None:
        return None
    cfg = _config()
    if ip not in cfg["hosts"]:
        cfg["hosts"].append(ip)
        _save_config()
    with _state_lock:
        _alive[ip] = info
    return info.name


def forget(ip: str) -> bool:
    cfg = _config()
    if ip not in cfg["hosts"]:
        return False
    cfg["hosts"].remove(ip)
    _save_config()
    with _state_lock:
        _alive.pop(ip, None)
    return True


def status_report() -> List[dict]:
    """For the Lights panel: {"name", "ip", "state"} with state connected | silent."""
    cfg = _config()
    if not cfg["enabled"]:
        return []
    with _state_lock:
        alive = dict(_alive)
    rows = [{"name": f"WLED {i.name}", "ip": ip, "state": "connected"} for ip, i in alive.items()]
    rows += [{"name": "WLED controller", "ip": ip, "state": "silent"} for ip in cfg["hosts"] if ip not in alive]
    return rows


def _reset_for_tests():
    global _cfg, _alive, _checked_at, _probed, _checking
    with _cfg_lock:
        _cfg = None
    with _state_lock:
        _alive, _checked_at, _checking = {}, 0.0, False
    _probed = frozenset()


# ----------------------------------------------------------------------------- driver
class WledStrip:
    keeps_state = True  # close() writes the look into WLED itself
    follows_typing = True
    keepalive_interval = 1.0  # realtime colours expire after REALTIME_TIMEOUT s unless repeated

    def __init__(self, info: _Info, state: Optional[dict] = None):
        self._info = info
        self._count = min(info.count, DEFAULT_SHOWN)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._live: Optional[List[int]] = None  # colours being streamed (None: WLED is on its own)
        modes = [
            Mode(0, "Direct", V_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            Mode(1, "Off", V_OFF, 0),
            Mode(2, "WLED's own effect", V_OWN, 0),
        ]
        self.device = Device(
            0,
            "LED Strip",
            f"WLED {info.name}",
            "WLED",
            f"WLED controller at {info.ip}, {info.count} LEDs",
            info.version,
            info.mac,
            f"wled:{info.mac}",
            0,
            modes,
            [],
            [],
            [],
        )
        self._layout([0] * self._count)
        if state is not None:
            self.device.active_mode = 2 if state.get("on", True) else 1

    def _layout(self, colors: List[int]):
        n = self._count
        self.device.zones = [Zone(0, "LEDs", 1, 1, self._info.count, n)]
        self.device.led_names = [f"LED {i + 1}" for i in range(n)] if n > 1 else ["Light"]
        self.device.colors = (list(colors) + [0] * n)[:n]

    def resize_zone(self, zone_idx: int, count: int):
        """Show the strip as this many LEDs (1 .. its real count); they are spread over the real ones."""
        if zone_idx != 0:
            raise ValueError(f"zone {zone_idx} does not exist")
        self._count = max(1, min(self._info.count, int(count)))
        self._layout(self.device.colors)

    def _physical(self, colors: List[int]) -> List[int]:
        total, n = self._info.count, self._count
        return [colors[i * n // total] for i in range(total)]

    def _stream(self, colors: List[int]):
        real = self._physical(colors)
        try:
            if len(real) <= 490:
                body = b"".join(bytes(u32_to_rgb(c)) for c in real)
                self._sock.sendto(bytes([2, REALTIME_TIMEOUT]) + body, (self._info.ip, self._info.udp_port))
            else:
                for start in range(0, len(real), 489):
                    body = b"".join(bytes(u32_to_rgb(c)) for c in real[start : start + 489])
                    head = bytes([4, REALTIME_TIMEOUT, start >> 8, start & 0xFF])
                    self._sock.sendto(head + body, (self._info.ip, self._info.udp_port))
        except OSError as e:
            raise DeviceError(f"{self.device.name}: {e}") from e

    def _post(self, body: dict):
        if _http(self._info.ip, "/json/state", body) is None:
            raise DeviceError(f"{self.device.name} did not answer")

    # ---- what the app asks for ----------------------------------------------------
    def set_colors(self, colors: List[int]):
        colors = (list(colors) + [0] * self._count)[: self._count]
        self.device.colors = colors
        if self.device.current_mode.value != V_DIRECT:
            return
        if self._live is None:
            self._post({"on": True})  # realtime colours do not show on a strip that is switched off
        self._live = colors
        self._stream(colors)

    def set_mode(self, mode: Mode, save: bool = False):  # noqa: ARG002 - see close()
        self.device.active_mode = mode.index
        if mode.value == V_DIRECT:
            self._live = None
            self.set_colors(self.device.colors)
        else:
            self._live = None  # stop streaming: WLED takes over within REALTIME_TIMEOUT seconds
            self._post({"on": mode.value != V_OFF})

    def keepalive(self):
        if self._live is not None:
            self._stream(self._live)

    def close(self):
        """Leave the colours behind as WLED's own state, so they stay when nobody streams any more."""
        live, self._live = self._live, None
        try:
            if live is not None and any(live):
                real = self._physical(live)
                for start in range(0, len(real), 200):  # WLED's JSON buffer is small: a couple of hundred per request
                    led: list = [start]
                    led += ["%02X%02X%02X" % u32_to_rgb(c) for c in real[start : start + 200]]
                    _http(self._info.ip, "/json/state", {"on": True, "seg": {"id": 0, "fx": 0, "i": led}})
            elif live is not None:
                _http(self._info.ip, "/json/state", {"on": False})
        finally:
            self._sock.close()


def probe() -> List[WledStrip]:
    global _probed
    _probed = frozenset()
    if not _config()["enabled"]:
        return []
    _refresh(search=True, wait=0.35)  # a controller that answers later is picked up by the next check
    with _state_lock:
        infos = list(_alive.values())
    found = [WledStrip(i, _http(i.ip, "/json/state")) for i in infos]
    _probed = frozenset(i.mac for i in infos)
    return found


__all__ = [
    "USB_IDS",
    "WledStrip",
    "add",
    "check",
    "discover",
    "fingerprint",
    "forget",
    "probe",
    "probed",
    "status_report",
]
