"""Hardware-free checks for the Govee LAN driver against a fake light on loopback: discovery, hotplug through the
backend, colours, segments, scenes, whites, power, rate limiting, the confirming resend, lights added by address."""

import base64
import json
import os
import select
import socket
import sys
import tempfile
import threading
import time
import types

TMP = tempfile.mkdtemp(prefix="rgbdeck-govee-")
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["APPDATA"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rgbdeck.backend as backend  # noqa: E402
from rgbdeck.hw import govee_lan as gl  # noqa: E402
from rgbdeck.models import rgb_to_u32, u32_to_rgb  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


gl.SCAN_GROUP = "127.0.0.1"  # the "multicast" scan goes straight to the fake light
gl.SCAN_PORT, gl.LISTEN_PORT, gl.CMD_PORT = free_port(), free_port(), free_port()
gl.CONFIRM_DELAY = 0.3


class FakeLight(threading.Thread):
    """Behaves like an H618F with LAN control on: answers scans, applies commands, reports devStatus."""

    def __init__(self, sku="H618F", dev_id="AA:BB:CC:DD:EE:FF:00:11", segments=15):
        super().__init__(daemon=True)
        self.sku, self.dev_id = sku, dev_id
        self.scan_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.scan_sock.bind(("127.0.0.1", gl.SCAN_PORT))
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.bind(("127.0.0.1", gl.CMD_PORT))
        self.on, self.brightness, self.kelvin, self.scene = True, 50, 0, None
        self.color = (10, 20, 30)
        self.segs = [self.color] * segments
        self.log = []  # (monotonic time, cmd, data)
        self.answer = True  # reply to scans / devStatus
        self.drop = 0  # ignore this many commands (lost datagrams)
        self.bad_checksum = 0
        self.stop = False

    def reply(self, addr, cmd, data):
        self.cmd_sock.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (addr[0], gl.LISTEN_PORT))

    def run(self):
        while not self.stop:
            ready, _, _ = select.select([self.scan_sock, self.cmd_sock], [], [], 0.05)
            for s in ready:
                try:
                    raw, addr = s.recvfrom(8192)
                except OSError:
                    continue
                msg = json.loads(raw)["msg"]
                cmd, data = msg["cmd"], msg["data"]
                if cmd == "scan":
                    if self.answer:
                        self.reply(
                            addr,
                            "scan",
                            {"ip": "127.0.0.1", "device": self.dev_id, "sku": self.sku, "wifiVersionSoft": "2.06.02"},
                        )
                    continue
                if s is not self.cmd_sock:
                    continue
                if cmd == "devStatus":
                    if self.answer:
                        r, g, b = self.color
                        self.reply(
                            addr,
                            "devStatus",
                            {
                                "onOff": int(self.on),
                                "brightness": self.brightness,
                                "color": {"r": r, "g": g, "b": b},
                                "colorTemInKelvin": self.kelvin,
                            },
                        )
                    continue
                if self.drop:
                    self.drop -= 1
                    continue
                self.log.append((time.monotonic(), cmd, data))
                if cmd == "turn":
                    self.on = bool(data["value"])
                elif cmd == "brightness":
                    self.brightness = data["value"]
                elif cmd == "colorwc":
                    self.kelvin = data["colorTemInKelvin"]
                    if not self.kelvin:
                        c = data["color"]
                        self.color = (c["r"], c["g"], c["b"])
                        self.segs = [self.color] * len(self.segs)
                    self.scene = None
                elif cmd == "ptReal":
                    for b64 in data["command"]:
                        p = base64.b64decode(b64)
                        x = 0
                        for byte in p:
                            x ^= byte
                        if len(p) != 20 or x != 0:
                            self.bad_checksum += 1
                            continue
                        if p[:4] == bytes([0x33, 0x05, 0x15, 0x01]):
                            mask = int.from_bytes(p[12:19], "little")
                            for n in range(len(self.segs)):
                                if mask >> n & 1:
                                    self.segs[n] = tuple(p[4:7])
                            self.scene, self.kelvin = None, 0
                        elif p[:3] == bytes([0x33, 0x05, 0x04]):
                            self.scene = p[3]

    def cmds(self, since=0.0):
        return [c for t, c, _d in self.log if t >= since]

    def close(self):
        self.stop = True
        self.join(1.0)
        self.scan_sock.close()
        self.cmd_sock.close()


def settle(t=0.25):
    time.sleep(t)


RED, GREEN, BLUE, BLACK = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255), 0

# --- packets
p = base64.b64decode(gl.segment_packet((255, 0, 0), [0, 14]))
check(
    len(p) == 20 and p[:7] == bytes([0x33, 5, 0x15, 1, 255, 0, 0]) and p[12] == 0x01 and p[13] == 0x40, "segment packet"
)
x = 0
for byte in p:
    x ^= byte
check(x == 0, "packet checksum is the XOR of the first 19 bytes")
p = base64.b64decode(gl.scene_packet(0x0A))
check(p[:4] == bytes([0x33, 5, 4, 0x0A]) and len(p) == 20, "scene packet")

# --- the backend sees network lights come and go
mod = types.ModuleType("rgbdeck.hw.unit_none")
backend.discover_drivers = lambda: [gl]
backend.hidraw.enumerate_hid = lambda *a, **k: []
gl.set_settings(True, [])
client = backend.LocalBackend()
t0 = time.monotonic()
client.refresh()
check(client.devices == [] and time.monotonic() - t0 < 0.6, "no lights: empty scan, and it does not wait long")
check(not client.poll_notifications(), "nothing there, nothing changed")

light = FakeLight()
light.start()
gl._lan.last_scan = 0.0  # as if the 30 s rescan timer came round
client.poll_notifications()
settle(0.3)
check(client.poll_notifications(), "a light that starts answering triggers a rescan")
client.refresh()
check(len(client.devices) == 1, f"light adopted ({[d.name for d in client.devices]})")
dev = client.devices[0]
names = [m.name for m in dev.modes]
check(
    dev.name == "Govee H618F" and dev.type_name == "LED Strip" and len(dev.colors) == 15,
    "H618F: strip with 15 segments",
)
check(dev.experimental and dev.serial == light.dev_id and dev.version == "2.06.02", "device details")
check(names[:5] == ["Direct", "Off", "Warm White", "Neutral White", "Cool White"], f"basic modes {names[:5]}")
check("Breathing" in names and "Rainbow" in names and "Snowflake" in names, "firmware scenes of the model listed")
check(
    dev.colors[0] == rgb_to_u32(5, 10, 15),
    f"starts from the light's own colour x brightness ({u32_to_rgb(dev.colors[0])})",
)
check(not client.poll_notifications(), "stable after adoption")
known = json.loads((gl.paths.config_dir() / "govee.json").read_text())["known"]
check(known.get(light.dev_id) == {"ip": "127.0.0.1", "sku": "H618F"}, "light remembered for the next start")
z = dev.zones[0]
check((z.leds_min, z.leds_max, z.leds_count) == (1, 15, 15), "segment count is adjustable from 1 to 15")
drv = client.driver_for(dev)
check(drv.keeps_state and drv.follows_typing, "driver flags")

# --- one colour: the documented colorwc, brightness forced to 100 once
t = time.monotonic()
client.update_leds(dev, [RED] * 15)
client.wait_idle()
settle()
check(light.on and light.brightness == 100 and light.color == (255, 0, 0), "uniform colour applied at full brightness")
check("ptReal" not in light.cmds(t) and "colorwc" in light.cmds(t), "uniform colour uses colorwc only")
settle(0.5)  # let the confirming resend pass

# --- paint one segment: one packet, nothing else
t = time.monotonic()
cols = [RED] * 15
cols[3] = GREEN
client.update_leds(dev, cols)
client.wait_idle()
settle(0.15)
first = [(c, d) for tt, c, d in light.log if tt >= t]
check(
    len(first) == 1 and first[0][0] == "ptReal" and len(first[0][1]["command"]) == 1,
    f"one changed segment = one packet ({first})",
)
check(
    light.segs[3] == (0, 255, 0) and light.segs[2] == (255, 0, 0) and light.segs[4] == (255, 0, 0),
    "only that segment changed",
)
settle(0.5)
check(light.bad_checksum == 0, "every packet had a valid checksum")

# --- many updates: rate limited, the last one wins
t = time.monotonic()
for i in range(40):
    client.update_leds(dev, [rgb_to_u32(i * 6, 0, 255 - i * 6)] * 15)
    time.sleep(0.01)
client.wait_idle()
settle(0.2)
n = len([c for c in light.cmds(t) if c == "colorwc"])
check(n <= 7, f"40 updates in 0.4 s became {n} datagrams (max 10 per second)")
check(light.color == (234, 0, 21), f"the last colour is what the light shows ({light.color})")
settle(0.5)

# --- a lost datagram is repaired by the confirming resend
light.drop = 1
client.update_leds(dev, [BLUE] * 15)
client.wait_idle()
settle(0.15)
check(light.color != (0, 0, 255), "(datagram dropped by the fake light)")
settle(0.5)
check(light.color == (0, 0, 255), "state re-sent after it settled")

# --- black = off, colour = back on
client.update_leds(dev, [BLACK] * 15)
client.wait_idle()
settle()
check(not light.on, "all black switches the light off")
client.update_leds(dev, [GREEN] * 15)
client.wait_idle()
settle()
check(light.on and light.color == (0, 255, 0), "a colour switches it back on")
settle(0.5)

# --- modes
by_name = {m.name: m for m in dev.modes}
client.update_mode(dev, by_name["Off"])
client.wait_idle()
settle()
check(not light.on, "Off mode")
warm = by_name["Warm White"]
warm.brightness = 40
client.update_mode(dev, warm)
client.wait_idle()
settle()
check(
    light.on and light.kelvin == 2700 and light.brightness == 40,
    f"Warm White at 40% ({light.kelvin} K, {light.brightness}%)",
)
client.update_mode(dev, by_name["Breathing"])
client.wait_idle()
settle()
check(light.scene == 0x0A and light.brightness == 100, f"Breathing scene (code {light.scene})")
client.update_mode(dev, by_name["Direct"])
client.update_leds(dev, [RED] * 15)
client.wait_idle()
settle()
check(light.scene is None and light.color == (255, 0, 0) and light.brightness == 100, "back to a plain colour")
settle(0.5)

# --- somebody switches the light off elsewhere: the next colour brings it back
light.on = False
client.update_leds(dev, [BLUE] * 15)
client.wait_idle()
settle(0.6)
check(light.on and light.color == (0, 0, 255), "light switched off behind our back comes on again")

# --- fewer LEDs than segments: each LED covers a run of segments
client.resize_zone(dev, 0, 5)
check(len(dev.colors) == 5 and dev.zones[0].leds_count == 5 and dev.led_names[4] == "Segment 5", "resized to 5 LEDs")
five = [RED, GREEN, BLUE, rgb_to_u32(255, 255, 0), rgb_to_u32(255, 0, 255)]
client.update_leds(dev, five)
client.wait_idle()
settle()
check(
    light.segs[0:3] == [(255, 0, 0)] * 3
    and light.segs[3:6] == [(0, 255, 0)] * 3
    and light.segs[12:15] == [(255, 0, 255)] * 3,
    "5 LEDs spread over 15 segments",
)
client.resize_zone(dev, 0, 1)
check(dev.led_names == ["Light"], "1 LED = the whole light")
settle(0.5)

# --- closing sends what is still pending
client.resize_zone(dev, 0, 15)
client.update_leds(dev, [GREEN] * 15)
client.update_leds(dev, [rgb_to_u32(1, 2, 3)] * 15)
client.close()
settle(0.1)
check(light.color == (1, 2, 3), "close() flushes the newest state")

# --- the light stops answering: gone after the grace period
client.refresh()
check(len(client.devices) == 1, "still there after a rescan")
light.answer = False
for rec in gl._lan.lights.values():
    rec.last_seen -= gl.GONE_AFTER + 1
check(client.poll_notifications(), "silent light is noticed")
client.refresh()
check(client.devices == [], "and removed")

# --- a light added by address works without ever answering (firewalled replies)
gl._config()["known"].clear()
gl._lan.lights.clear()
check(gl.set_settings(True, ["127.0.0.1", "not an ip"]), "settings changed")
check(gl.get_settings() == {"enabled": True, "manual": ["127.0.0.1"]}, "invalid addresses are dropped")
client.refresh()
check(
    len(client.devices) == 1 and client.devices[0].name == "Govee light (127.0.0.1)",
    f"blind light listed ({[d.name for d in client.devices]})",
)
blind = client.devices[0]
check(
    len(blind.colors) == 1 and blind.zones[0].leds_max == gl.UNKNOWN_MODEL_SEGMENTS,
    "unknown model: one LED, adjustable",
)
client.update_leds(blind, [RED])
client.wait_idle()
settle()
check(light.color == (255, 0, 0) and light.on, "commands reach it")
check(not client.poll_notifications(), "blind light is stable")

# --- switched off in settings
gl.set_settings(False, [])
check(client.poll_notifications(), "disabling is a change")
client.refresh()
check(client.devices == [] and gl.fingerprint() == frozenset(), "disabled: no lights, no scans")

# --- an unknown model that answers gets the generic feature set
gl.set_settings(True, [])
light.sku, light.answer = "H9ZZZ", True
gl._lan.last_scan = 0.0
client.poll_notifications()
settle(0.3)
client.refresh()
check(len(client.devices) == 1 and client.devices[0].type_name == "Light", "unknown model adopted as a plain light")
un = [m.name for m in client.devices[0].modes]
check(un == ["Direct", "Off", "Warm White", "Neutral White", "Cool White"], f"generic modes only ({un})")

client.shutdown()
light.close()
gl._reset_for_tests()
check(threading.active_count() < 6, f"no thread leak ({threading.active_count()})")
print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
