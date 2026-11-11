"""Corsair Commander Duo driver against a simulated hub that follows the documented Commander Core protocol."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.hw import corsair_commander_duo as duo  # noqa: E402
from rgbdeck.models import DeviceError, rgb_to_u32  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeHub:
    """Answers every request with one report echoing the opcode; keeps endpoints, port setup and colours."""

    def __init__(self, detected=((2, 34), (0, 0)), silent=False):
        self.detected, self.silent = detected, silent
        self.requests, self.queue = [], []
        self.mode = "hardware"
        self.open = {}  # handle -> endpoint
        self.manual, self.counts, self.restarts = None, None, 0
        self.pending = {}  # handle -> [bytes wanted, bytearray]
        self.frames = []
        self.drop_session = False
        self.closed = False

    def drain(self):
        pass

    def close(self):
        self.closed = True

    def write(self, data):
        assert len(data) == 65 and data[0] == 0x00 and data[1] == 0x08, data[:4].hex()
        req = bytes(data[2:])
        self.requests.append(req)
        if self.silent:
            return 65
        op = req[0]
        reply = bytearray(64)
        reply[1] = op
        if self.drop_session and op != duo.OP_MODE:
            reply[2] = duo.ERR_NO_SESSION
        elif op == duo.OP_MODE:
            self.mode = "software" if req[3] == 2 else "hardware"
            self.drop_session = False
        elif op == duo.OP_FIRMWARE:
            reply[3:7] = bytes([0, 9, 107, 0])
        elif op == duo.OP_OPEN:
            self.open[req[1]] = req[2]
        elif op == duo.OP_CLOSE:
            self.open.pop(req[2], None)
        elif op == duo.OP_READ:
            if self.open.get(req[1]) == duo.EP_LED_PORTS:
                reply[3:6] = bytes([0x0F, 0x00, 2])
                for i, (state, leds) in enumerate(self.detected):
                    reply[6 + 4 * i : 10 + 4 * i] = bytes([state, 0, leds & 0xFF, leds >> 8])
        elif op == duo.OP_WRITE:
            want = req[2] | req[3] << 8
            body = bytearray(req[6 : 6 + min(want, 57)])
            self.pending[req[1]] = [want, body]
            self._maybe_finish(req[1])
        elif op == duo.OP_WRITE_MORE:
            want, body = self.pending[req[1]]
            body += req[2 : 2 + min(61, want - len(body))]
            self._maybe_finish(req[1])
        elif op == duo.OP_LED_RESTART:
            self.restarts += 1
        self.queue.append(bytes(reply))
        return 65

    def _maybe_finish(self, handle):
        want, body = self.pending[handle]
        if len(body) < want:
            return
        del self.pending[handle]
        ep, dtype, data = self.open.get(handle), bytes(body[:2]), bytes(body[2:])
        if ep == duo.EP_COLORS and dtype == b"\x12\x00":
            self.frames.append(data)
        elif ep == duo.EP_PORT_MANUAL and dtype == b"\x0d\x00":
            self.manual = data
        elif ep == duo.EP_PORT_COUNT and dtype == b"\x0c\x00":
            self.counts = data

    def read(self, size=64, timeout=0.5):
        return self.queue.pop(0) if self.queue else None


duo.READ_TIMEOUT = 0.01
hub = FakeHub()
d = duo.CommanderDuo("fake", "Corsair Commander Duo", hid=hub)
check(d.version == "0.9.107", f"firmware version read ({d.version})")
check(hub.mode == "software", "the hub is put in software mode")
check(
    d.counts == [34, 0] and [z.leds_count for z in d.device.zones] == [34, 0], f"LEDs per port as detected ({d.counts})"
)
check(
    hub.open.get(duo.H_COLOR) == duo.EP_COLORS and duo.H_DATA not in hub.open,
    "colour endpoint left open, the others closed again",
)
check(d.device.zones[1].leds_max == 204 and len(d.device.led_names) == 34, "an empty port can be given up to 204 LEDs")

red, blue = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 0, 255)
n = len(hub.requests)
d.set_colors([red] * 17 + [blue] * 17)
check(
    len(hub.frames) == 1 and hub.frames[0] == b"\xff\x00\x00" * 17 + b"\x00\x00\xff" * 17,
    "colours arrive as r g b per LED",
)
sent = hub.requests[n:]
check(
    [r[:2].hex() for r in sent] == ["0600", "0700"],
    f"108 bytes travel as one write and one continuation ({[r[:2].hex() for r in sent]})",
)
check(sent[0][2:8] == bytes([104, 0, 0, 0, 0x12, 0x00]), "length counts data type + colours; data type 12 00")

d.resize_zone(1, 60)
check(
    hub.manual == bytes([2, 1, 0, 1, 1]) and hub.counts == bytes([2, 0, 0, 60, 0]),
    f"a port set by hand is announced with its count ({hub.manual.hex()} / {hub.counts.hex()})",
)
check(
    hub.restarts == 1 and hub.open.get(duo.H_COLOR) == duo.EP_COLORS,
    "the LED ports are restarted and the colour endpoint reopened",
)
check(
    len(d.device.colors) == 94 and d.device.colors[:17] == [red] * 17 and d.device.zones[1].start_idx == 34,
    "zones rebuilt, colours of the other port kept",
)
d.resize_zone(0, 500)
check(d.counts == [204, 60] and hub.counts == bytes([2, 204, 0, 60, 0]), "LED counts are capped at 204")
d.resize_zone(0, 34)
d.set_colors([red] * 34 + [blue] * 60)
check(hub.frames[-1] == b"\xff\x00\x00" * 34 + b"\x00\x00\xff" * 60, "port 1 first, then port 2")
big = [r for r in hub.requests[-6:] if r[0] in (6, 7)]
check(len(big) == 5 and all(len(r) == 63 for r in big), f"282 bytes need five reports ({len(big)})")

n = len(hub.frames)
d.keepalive()
check(len(hub.frames) == n + 1 and hub.frames[-1] == hub.frames[-2], "keepalive repeats the colours")
hub.drop_session = True
d.set_colors([blue] * 94)
check(
    hub.mode == "software" and hub.frames[-1] == b"\x00\x00\xff" * 94,
    "a dropped session is entered again and the colours re-sent",
)
try:
    d.resize_zone(5, 1)
    check(False, "bad zone")
except ValueError:
    check(True, "a port that does not exist is refused")
d.close()
check(hub.mode == "hardware" and hub.closed, "closing hands the LEDs back to the hub")

empty = duo.CommanderDuo("fake", "Corsair Commander Duo", hid=FakeHub(detected=((0, 0), (1, 12))))
check(empty.counts == [0, 0], "only ports in state 2 count as detected")
n = len(empty.hid.requests)
empty.set_colors([])
empty.keepalive()
check(
    not empty.hid.frames and empty.hid.requests[n:][-1][:2] == bytes([duo.OP_FIRMWARE, 0x13]),
    "no LEDs: nothing to colour, keepalive still talks to the hub",
)
dead = FakeHub(silent=True)
try:
    duo.CommanderDuo("fake", "Corsair Commander Duo", hid=dead)
    check(False, "silent hub")
except DeviceError as e:
    check("iCUE" in str(e) and dead.closed, "a hub that does not answer is skipped with a hint, and released")


class Info:
    def __init__(self, interface, descriptor=b""):
        self.interface, self.descriptor, self.path, self.pid = interface, descriptor, f"if{interface}", 0x0C56


vendor = bytes([0x06, 0x42, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0xC0])  # usage page ff42, usage 1, collection
order = [i.interface for i in duo._candidates([Info(1), Info(0), Info(2, vendor)])]
check(order == [2, 0, 1], f"the vendor interface is tried first, then interface 0 ({order})")
check((0x1B1C, 0x0C56) in duo.USB_IDS and duo.EXPERIMENTAL, "listed for the udev rule; marked experimental")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
