# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Govee Wi-Fi lights over Govee's LAN API: plain UDP on the local network, no cloud, no account.

The light's owner switches on "LAN Control" for it in the Govee Home app (device settings). After that:

    scan       -> multicast 239.255.255.250:4001      {"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}
    answers    <- to port 4002 of whoever asked       {"msg":{"cmd":"scan","data":{"ip","device","sku",...}}}
    commands   -> light:4003                          turn / brightness / colorwc / devStatus / ptReal

turn, brightness, colorwc and devStatus are Govee's documented commands and work on every LAN-capable model.
ptReal carries the light's Bluetooth packets (20 bytes, last byte = XOR of the rest, base64); that is how
segment colours ("33 05 15 01 r g b 00*5 <segment bitmask>") and firmware scenes ("33 05 04 <code>") are set.
Which model has how many segments and which scenes comes from govee_devices.py.

Packet formats follow Govee's LAN API guide and the govee-local-api project (Apache-2.0); see govee_devices.py.

UDP gives no delivery guarantee and a light can be flooded, so every light has a small sender thread: at most
one update per MIN_SEND_INTERVAL (newer states replace older unsent ones), and the last state is sent once
more, in full, shortly after things settle.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
import struct
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from rgbdeck import paths
from rgbdeck.hw.govee_devices import MODELS, SCENE_SETS, lookup
from rgbdeck.models import (
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_PER_LED_COLOR,
    Device,
    Mode,
    Zone,
    rgb_to_u32,
    u32_to_rgb,
)

log = logging.getLogger("rgbdeck.govee")

EXPERIMENTAL = True  # until someone confirms it on real lights
USB_IDS: list = []  # not a USB device: nothing for the udev rule

SCAN_GROUP = "239.255.255.250"
SCAN_PORT = 4001  # lights listen here for "scan"
LISTEN_PORT = 4002  # lights answer to this port on the asker's address
CMD_PORT = 4003  # lights take commands here

RESCAN_INTERVAL = 30.0  # seconds between presence scans
GONE_AFTER = 100.0  # a light that missed three scans is treated as unplugged
PROBE_WAIT = 0.7  # how long a device scan waits for remembered lights to answer
FIRST_WAIT = 0.25  # ... and for lights nobody has seen yet
STATUS_WAIT = 0.4
MIN_SEND_INTERVAL = 0.1  # at most 10 updates per second and light
CONFIRM_DELAY = 0.6  # the settled state is sent once more after this long
MAX_SEGMENTS = 56  # 7 bitmask bytes in the segment packet
UNKNOWN_MODEL_SEGMENTS = 20  # a model missing from the table may be given up to this many by the user

V_DIRECT, V_OFF, V_WHITE, V_SCENE = 0, 1, 0x10000, 0x20000
WHITES = (("Warm White", 2700), ("Neutral White", 4000), ("Cool White", 6500))
SCENE_NAMES = {"snow_flake": "Snowflake", "breathe": "Breathing", "valentines_day": "Valentine's Day"}

# Windows asks "allow this app on your network?" as soon as a program listens for UDP, so looking for lights
# is opt-in there (Settings); elsewhere it is on unless switched off.
DEFAULT_ENABLED = sys.platform != "win32"


# ----------------------------------------------------------------------------- settings
_cfg_lock = threading.Lock()
_cfg: Optional[dict] = None


def _config_file():
    return paths.config_dir() / "govee.json"


def _config() -> dict:
    """{"enabled": bool, "manual": [ip, ...], "known": {device id: {"ip": ..., "sku": ...}}} (cached)."""
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
                "enabled": bool(data.get("enabled", DEFAULT_ENABLED)),
                "manual": [str(ip) for ip in data.get("manual", []) if _valid_ip(str(ip))],
                "known": {str(k): dict(v) for k, v in (data.get("known") or {}).items() if isinstance(v, dict)},
                # address -> MAC of Govee hardware a network search came across (names the entry in the panel)
                "seen": {str(k): str(v) for k, v in (data.get("seen") or {}).items() if _valid_ip(str(k))},
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


def _valid_ip(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text.strip()), ipaddress.IPv4Address)
    except ValueError:
        return False


def get_settings() -> dict:
    """For the Settings dialog: {"enabled": bool, "manual": [ip, ...]}."""
    cfg = _config()
    return {"enabled": cfg["enabled"], "manual": list(cfg["manual"])}


def set_settings(enabled: bool, manual: List[str]) -> bool:
    """Store the user's choice; True if something changed (the caller then rescans)."""
    cfg = _config()
    manual = [ip.strip() for ip in manual if _valid_ip(ip)]
    manual = list(dict.fromkeys(manual))
    if cfg["enabled"] == bool(enabled) and cfg["manual"] == manual:
        return False
    cfg["enabled"], cfg["manual"] = bool(enabled), manual
    _save_config()
    _lan.last_scan = 0.0
    return True


def _reset_for_tests():
    global _cfg
    _lan.stop()
    with _cfg_lock:
        _cfg = None


def found_lights() -> List[Tuple[str, str]]:
    """(model, address) of every light that is answering right now, for display."""
    return [(r.sku or "light", r.ip) for r in _lan.present() if not r.blind]


def _reach(ip: str) -> str:
    """What sits at this address, judged by how it treats a command datagram:

    "refused"      something is there but nothing listens on the command port: a Govee light with LAN Control
                   switched off (or simply not a Govee light)
    "unreachable"  no route / host down
    "quiet"        no objection: a light that accepts commands, or a host that drops them silently"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.3)
    try:
        s.connect((ip, CMD_PORT))
        for _ in range(2):  # the ICMP "port unreachable" for the first send surfaces on the next call
            s.send(b'{"msg":{"cmd":"devStatus","data":{}}}')
            time.sleep(0.1)
        try:
            s.recv(64)
        except socket.timeout:
            pass
        return "quiet"
    except (ConnectionRefusedError, ConnectionResetError):  # Linux / macOS, Windows
        return "refused"
    except OSError:
        return "unreachable"
    finally:
        s.close()


def diagnose(ip: str, wait: float = 1.5) -> Tuple[str, str]:
    """Check one address the user typed: ("found", model) when a Govee light answers there, otherwise
    ("refused" | "unreachable" | "quiet" | "invalid", "") as in _reach(). Blocks for up to `wait` seconds."""
    ip = ip.strip()
    if not _valid_ip(ip):
        return "invalid", ""
    lan = _lan
    lan.start()
    t0 = time.monotonic()
    lan.send(ip, "scan", {"account_topic": "reserve"}, SCAN_PORT)
    state = _reach(ip)
    lan.reach[ip] = state

    def answered() -> bool:
        return any(r.ip == ip and r.last_seen >= t0 for r in lan.lights.values())

    if lan.listening and state == "quiet" and lan.wait_until(t0 + wait, answered) or answered():
        return "found", next((r.sku for r in lan.lights.values() if r.ip == ip), "")
    return state, ""


def rescan():
    """Look for lights now instead of at the next 30 s tick."""
    if _config()["enabled"]:
        _lan.start()
        _lan.scan()


# MAC prefixes registered to Shenzhen Intellirocks Tech (Govee) in the IEEE registry. Older lights carry
# their Wi-Fi module maker's prefix instead, so this recognises many Govee devices, not all.
GOVEE_OUIS = ("5c:e7:53", "d4:13:68", "d4:ad:fc", "e0:2a:25")
SWEEP_MAX_HOSTS = 1024  # a bigger network is searched only in the /24 around this machine
_VIRTUAL_IFACES = (
    "lo",
    "zt",
    "tun",
    "tap",
    "wg",
    "docker",
    "br-",
    "veth",
    "virbr",
    "waydroid",
    "tailscale",
    "vmnet",
    "vboxnet",
)


def _sweepable(ip: str, mask: str) -> Optional[ipaddress.IPv4Network]:
    try:
        iface = ipaddress.ip_interface(f"{ip}/{mask}")
    except ValueError:
        return None
    if not iface.ip.is_private or iface.ip.is_loopback or iface.ip.is_link_local:
        return None  # only home / office LANs: never walk a public range
    net = iface.network
    if net.num_addresses > SWEEP_MAX_HOSTS:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return net


def _local_networks() -> List[ipaddress.IPv4Network]:
    """The LANs this machine sits on (physical interfaces only; VPN and container networks are left alone)."""
    nets: List[Optional[ipaddress.IPv4Network]] = []
    if sys.platform.startswith("linux"):
        try:
            import fcntl

            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for _idx, name in socket.if_nameindex():
                    if name.startswith(_VIRTUAL_IFACES):
                        continue
                    ifr = struct.pack("256s", name.encode()[:15])
                    try:  # SIOCGIFADDR, SIOCGIFNETMASK
                        ip = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, ifr)[20:24])
                        mask = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x891B, ifr)[20:24])
                    except OSError:
                        continue
                    nets.append(_sweepable(ip, mask))
            finally:
                probe.close()
        except (ImportError, OSError):
            pass
    else:  # no portable way to read the netmask: assume the usual home /24
        nets = [_sweepable(ip, "255.255.255.0") for ip in _local_ipv4()]
    return list(dict.fromkeys(n for n in nets if n is not None))


def _neighbors() -> Dict[str, str]:
    """address -> MAC (aa:bb:cc:dd:ee:ff) of every host this machine has recently talked to (the ARP table)."""
    out: Dict[str, str] = {}
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/net/arp") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                        out[parts[0]] = parts[3].lower()
        else:
            import re
            import subprocess

            flags = 0x08000000 if sys.platform == "win32" else 0  # no console window
            text = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5, creationflags=flags).stdout
            pattern = r"(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})"
            for m in re.finditer(pattern, text):
                out[m.group(1)] = ":".join(f"{int(x, 16):02x}" for x in re.split("[:-]", m.group(2)))
    except Exception as e:  # noqa: BLE001 - a missing tool or odd output only costs the vendor hint
        log.debug("could not read the neighbour table: %s", e)
    return out


def search_network(networks: Optional[List[ipaddress.IPv4Network]] = None, progress=None) -> dict:
    """Walk the local network looking for Govee lights; blocks for seconds, so call it off the UI thread.

    Every address gets the scan request directly, which finds lights where the router does not pass multicast
    on. The ARP table then shows which hosts carry a Govee MAC: one of those that did not answer is a Govee
    device with LAN Control switched off (or one whose answers are filtered). Those are kept on the list so
    they connect by themselves once they start answering.

    A /24 takes about two seconds. Bigger networks take longer (the OS resolves only a couple of hundred new
    addresses at a time); progress(done, total) is called along the way.

    Returns {"found": [(model, address)], "locked": [address], "blind": [address], "hosts": n}."""
    cfg, lan = _config(), _lan
    if not cfg["enabled"]:
        cfg["enabled"] = True
        _save_config()
    lan.start()
    t0 = time.monotonic()
    lan.scan()
    nets = list(networks) if networks is not None else _local_networks()
    request = {"account_topic": "reserve"}
    locked: List[str] = []
    blind: List[str] = []
    judged = set()

    def note_govee_hardware():
        """Hosts with a Govee MAC that are not answering: say why, and remember them."""
        with lan.cv:
            answering = {r.ip for r in lan.lights.values() if r.last_seen >= t0}
        remembered = {v.get("ip") for v in cfg["known"].values()}
        for ip, mac in sorted(_neighbors().items()):
            if mac[:8] not in GOVEE_OUIS or ip in answering or ip in judged:
                continue
            try:
                if not any(ipaddress.ip_address(ip) in net for net in nets):
                    continue
            except ValueError:
                continue
            judged.add(ip)
            state = _reach(ip)
            lan.reach[ip] = state
            if state == "unreachable":
                continue
            cfg["seen"][ip] = mac
            if ip not in cfg["manual"] and ip not in remembered:
                cfg["manual"].append(ip)
            (locked if state == "refused" else blind).append(ip)

    note_govee_hardware()  # whatever the ARP table already holds shows up at once
    total = sum(max(0, net.num_addresses - 2) for net in nets) or 1
    hosts = 0
    for net in nets:
        for host in net.hosts():
            lan.send(str(host), "scan", request, SCAN_PORT)
            hosts += 1
            time.sleep(0.002)
            if progress is not None and hosts % 16 == 0:
                progress(hosts, total)
    time.sleep(1.2)  # answers, and address resolution for the stragglers
    note_govee_hardware()
    with lan.cv:
        found = sorted((r.sku or "light", r.ip) for r in lan.lights.values() if r.last_seen >= t0)
    answering = {ip for _sku, ip in found}
    locked = [ip for ip in locked if ip not in answering]
    blind = [ip for ip in blind if ip not in answering]
    _save_config()
    lan.last_scan = time.monotonic()
    log.info(
        "network search over %d addresses: answering %s, LAN Control off %s, silent %s", hosts, found, locked, blind
    )
    return {"found": found, "locked": locked, "blind": blind, "hosts": hosts}


def forget(ip: str) -> bool:
    """Drop an address the user added and any remembered light at it; True if something was removed."""
    cfg = _config()
    gone = [k for k, v in cfg["known"].items() if v.get("ip") == ip]
    if ip not in cfg["manual"] and not gone:
        return False
    cfg["manual"] = [x for x in cfg["manual"] if x != ip]
    cfg["seen"].pop(ip, None)
    for k in gone:
        del cfg["known"][k]
    with _lan.cv:
        for k in gone:
            _lan.lights.pop(k, None)
        _lan.reach.pop(ip, None)
    _save_config()
    return True


def status_report() -> List[dict]:
    """Every light RGB Deck knows about, for the Lights panel: {"name", "ip", "state"} with state one of
    connected | refused | unreachable | blind | silent | checking."""
    cfg, lan = _config(), _lan
    rows, listed = [], set()
    present = lan.present()
    for r in present:
        if not r.blind:
            rows.append({"name": f"Govee {r.sku}" if r.sku else "Govee light", "ip": r.ip, "state": "connected"})
            listed.add(r.ip)
    for v in cfg["known"].values():
        ip = v.get("ip") or ""
        if ip and ip not in listed:
            reach = lan.reach.get(ip, "checking")
            state = "silent" if reach == "quiet" else reach
            rows.append({"name": f"Govee {v.get('sku')}" if v.get("sku") else "Govee light", "ip": ip, "state": state})
            listed.add(ip)
    for ip in cfg["manual"]:
        if ip not in listed:
            reach = lan.reach.get(ip, "checking")
            name = "Govee device" if ip in cfg["seen"] else "Light added by address"
            rows.append({"name": name, "ip": ip, "state": "blind" if reach == "quiet" else reach})
            listed.add(ip)
    return rows


# ----------------------------------------------------------------------------- transport
class _Light:
    """What the network knows about one light."""

    def __init__(self, dev_id: str, ip: str, sku: str = "", version: str = ""):
        self.id, self.ip, self.sku, self.version = dev_id, ip, sku, version
        self.last_seen = 0.0
        self.blind = False  # added by address and never heard from: commands are sent without confirmation


def _local_ipv4() -> List[str]:
    """This machine's LAN addresses (a multicast scan has to leave through every interface)."""
    found: List[str] = []
    try:  # the address the OS would route LAN multicast through
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((SCAN_GROUP, SCAN_PORT))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    if not sys.platform.startswith("linux"):
        try:  # Windows / macOS answer this locally; on Linux it can mean a slow DNS lookup, see below instead
            found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            pass
    else:
        try:
            import fcntl

            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for _idx, name in socket.if_nameindex():
                    try:  # SIOCGIFADDR
                        packed = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
                        found.append(socket.inet_ntoa(packed[20:24]))
                    except OSError:
                        continue
            finally:
                probe.close()
        except (ImportError, OSError):
            pass
    return [ip for ip in dict.fromkeys(found) if not ip.startswith("127.") and ip != "0.0.0.0"]


class _Lan:
    """One UDP socket for everything: scans, commands, and the answers on LISTEN_PORT."""

    def __init__(self):
        self.cv = threading.Condition()
        self.sock: Optional[socket.socket] = None
        self.listening = False
        self.lights: Dict[str, _Light] = {}
        self.status: Dict[str, Tuple[float, dict]] = {}  # ip -> (when, devStatus data)
        self.last_scan = 0.0
        self.probed: frozenset = frozenset()
        self.reach: Dict[str, str] = {}  # address -> _reach() verdict, for addresses that are not answering scans
        self._reach_busy = False

    # ---- socket ------------------------------------------------------------------
    def start(self):
        with self.cv:
            if self.sock is not None:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", LISTEN_PORT))
                self.listening = True
            except OSError as e:
                # Another program owns the port (Govee Desktop, Home Assistant ...): commands still go
                # out, but no light can be discovered; lights added by address keep working.
                self.listening = False
                log.warning(
                    "can't listen on UDP %d for Govee lights (%s): only lights added by address work", LISTEN_PORT, e
                )
            self.sock = s
            threading.Thread(target=self._reader, args=(s,), name="rgbdeck-govee-rx", daemon=True).start()

    def stop(self):
        """Tests only: drop the socket and everything learned."""
        with self.cv:
            s, self.sock = self.sock, None
            self.lights.clear()
            self.status.clear()
            self.last_scan = 0.0
            self.probed = frozenset()
            self.reach.clear()
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def send(self, ip: str, cmd: str, data: dict, port: Optional[int] = None):
        s = self.sock
        if s is None:
            return
        payload = json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode()
        try:
            s.sendto(payload, (ip, CMD_PORT if port is None else port))
        except OSError as e:
            log.debug("send %s to %s failed: %s", cmd, ip, e)

    def scan(self):
        """Ask every light to announce itself: multicast on each interface, plus directly at known addresses."""
        s = self.sock
        if s is None:
            return
        self.last_scan = time.monotonic()
        request = {"account_topic": "reserve"}
        payload = json.dumps({"msg": {"cmd": "scan", "data": request}}, separators=(",", ":")).encode()
        try:
            multicast = ipaddress.ip_address(SCAN_GROUP).is_multicast
        except ValueError:
            multicast = False
        with self.cv:  # IP_MULTICAST_IF is socket state: one scan at a time
            if multicast:
                for local in _local_ipv4() or [""]:
                    try:
                        if local:
                            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
                        s.sendto(payload, (SCAN_GROUP, SCAN_PORT))
                    except OSError as e:
                        log.debug("scan via %s failed: %s", local or "default route", e)
            else:
                try:
                    s.sendto(payload, (SCAN_GROUP, SCAN_PORT))
                except OSError as e:
                    log.debug("scan failed: %s", e)
        cfg = _config()
        direct = (
            {v.get("ip") for v in cfg["known"].values()} | set(cfg["manual"]) | {r.ip for r in self.lights.values()}
        )
        for ip in direct:
            if ip and _valid_ip(ip):
                self.send(ip, "scan", request, SCAN_PORT)
        # addresses we were told about but that stay silent: find out why (off the caller's thread, it waits)
        wanted = [
            ip for ip in {v.get("ip") for v in cfg["known"].values()} | set(cfg["manual"]) if ip and _valid_ip(ip)
        ]
        if wanted and not self._reach_busy:
            self._reach_busy = True
            threading.Thread(target=self._check_reach, args=(wanted,), name="rgbdeck-govee-reach", daemon=True).start()

    def _check_reach(self, ips: List[str]):
        try:
            time.sleep(0.5)  # give scan answers time to arrive: an answering light needs no verdict
            now = time.monotonic()
            with self.cv:
                answering = {r.ip for r in self.lights.values() if r.last_seen and now - r.last_seen < 5.0}
            for ip in ips:
                if ip in answering:
                    self.reach.pop(ip, None)
                else:
                    self.reach[ip] = _reach(ip)
        finally:
            self._reach_busy = False

    def _reader(self, s: socket.socket):
        while self.sock is s:
            try:
                data, addr = s.recvfrom(8192)
            except OSError:
                # Windows reports a previous send's "port unreachable" here; a closed socket ends the loop
                if self.sock is not s:
                    return
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(data)["msg"]
                cmd, body = msg["cmd"], msg["data"]
            except (ValueError, KeyError, TypeError):
                continue  # other traffic that happens to hit the port
            if not isinstance(body, dict):
                continue
            if cmd == "scan":
                self._scan_reply(body, addr[0])
            elif cmd == "devStatus":
                with self.cv:
                    now = time.monotonic()
                    self.status[addr[0]] = (now, body)
                    for r in self.lights.values():
                        if r.ip == addr[0]:
                            r.last_seen = now
                    self.cv.notify_all()

    def _scan_reply(self, body: dict, src_ip: str):
        dev_id = str(body.get("device") or "")
        if not dev_id:
            return
        ip = str(body.get("ip") or src_ip)
        if not _valid_ip(ip):
            ip = src_ip
        sku = str(body.get("sku") or "").upper()
        version = str(body.get("wifiVersionSoft") or "")
        with self.cv:
            r = self.lights.get(dev_id)
            if r is None:
                r = self.lights[dev_id] = _Light(dev_id, ip, sku, version)
                log.info("Govee light %s (%s) at %s", sku or "?", dev_id, ip)
            elif r.ip != ip:
                log.info("Govee light %s moved from %s to %s", r.sku or dev_id, r.ip, ip)
            r.ip, r.sku, r.version = ip, sku or r.sku, version or r.version
            r.last_seen = time.monotonic()
            self.cv.notify_all()
        cfg = _config()
        if cfg["known"].get(dev_id) != {"ip": ip, "sku": r.sku}:
            cfg["known"][dev_id] = {"ip": ip, "sku": r.sku}
            _save_config()

    # ---- who is there ------------------------------------------------------------
    def present(self, now: Optional[float] = None) -> List[_Light]:
        """Lights answering scans, plus blind entries for addresses the user typed that never answered."""
        now = time.monotonic() if now is None else now
        cfg = _config()
        with self.cv:
            out = [r for r in self.lights.values() if r.last_seen and now - r.last_seen < GONE_AFTER]
        out.sort(key=lambda r: r.id)
        # an address that once belonged to an answering light is that light: don't also show it blind
        taken = {r.ip for r in out} | {v.get("ip") for v in cfg["known"].values()}
        for ip in cfg["manual"]:
            # only an address that takes commands without objection is worth driving blind; one that refuses
            # them (LAN Control off) or is unreachable would be a device card that does nothing
            if ip not in taken and self.reach.get(ip) == "quiet":
                blind = _Light(f"ip:{ip}", ip)
                blind.blind = True
                out.append(blind)
        return out

    def wait_until(self, deadline: float, done) -> bool:
        with self.cv:
            while not done():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cv.wait(left)
        return True


_lan = _Lan()


# ----------------------------------------------------------------------------- packets
def _packet(body: bytes) -> str:
    """One Bluetooth packet for ptReal: 19 bytes, zero padded, plus the XOR checksum, base64."""
    raw = bytearray(body[:19].ljust(19, b"\x00"))
    x = 0
    for b in raw:
        x ^= b
    raw.append(x)
    return base64.b64encode(bytes(raw)).decode()


def segment_packet(rgb: Tuple[int, int, int], segments: List[int]) -> str:
    """Colour for a set of segments (0-based): 33 05 15 01 r g b 00*5 <bitmask, bit n = segment n>."""
    mask = bytearray(7)
    for n in segments:
        if 0 <= n < MAX_SEGMENTS:
            mask[n // 8] |= 1 << (n % 8)
    return _packet(
        bytes([0x33, 0x05, 0x15, 0x01, rgb[0] & 0xFF, rgb[1] & 0xFF, rgb[2] & 0xFF, 0, 0, 0, 0, 0]) + bytes(mask)
    )


def scene_packet(code: int) -> str:
    return _packet(bytes([0x33, 0x05, 0x04, code & 0xFF]))


# ----------------------------------------------------------------------------- driver
class GoveeLight:
    keeps_state = True  # the light remembers colour, scene and power by itself: nothing to store on close
    follows_typing = True  # the app may pulse it along with a keyboard's typing effect

    def __init__(self, lan: _Lan, light: _Light, status: Optional[dict] = None):
        self._lan, self._light = lan, light
        self.model = lookup(light.sku)
        known = light.sku in MODELS
        # physical segments the light exposes; the user may show fewer LEDs (1 = treat it as one light)
        self._physical = self.model.segments
        if self._physical:
            self._max = self._physical
        else:
            self._max = 1 if known else UNKNOWN_MODEL_SEGMENTS
        self._count = self._physical or 1
        self._cv = threading.Condition()
        self._want: Optional[tuple] = None  # newest state not sent yet
        self._last_want: Optional[tuple] = None
        self._last_tx = 0.0
        self._confirm_at: Optional[float] = None
        self._closed = False
        self._on: Optional[bool] = None  # what the light is believed to be doing
        self._bri: Optional[int] = None
        self._sent: Optional[List[int]] = None  # segment colours as last sent (None: unknown / not in colour mode)
        self.device = self._build_device(status)
        self._thread = threading.Thread(target=self._run, name=f"rgbdeck-govee-{light.ip}", daemon=True)
        self._thread.start()

    # ---- device model ---------------------------------------------------------------
    def _build_device(self, status: Optional[dict]) -> Device:
        m, light = self.model, self._light
        bri_flag = MODE_FLAG_HAS_BRIGHTNESS if m.brightness else 0
        modes = [
            Mode(0, "Direct", V_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            Mode(1, "Off", V_OFF, 0),
        ]

        def add(name: str, value: int):
            modes.append(Mode(len(modes), name, value, bri_flag, brightness_min=1, brightness_max=100, brightness=100))

        if m.white:
            lo, hi = m.kelvin
            seen = set()
            for name, kelvin in WHITES:
                kelvin = max(lo, min(hi, kelvin))
                if kelvin not in seen:
                    seen.add(kelvin)
                    add(name, V_WHITE + kelvin)
        if m.scenes >= 0:
            for raw, code in sorted(SCENE_SETS[m.scenes].items()):
                name = SCENE_NAMES.get(raw) or raw.replace("_", " ").title()
                if not any(x.name == name for x in modes):
                    add(name, V_SCENE + code)
        name = f"Govee {light.sku}" if light.sku else "Govee light"
        if light.blind:
            name += f" ({light.ip})"
        dev = Device(
            0,
            "LED Strip" if self._physical else "Light",
            name,
            "Govee",
            f"Wi-Fi light at {light.ip}" + (" (no answer yet: sending blind)" if light.blind else ""),
            light.version,
            light.id,
            f"govee:{light.id}",
            0,
            modes,
            [],
            [],
            [],
        )
        self.device = dev
        self._layout([0] * self._count)
        if status:
            self._adopt_status(status)
        return dev

    def _layout(self, colors: List[int]):
        n = self._count
        self.device.zones = [Zone(0, "Segments" if self._max > 1 else "Light", 1, 1, self._max, n)]
        self.device.led_names = [f"Segment {i + 1}" for i in range(n)] if n > 1 else ["Light"]
        self.device.colors = (list(colors) + [0] * n)[:n]

    def _adopt_status(self, st: dict):
        """Start from what the light is showing (devStatus: onOff, brightness, color, colorTemInKelvin)."""
        try:
            on = bool(int(st.get("onOff", 0)))
            bri = max(0, min(100, int(st.get("brightness", 100))))
            col = st.get("color") or {}
            rgb = (int(col.get("r", 0)), int(col.get("g", 0)), int(col.get("b", 0)))
            kelvin = int(st.get("colorTemInKelvin", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return
        self._on, self._bri = on, bri
        dev = self.device
        if not on:
            dev.active_mode = 1
            return
        whites = [m for m in dev.modes if V_WHITE <= m.value < V_SCENE]
        if kelvin and whites:
            best = min(whites, key=lambda m: abs((m.value - V_WHITE) - kelvin))
            best.brightness = max(1, bri)
            dev.active_mode = best.index
            return
        shown = rgb_to_u32(*(int(c * bri / 100 + 0.5) for c in rgb))
        dev.colors = [shown] * self._count

    def resize_zone(self, zone_idx: int, count: int):
        """Show the light as this many LEDs (1 .. its segment count); they are spread over the real segments."""
        if zone_idx != 0:
            raise ValueError(f"zone {zone_idx} does not exist")
        with self._cv:
            self._count = max(1, min(self._max, int(count)))
            self._layout(self.device.colors)
            self._sent = None

    def _segments_of(self, led: int) -> List[int]:
        """Real segments behind one shown LED."""
        total = self._physical or self._count
        return [s for s in range(total) if s * self._count // total == led]

    # ---- what the app asks for ----------------------------------------------------
    def set_colors(self, colors: List[int]):
        colors = (list(colors) + [0] * self._count)[: self._count]
        self.device.colors = colors
        self._request(("direct", colors))

    def set_mode(self, mode: Mode, save: bool = False):  # noqa: ARG002 - the light always keeps its state
        self.device.active_mode = mode.index
        bri = max(1, min(100, int(mode.brightness or 100)))
        if mode.value == V_DIRECT:
            self._request(("direct", list(self.device.colors)), fresh=True)
        elif mode.value == V_OFF:
            self._request(("off",))
        elif mode.value >= V_SCENE:
            self._request(("scene", mode.value - V_SCENE, bri))
        else:
            self._request(("white", mode.value - V_WHITE, bri))

    def close(self):
        """Stop the sender; whatever is still unsent or unconfirmed goes out now, in full."""
        with self._cv:
            if self._closed:
                return
            self._closed = True
            final = self._want or (self._last_want if self._confirm_at is not None else None)
            self._want = None
            if final is not None:
                self._transmit(final, full=True)
            self._cv.notify_all()
        self._thread.join(timeout=1.0)

    # ---- sender ---------------------------------------------------------------------
    def _request(self, want: tuple, fresh: bool = False):
        with self._cv:
            if self._closed:
                return
            if fresh:
                self._sent = None
            self._want = want
            self._cv.notify_all()

    def _run(self):
        with self._cv:
            while not self._closed:
                now = time.monotonic()
                if self._want is not None:
                    due = self._last_tx + MIN_SEND_INTERVAL
                    if now < due:
                        self._cv.wait(due - now)
                        continue
                    want, self._want = self._want, None
                    self._transmit(want, full=False)
                    self._last_want = want
                    self._last_tx = time.monotonic()
                    self._confirm_at = self._last_tx + CONFIRM_DELAY
                    continue
                if self._confirm_at is not None:
                    if now < self._confirm_at:
                        self._cv.wait(self._confirm_at - now)
                        continue
                    self._confirm_at = None
                    if self._last_want is not None:
                        self._transmit(self._last_want, full=True)  # in case a datagram was lost
                    self._lan.send(self._light.ip, "devStatus", {})
                    continue
                self._cv.wait()

    def _refresh_beliefs(self):
        """A devStatus that arrived after our last command says what the light really does (the Govee app
        or a wall switch may have changed it)."""
        st = self._lan.status.get(self._light.ip)
        if not st or st[0] <= self._last_tx + 0.05:
            return
        try:
            self._on = bool(int(st[1].get("onOff", 0)))
            self._bri = int(st[1].get("brightness", 0))
        except (TypeError, ValueError, AttributeError):
            pass

    def _transmit(self, want: tuple, full: bool):
        """Turn a wanted state into datagrams. full=True repeats everything; otherwise only what changed."""
        self._refresh_beliefs()
        ip, send = self._light.ip, self._lan.send
        kind = want[0]

        def power(on: bool):
            if full or self._on is not on:
                send(ip, "turn", {"value": int(on)})
            self._on = on

        def brightness(value: int):
            if self.model.brightness and (full or self._bri != value):
                send(ip, "brightness", {"value": value})
            self._bri = value

        if kind == "off":
            power(False)
        elif kind == "white":
            power(True)
            send(ip, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": want[1]})
            brightness(want[2])
            self._sent = None
        elif kind == "scene":
            power(True)
            send(ip, "ptReal", {"command": [scene_packet(want[1])]})
            brightness(want[2])
            self._sent = None
        else:
            self._transmit_colors(want[1], full, power, brightness)

    def _transmit_colors(self, colors: List[int], full: bool, power, brightness):
        ip, send = self._light.ip, self._lan.send
        if not any(colors):  # all black is "off" for a room light
            power(False)
            return
        power(True)
        if not self.model.rgb:  # brightness-only light: the colour's level is its brightness
            brightness(max(1, max(max(u32_to_rgb(c)) for c in colors) * 100 // 255))
            return
        brightness(100)  # colours are absolute: the app does its own dimming
        if len(set(colors)) == 1:
            r, g, b = u32_to_rgb(colors[0])
            send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
            self._sent = list(colors)
            return
        prev = None if full else self._sent
        if prev is None or len(prev) != len(colors):
            changed = list(range(len(colors)))
        else:
            changed = [i for i, c in enumerate(colors) if c != prev[i]]
        by_color: Dict[int, List[int]] = {}
        for i in changed:
            by_color.setdefault(colors[i], []).extend(self._segments_of(i))
        if by_color:
            packets = [segment_packet(u32_to_rgb(c), segs) for c, segs in by_color.items()]
            send(ip, "ptReal", {"command": packets})
        self._sent = list(colors)


# ----------------------------------------------------------------------------- module API
def fingerprint() -> frozenset:
    """Lights present right now (the backend rescans when this differs from probed())."""
    if not _config()["enabled"]:
        return frozenset()
    _lan.start()
    if time.monotonic() - _lan.last_scan >= RESCAN_INTERVAL:
        _lan.scan()
    return frozenset(r.id for r in _lan.present())


def probed() -> frozenset:
    """The lights the last probe() handed out."""
    return _lan.probed


def probe() -> List[GoveeLight]:
    lan = _lan
    lan.probed = frozenset()
    cfg = _config()
    if not cfg["enabled"]:
        return []
    lan.start()
    t0 = time.monotonic()
    lan.scan()
    if lan.listening:
        expected = set(cfg["known"])

        def answered() -> bool:
            fresh = {r.id for r in lan.lights.values() if r.last_seen >= t0}
            return bool(expected) and expected <= fresh

        # Remembered lights get a moment to answer. Lights nobody has seen yet are picked up by the
        # hotplug poll a few seconds later, so a machine without any does not pay for the wait.
        lan.wait_until(t0 + (PROBE_WAIT if expected or cfg["manual"] else FIRST_WAIT), answered)
    answering = {r.ip for r in lan.lights.values() if r.last_seen >= t0}
    for ip in cfg["manual"]:
        if ip not in answering and ip not in lan.reach:
            lan.reach[ip] = _reach(ip)  # first time: decide now whether it can be driven blind
    lights = lan.present()
    asked = [r for r in lights if not r.blind]
    if asked and lan.listening:
        t1 = time.monotonic()
        for r in asked:
            lan.send(r.ip, "devStatus", {})
        lan.wait_until(t1 + STATUS_WAIT, lambda: all(lan.status.get(r.ip, (0.0,))[0] >= t1 for r in asked))
    found = []
    for r in lights:
        st = lan.status.get(r.ip)
        found.append(GoveeLight(lan, r, st[1] if st else None))
    lan.probed = frozenset(r.id for r in lights)
    return found


__all__ = [
    "USB_IDS",
    "GoveeLight",
    "fingerprint",
    "diagnose",
    "forget",
    "found_lights",
    "rescan",
    "search_network",
    "status_report",
    "get_settings",
    "probe",
    "probed",
    "scene_packet",
    "segment_packet",
    "set_settings",
]
