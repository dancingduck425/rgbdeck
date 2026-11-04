"""Hardware-free checks for rgbdeck.hw.hyperx: exact packet bytes against the OpenRGB C++ the driver was ported
from. No /dev/hidraw is touched - a fake HidDevice records every feature report / output report."""
import os
import sys
from collections import deque
from typing import Deque, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.hw import hyperx  # noqa: E402
from rgbdeck.hw.hidraw import HidInfo  # noqa: E402
from rgbdeck.models import NO_LED, DeviceError, rgb_to_u32  # noqa: E402

fails: List[str] = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeHid:
    """Records writes like hidapi would hand them to hidraw (report id byte first, exact length)."""

    def __init__(self, replies: Optional[List[bytes]] = None):
        self.sent: List[Tuple[str, bytes]] = []
        self.replies: Deque[bytes] = deque(replies or [])
        self.path = "/dev/hidraw99"
        self.closed = False

    def write(self, data: bytes) -> int:
        self.sent.append(("write", bytes(data)))
        return len(data)

    def send_feature(self, data: bytes):
        self.sent.append(("feature", bytes(data)))

    def read(self, size: int = 64, timeout: float = 0.5) -> Optional[bytes]:
        return self.replies.popleft() if self.replies else None

    def get_feature(self, report_id: int, size: int) -> bytes:
        return bytes(size)

    def drain(self):
        pass

    def close(self):
        self.closed = True


def info(vid=0x0951, pid=0x0000, iface=0) -> HidInfo:
    return HidInfo("/dev/hidraw99", vid, pid, "fake", iface, b"")


def make(cls, pid=0, replies=None, **kw):
    hid = FakeHid(replies)
    drv = cls(hid, info(pid=pid), cls.__name__, **kw)
    return drv, hid


def quad(entries: List[Tuple[int, int, int]]) -> bytes:
    """Expected 65-byte "0x81 R G B" report built independently of the driver."""
    buf = bytearray(65)
    for i, (r, g, b) in enumerate(entries):
        buf[1 + 4 * i:5 + 4 * i] = (0x81, r, g, b)
    return bytes(buf)


RED, GREEN, BLUE, WHITE = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255), rgb_to_u32(255, 255, 255)


# --- table sanity ------------------------------------------------------------------------------------------------
def table_check(drv, label):
    dev = drv.device
    n = len(dev.led_names)
    check(sum(z.leds_count for z in dev.zones) == n, f"{label}: {n} LED names cover the zones")
    check(len(dev.colors) == n, f"{label}: colors sized to LED count")
    check(dev.modes[0].name == "Direct" and dev.modes[0].has_per_led, f"{label}: Direct mode first, per-LED")
    for z in dev.zones:
        if z.matrix is None:
            continue
        cells = [c for row in z.matrix for c in row if c != NO_LED]
        width = {len(row) for row in z.matrix}
        check(len(width) == 1, f"{label}: matrix rows have one width")
        check(all(z.start_idx <= c < z.start_idx + z.leds_count for c in cells), f"{label}: matrix indices in range")
        dupes = sorted({c for c in cells if cells.count(c) > 1})
        # OpenRGB's Alloy Origins matrix_map lists LED 71 (F12) twice (row 0 col 15 and row 1 col 14); kept verbatim.
        allowed = [71] if label == "HyperXAlloyOrigins" else []
        check(dupes == allowed, f"{label}: matrix indices unique (known upstream dupes: {allowed})")
    named = [x for x in dev.led_names if x.startswith("Key: ")]
    check(len(set(named)) == len(named), f"{label}: key names unique")


for cls, pid in ((hyperx.HyperXAlloyOrigins, 0x16E5), (hyperx.HyperXAlloyOrigins60, 0x1734),
                 (hyperx.HyperXAlloyOrigins65, 0x038F), (hyperx.HyperXAlloyElite2, 0x1711),
                 (hyperx.HyperXAlloyElite, 0x16BE), (hyperx.HyperXAlloyFPS, 0x16DC), (hyperx.HyperXOrigins2_65, 0x0CC2),
                 (hyperx.HyperXEve1800, 0x08C2), (hyperx.HyperXPulsefireSurge, 0x16D3),
                 (hyperx.HyperXPulsefireFPSPro, 0x16D7), (hyperx.HyperXPulsefireRaid, 0x16E4),
                 (hyperx.HyperXPulsefireHaste, 0x1727), (hyperx.HyperXPulsefireDart, 0x16E2),
                 (hyperx.HyperXMousemat, 0x1705), (hyperx.HyperXQuadCastS, 0x171F), (hyperx.HyperXQuadCast2S, 0x02B5)):
    d, _ = make(cls, pid)
    table_check(d, cls.__name__)

check(len(hyperx.ORIGINS_LED_NAMES) == 107, "Alloy Origins: 107 LEDs")
check(len(hyperx.ELITE2_LED_NAMES) == 128, "Alloy Elite 2: 128 LEDs")
check(len(hyperx.ELITE_KEY_NAMES) == 106 and len(hyperx.ELITE_KEY_OFFSETS) == 106, "Alloy Elite/FPS: 106 keys")
check(len(hyperx.ORIGINS60_LED_NAMES) == 71 and len(hyperx.ORIGINS65_LED_NAMES) == 77, "Origins 60/65: 71/77 LEDs")
check(len(hyperx.ORIGINS2_65_LED_NAMES) == 74, "Origins 2 65: 74 LEDs")
check(len(hyperx.CORE_DEFAULT_VALUES) == len(hyperx._CORE_BASE) == 89, "Origins Core: 89 base keys")
check(all(k in hyperx.ORIGINS_LED_NAMES for k in ("Key: Escape", "Key: Left Shift", "Key: Number Pad 7",
                                                   "Key: \\ (ANSI)", "Key: \\ (ISO)")), "canonical key spellings")


# --- Alloy Origins direct frame ---------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXAlloyOrigins, 0x16E5)
names = drv.device.led_names
check(names.index("Key: A") == 9 and drv.device.zones[0].matrix[3][2] == 9, "Origins: 'Key: A' is LED 9 at row 3 col 2")
frame = [0] * 107
frame[names.index("Key: Escape")] = RED
frame[names.index("Key: A")] = GREEN
frame[names.index("Key: Number Pad Enter")] = BLUE
drv.set_colors(frame)
pk = hid.sent
check(len(pk) == 10 and all(k == "feature" and len(b) == 65 for k, b in pk), "Origins: init + 9 feature reports, 65 B")
init = bytearray(65)
init[1], init[2], init[9] = 0x04, 0xF2, 0x09
check(pk[0][1] == bytes(init), "Origins: init packet 00 04 F2 .. [9]=09")
exp0 = [(0, 0, 0)] * 16
exp0[0] = (255, 0, 0)
exp0[9] = (0, 255, 0)
check(pk[1][1] == quad(exp0), "Origins: packet 1 carries Escape (slot 0) and A (slot 9)")
# slot 23 is the first blank; LED 23 (F3) lands in slot 24
drv.set_colors([WHITE if i == 23 else 0 for i in range(107)])
p2 = hid.sent[10 + 2][1]
check(p2[1 + 4 * 7:5 + 4 * 7] == bytes((0x81, 0, 0, 0)) and p2[1 + 4 * 8:5 + 4 * 8] == bytes((0x81, 255, 255, 255)),
      "Origins: blank slot 23 inserted before F3")
# LED 106 (Number Pad Enter) sits at slot 106 + 19 blanks = 125 -> packet 7 offset 13; packet 8 is the 0x81 pad
check(pk[8][1][1 + 4 * 13:5 + 4 * 13] == bytes((0x81, 0, 0, 255)), "Origins: Number Pad Enter in slot 125")
check(pk[9][1] == quad([(0, 0, 0)]), "Origins: 9th colour packet is the single 0x81 00 00 00 pad")
hid.sent.clear()
drv.keepalive()
check(len(hid.sent) == 10 and hid.sent[0][1] == bytes(init), "Origins: keepalive() resends the whole frame")
check(drv.keepalive_interval == 0.05, "Origins: OpenRGB keepalive threshold 50 ms")

# --- Alloy Origins 60 / 65 ----------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXAlloyOrigins60, 0x1734)
drv.set_colors([RED] + [0] * 70)
init60 = bytearray(65)
init60[1], init60[2], init60[9] = 0x04, 0xF2, 0x05
check(len(hid.sent) == 6 and hid.sent[0][1] == bytes(init60), "Origins 60: init [9]=05 + 5 packets")
check(hid.sent[5][1] == quad([(0, 0, 0)] * 7), "Origins 60: last packet holds the 7 remaining LEDs")
check(drv.device.led_names[0] == "Unused LED 0" and drv.device.led_names[61] == "Left Space", "Origins 60: names")
drv, hid = make(hyperx.HyperXAlloyOrigins65, 0x038F)
drv.set_colors([0] * 77)
check(len(hid.sent) == 6 and hid.sent[5][1] == quad([(0, 0, 0)] * 13), "Origins 65: 77 LEDs -> 16x4 + 13")

# --- Alloy Elite 2 direct frame ---------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXAlloyElite2, 0x1711)
names = drv.device.led_names
frame = [0] * 128
frame[names.index("Key: Escape")] = RED
frame[names.index("Key: F3")] = WHITE              # LED 23 -> slot 24 (slot 23 blank)
mute = names.index("Key: Media Mute")              # LED 98 (after Number Pad 2)
frame[mute] = GREEN
frame[names.index("RGB Strip 18")] = BLUE          # LED 127 -> slot 143
drv.set_colors(frame)
pk = hid.sent
check(len(pk) == 10 and all(k == "feature" and len(b) == 65 for k, b in pk), "Elite 2: init + 9 feature reports")
init2 = bytearray(65)
init2[1], init2[2] = 0x04, 0xF2
check(pk[0][1] == bytes(init2), "Elite 2: init packet 00 04 F2 (no byte 9)")
check(pk[1][1][1:5] == bytes((0x81, 255, 0, 0)), "Elite 2: Escape in slot 0")
check(pk[2][1][1 + 4 * 7:5 + 4 * 7] == bytes((0x81, 0, 0, 0)) and pk[2][1][1 + 4 * 8:5 + 4 * 8] == bytes((0x81, 255, 255, 255)),
      "Elite 2: blank slot 23, F3 in slot 24")
# slot(k) = k + number of blank slots before it; walk the stream like the C++ `*skip_idx == color_idx + skipped` loop
def slot_of(led: int, skips) -> int:
    pos = 0
    for s in range(1000):
        if s in skips:
            continue
        if pos == led:
            return s
        pos += 1
    raise AssertionError


sm = slot_of(mute, set(hyperx.ELITE2_SKIP))
check(mute == 98 and sm == 114, f"Elite 2: Media Mute is LED {mute} -> slot {sm}")
check(pk[1 + sm // 16][1][1 + 4 * (sm % 16):5 + 4 * (sm % 16)] == bytes((0x81, 0, 255, 0)), f"Elite 2: Media Mute in slot {sm}")
check(pk[9][1][61:65] == bytes((0x81, 0, 0, 255)), "Elite 2: RGB Strip 18 is the very last slot (143)")
check(all(pk[i][1][1 + 4 * j] == 0x81 for i in range(1, 10) for j in range(16)), "Elite 2: 144 slots, every one tagged 0x81")
check(drv.keepalive_interval == 1.0, "Elite 2: OpenRGB keepalive threshold 1 s")

# --- Alloy Elite RGB / FPS ---------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXAlloyElite, 0x16BE)
names = drv.device.led_names
frame = [0] * 128
frame[names.index("Key: Escape")] = rgb_to_u32(1, 2, 3)
frame[names.index("Key: Number Pad .")] = rgb_to_u32(4, 5, 6)     # key index 105 -> offset 0x95
frame[names.index("RGB Strip 1")] = rgb_to_u32(7, 8, 9)           # ext index 0 -> 0x08 / 0x29 / 0x39
frame[names.index("Key: Media Mute")] = rgb_to_u32(10, 11, 12)    # ext index 21 -> 0x12 / 0x22 / 0x32
drv.set_colors(frame)
pk = hid.sent
check(len(pk) == 4 and all(k == "feature" and len(b) == 264 for k, b in pk), "Elite RGB: 4 x 264-byte feature reports")
check([b[:4] for _, b in pk] == [bytes((7, 0x16, 1, 0xA0)), bytes((7, 0x16, 2, 0xA0)), bytes((7, 0x16, 3, 0xA0)),
                                 bytes((7, 0x16, 4, 0xA0))], "Elite RGB: direct headers R, G, B, extended")
check(pk[0][1][0x08] == 1 and pk[1][1][0x08] == 2 and pk[2][1][0x08] == 3, "Elite RGB: Escape at offset 0x08 per plane")
check(pk[0][1][0x95] == 4 and pk[1][1][0x95] == 5 and pk[2][1][0x95] == 6, "Elite RGB: Number Pad . at offset 0x95")
ext = pk[3][1]
check(ext[0x08] == 7 and ext[0x29] == 8 and ext[0x39] == 9, "Elite RGB: RGB Strip 1 in extended plane")
check(ext[0x12] == 10 and ext[0x22] == 11 and ext[0x32] == 12, "Elite RGB: Media Mute in extended plane")
check(all(ext[i] == 0 for i in range(0x94, 264)) and all(ext[i] == 0 for i in range(4, 8)), "Elite RGB: ext bytes outside 08..93 zero")
hid.sent.clear()
static = drv.device.modes[1]
drv.set_mode(static, False)
eff = hid.sent[0][1]
check(len(hid.sent) == 1 and eff[:3] == bytes((7, 0x02, 1)) and eff[0x09] == 1 and eff[0x0A] == 0x01,
      "Elite RGB: Static -> SendEffect profile 1 mode 01")
check(eff[0x1B:0x23] == bytes([0xFF] * 8) and eff[0x73:0x7B] == bytes([2] * 8) and eff[0x72] == 9,
      "Elite RGB: reactive none, spectrum colour mode, speed tail")
check(eff[0x6B:0x71] == bytes((9, 9, 5, 5, 6, 5)), "Elite RGB: fixed bytes 6B..70")
hid.sent.clear()
drv.set_colors(frame)
check(len(hid.sent) == 4 and hid.sent[0][1][:4] == bytes((7, 0x06, 1, 1)) and hid.sent[3][1][:4] == bytes((7, 0x06, 1, 4)),
      "Elite RGB: colours in Static mode go to profile 1 (07 06 01 <ch>)")
hid.sent.clear()
breathing = drv.device.modes[3]
breathing.colors = [rgb_to_u32(9, 8, 7), rgb_to_u32(6, 5, 4)]
breathing.speed = 3
drv.set_mode(breathing, False)
eff = hid.sent[0][1]
check(eff[0x0A] == 2 and eff[0x71] == 3 and eff[0x73] == 1 and eff[0x2A:0x2C] == bytes((9, 6)) and eff[0x42:0x44] == bytes((8, 5))
      and eff[0x5A:0x5C] == bytes((7, 4)) and eff[0x29] == 0, "Elite RGB: Breathing dual colours at 2A/42/5A + 2B/43/5B")
wave = drv.device.modes[2]
wave.direction = 1
hid.sent.clear()
drv.set_mode(wave, False)
check(hid.sent[0][1][0x0D] == 1 and hid.sent[0][1][0x0E] == 1 and hid.sent[0][1][0x0A] == 0, "Elite RGB: Wave direction bytes")

drv, hid = make(hyperx.HyperXAlloyFPS, 0x16DC)
drv.set_colors([rgb_to_u32(1, 2, 3)] + [0] * 105)
check(len(hid.sent) == 3 and [b[:4] for _, b in hid.sent] == [bytes((7, 0x16, 1, 0xA0)), bytes((7, 0x16, 2, 0xA0)),
                                                            bytes((7, 0x16, 3, 0xA0))], "Alloy FPS: 3 direct planes")
check(hid.sent[2][1][0x08] == 3 and len(hid.sent[2][1]) == 264, "Alloy FPS: blue plane carries Escape at 0x08")

# --- Alloy Origins Core -------------------------------------------------------------------------------------------
reply = bytearray(65)
reply[56] = hyperx.CORE_VARIANT_ANSI
drv, hid = make(hyperx.HyperXAlloyOriginsCore, 0x16E6, replies=[bytes(reply)])
q = hid.sent[0]
check(q[0] == "write" and len(q[1]) == 65 and q[1][1] == 0x10, "Origins Core: variant query 00 10 (65 B output report)")
names = drv.device.led_names
check(len(names) == 87 and "Key: #" not in names and "Key: \\ (ISO)" not in names and "Key: \\ (ANSI)" in names,
      "Origins Core ANSI: 87 keys, ISO-only keys removed")
check(names[0] == "Key: Escape" and names[1] == "Key: F1" and names[16] == "Key: `" and names[-1] == "Key: Right Arrow",
      "Origins Core: row-major key order")
m = drv.device.zones[0].matrix
check(len(m) == 6 and len(m[0]) == 17 and m[3][1] == names.index("Key: A") and m[2][13] == names.index("Key: \\ (ANSI)"),
      "Origins Core: 6x17 matrix maps rows/cols")
hid.sent.clear()
frame = [0] * 87
frame[names.index("Key: A")] = rgb_to_u32(11, 22, 33)       # LED value 26 -> pos 98
frame[names.index("Key: Right Arrow")] = rgb_to_u32(1, 2, 3)  # value 92 -> pos 7*48 + 8 + 2 = 346
drv.set_colors(frame)
pk = hid.sent
check(len(pk) == 7 and all(k == "write" for k, _ in pk) and [len(b) for _, b in pk] == [65] * 6 + [25],
      "Origins Core: 380 bytes in 6x60 + 20, written as len+5")
check([b[1] for _, b in pk] == [0xA2] * 7 and [b[2] for _, b in pk] == list(range(7)) and pk[6][1][4] == 20,
      "Origins Core: 0xA2 header, sequence, payload length")
buf = drv.color_buffer(frame)
check(buf[98] == 22 and buf[114] == 11 and buf[130] == 33, "Origins Core: A -> G@98 R@114 B@130")
check(buf[346] == 2 and buf[362] == 1 and buf[378] == 3, "Origins Core: Right Arrow (value 92) -> G@346 R@362 B@378")
check(pk[1][1][5 + 38] == 22 and pk[1][1][5 + 54] == 11 and pk[2][1][5 + 10] == 33, "Origins Core: bytes land in packets 1/2")
hid.sent.clear()
drv.set_mode(drv.device.modes[0], False)
check(hid.sent[0][1][1] == 0xA7 and hid.sent[0][1][4] == 1 and hid.sent[0][1][5] == 255 and len(hid.sent[0][1]) == 65,
      "Origins Core: set_mode sends brightness 00 A7 00 00 01 FF")
reply[56] = hyperx.CORE_VARIANT_ABNT2
drv, hid = make(hyperx.HyperXAlloyOriginsCore, 0x098F, replies=[bytes(reply)])
names = drv.device.led_names
keys = drv.keys
check(len(names) == 89 and "Key: \\ (ANSI)" not in names and names.count("Key: ;") == 1 and "Key: \\ (ISO)" in names,
      "Origins Core ABNT2: 89 keys (ANSI backslash gone, ISO backslash + inserted ; present)")
check(names[names.index("Key: ç") + 1] == "Key: ~", "Origins Core ABNT2: row-3 ; and ' shown under alternate names")
row4 = [(k.col, k.name, k.value) for k in keys if k.row == 4]
check((11, "Key: ;", 82) in row4 and (12, "Key: /", 83) in row4 and (13, "Key: Right Shift", 84) in row4
      and (15, "Key: Up Arrow", 85) in row4, "Origins Core ABNT2: ; and / swapped in with values 82/83")
check("Key: ç" in names and "Key: ~" in names and "Key: ´ `" in names, "Origins Core ABNT2: alternate names used")
drv, hid = make(hyperx.HyperXAlloyOriginsCore, 0x16E6, replies=[])
check(len(drv.device.led_names) == 87, "Origins Core: no reply -> ANSI")

# --- Origins 2 65 / Eve 1800 --------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXOrigins2_65, 0x0CC2)
names = drv.device.led_names
frame = [0] * 74
frame[names.index("Key: Escape")] = rgb_to_u32(1, 2, 3)
frame[names.index("Key: Right Arrow")] = rgb_to_u32(4, 5, 6)   # LED 73 -> packet 3 slot 13
drv.set_colors(frame)
pk = hid.sent
check(len(pk) == 5 and all(k == "write" and len(b) == 65 for k, b in pk), "Origins 2 65: init + 4 output reports")
check(pk[0][1][:4] == bytes((0x44, 1, 4, 0)) and [b[:4] for _, b in pk[1:]] == [bytes((0x44, 2, s, 0)) for s in range(4)],
      "Origins 2 65: 44 01 04 then 44 02 <seq>")
check(pk[1][1][4:7] == bytes((1, 2, 3)) and pk[4][1][4 + 39:7 + 39] == bytes((4, 5, 6)), "Origins 2 65: RGB triplets at 4+3i")
check(names[61] == "Key: \\ (ISO)" and drv.device.zones[0].matrix[3][1] == 61, "Origins 2 65: ISO backslash LED 61")

drv, hid = make(hyperx.HyperXEve1800, 0x08C2)
drv.set_colors([rgb_to_u32(1, 2, 3)] * 10)
check(len(hid.sent) == 2 and hid.sent[0][1][:4] == bytes((0x44, 1, 4, 0)) and hid.sent[1][1][:4] == bytes((0x44, 2, 0, 0))
      and hid.sent[1][1][4:34] == bytes((1, 2, 3)) * 10 and hid.sent[1][1][34] == 0, "Eve 1800: init + 10 zones")
hid.sent.clear()
m0 = drv.device.modes[0]
m0.brightness = 0x7F
drv.set_mode(m0, False)
check(hid.sent[0][1][:5] == bytes((0x40, 1, 0, 0, 0x7F)) and len(hid.sent) == 3, "Eve 1800: brightness 40 01 .. then frame")

# --- Pulsefire Surge --------------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXPulsefireSurge, 0x16D3)
names = drv.device.led_names
check(names[0] == "LED Strip LED 1" and names[31] == "LED Strip LED 32" and names[32] == "Logo", "Surge: LED names")
frame = [0] * 33
frame[0] = rgb_to_u32(1, 2, 3)
frame[31] = rgb_to_u32(4, 5, 6)
frame[32] = rgb_to_u32(7, 8, 9)
drv.set_colors(frame)
exp = bytearray(264)
exp[0], exp[1], exp[3] = 0x07, 0x14, 0xA0
exp[0x08], exp[0x28], exp[0x48] = 1, 2, 3
exp[0x27], exp[0x47], exp[0x67] = 4, 5, 6
exp[0x6C:0x6F] = (7, 8, 9)
check(hid.sent == [("feature", bytes(exp))], "Surge: single 264-byte 07 14 00 A0 report with R/G/B planes + logo")

drv, hid = make(hyperx.HyperXPulsefireFPSPro, 0x16D7)
drv.set_colors([rgb_to_u32(1, 2, 3)])
exp = bytearray(264)
exp[0], exp[1], exp[2:5], exp[8] = 0x07, 0x0A, (1, 2, 3), 0xA0
check(hid.sent == [("feature", bytes(exp))], "FPS Pro / Core: 07 0A R G B 00 00 00 A0")

drv, hid = make(hyperx.HyperXPulsefireRaid, 0x16E4)
drv.set_colors([rgb_to_u32(1, 2, 3), rgb_to_u32(4, 5, 6)])
exp = bytearray(264)
exp[0], exp[1], exp[2:8], exp[8] = 0x07, 0x0A, (1, 2, 3, 4, 5, 6), 0xA0
check(hid.sent == [("feature", bytes(exp))] and drv.keepalive_interval == 1.0, "Raid: 07 0A RGB RGB A0, keepalive 1 s")

drv, hid = make(hyperx.HyperXPulsefireHaste, 0x1727)
drv.set_colors([rgb_to_u32(1, 2, 3)])
s1 = bytearray(65)
s1[1], s1[2], s1[8] = 0x04, 0xF2, 0x02
s2 = bytearray(65)
s2[1], s2[2:5], s2[8] = 0x81, (1, 2, 3), 0x02
check(hid.sent == [("feature", bytes(s1)), ("feature", bytes(s2))], "Haste: setup + colour feature reports")

# --- Pulsefire Dart -----------------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXPulsefireDart, 0x16E2)
drv.set_colors([rgb_to_u32(1, 2, 3), rgb_to_u32(4, 5, 6)])
check(len(hid.sent) == 2 and all(k == "write" and len(b) == 65 for k, b in hid.sent), "Dart: two 65-byte output reports")
p_scroll, p_logo = hid.sent[0][1], hid.sent[1][1]
check(p_scroll[:5] == bytes((0, 0xD2, 0x10, 0x00, 0x08)) and p_scroll[5:11] == bytes((1, 2, 3, 1, 2, 3))
      and p_scroll[11] == 0x64 and p_scroll[12] == 0, "Dart: scroll wheel packet (static, brightness 64, speed 0)")
check(p_logo[:5] == bytes((0, 0xD2, 0x00, 0x00, 0x08)) and p_logo[5:8] == bytes((4, 5, 6)), "Dart: logo packet")
hid.sent.clear()
cycle = drv.device.modes[2]
drv.set_mode(cycle, True)
check(len(hid.sent) == 2 and hid.sent[0][1][2:4] == bytes((0x20, 0x12)) and hid.sent[0][1][12] == 0x32
      and hid.sent[1][1][:3] == bytes((0, 0xDE, 0xFF)), "Dart: Spectrum Cycle -> LED_ALL packet + save 00 DE FF")
check(cycle.speed_min == 0x64 and cycle.speed_max == 0x00, "Dart: OpenRGB's inverted speed range kept")

# --- Fury Ultra / mousemats ---------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXMousemat, 0x1705)
names = drv.device.led_names
check(len(names) == 20 and names[0] == "Underglow LED 1" and names[14] == "Underglow LED 15" and names[15] == "LED Strip LED 1"
      and names[19] == "LED Strip LED 5", "Fury Ultra: 15 underglow + 5 strip")
check([z.name for z in drv.device.zones] == ["Underglow", "LED Strip"] and drv.device.zones[1].start_idx == 15,
      "Fury Ultra: zones")
frame = [0] * 20
frame[0] = rgb_to_u32(10, 20, 30)
frame[19] = rgb_to_u32(40, 50, 60)
drv.set_colors(frame)
initm = bytearray(65)
initm[1], initm[2], initm[9] = 0x04, 0xF2, 0x02
e1 = [(0, 0, 0)] * 16
e1[0] = (10, 20, 30)
e2 = [(0, 0, 0)] * 16
e2[3] = (40, 50, 60)
check(hid.sent == [("feature", bytes(initm)), ("feature", quad(e1)), ("feature", quad(e2))],
      "Fury Ultra: init [9]=02 + two 16-entry 0x81 packets (32 slots, zero padded)")
drv, hid = make(hyperx.HyperXMousemat, 0x1741, underglow=2, strip=0)
check(drv.device.led_names == ["Underglow LED 1", "Underglow LED 2"] and len(drv.device.zones) == 1, "Pulsefire Mat XL: 2 LEDs")
drv.set_colors([RED, GREEN])
check(len(hid.sent) == 3 and hid.sent[1][1][1:9] == bytes((0x81, 255, 0, 0, 0x81, 0, 255, 0)), "Pulsefire Mat XL: frame")

# --- QuadCast S ---------------------------------------------------------------------------------------------------
drv, hid = make(hyperx.HyperXQuadCastS, 0x171F)
drv.set_colors([rgb_to_u32(1, 2, 3), rgb_to_u32(4, 5, 6)])
c = bytearray(65)
c[1:9] = (0x81, 1, 2, 3, 0x81, 4, 5, 6)
r = bytearray(65)
r[1], r[2], r[8], r[9] = 0x04, 0xF2, 0, 1
check(hid.sent == [("feature", bytes(c)), ("feature", bytes(r))], "QuadCast S: colour packet + register F2 (0, 1)")
hid.sent.clear()
drv.set_mode(drv.device.modes[0], True)
regs = [(b[2], b[8], b[9]) for _, b in hid.sent if b[1] == 0x04]
check(regs == [(0x53, 1, 0), (0x02, 0, 0), (0x23, 1, 0), (0x02, 0, 0), (0xF2, 0, 1)], "QuadCast S: save register sequence")
eot = [b for _, b in hid.sent if b[1] == 0x08]
check(len(eot) == 1 and eot[0][0x3C:0x41] == bytes((0x28, 1, 0, 0xAA, 0x55)), "QuadCast S: EOT packet")
check(hid.sent[1][1] == bytes(c) and len(hid.sent) == 8, "QuadCast S: frame packet inside the save transaction")
check(drv.device.type_name == "Microphone" and drv.device.led_names == ["Top", "Bottom"], "QuadCast S: model")

# --- QuadCast 2 S -------------------------------------------------------------------------------------------------
def echo(pkt: bytes) -> bytes:
    rep = bytearray(64)
    rep[14], rep[15] = pkt[0], pkt[1]
    return bytes(rep)


hid = FakeHid()
drv = hyperx.HyperXQuadCast2S(hid, info(0x03F0, 0x02B5, 1), "QuadCast 2 S")
# feed echoes lazily: every write queues its own echo so the driver's read() sees a valid reply
orig_write = hid.write


def write_with_echo(data: bytes) -> int:
    hid.replies.append(echo(data))
    return orig_write(data)


hid.write = write_with_echo
frame = [0] * 108
frame[0] = rgb_to_u32(1, 2, 3)
frame[107] = rgb_to_u32(4, 5, 6)
drv.set_colors(frame)
pk = hid.sent
check(len(pk) == 7 and all(k == "write" and len(b) == 64 for k, b in pk), "QuadCast 2 S: header + 6 colour packets, 64 B")
check(pk[0][1][:3] == bytes((0x44, 1, 6)) and [b[:3] for _, b in pk[1:]] == [bytes((0x44, 2, p)) for p in range(6)],
      "QuadCast 2 S: 44 01 06 then 44 02 <pkt>")
check(pk[1][1][4:7] == bytes((1, 2, 3)) and pk[6][1][4 + 3 * 7:7 + 3 * 7] == bytes((4, 5, 6)) and pk[6][1][4 + 3 * 8] == 0,
      "QuadCast 2 S: LED 0 first, LED 107 is slot 7 of packet 5")
check(drv.errors == 0, "QuadCast 2 S: echoes accepted")
check(drv.device.zones[0].matrix[8][10] == 0 and drv.device.zones[0].matrix[0][0] == 26, "QuadCast 2 S: 9x12 matrix")
hid.sent.clear()
drv.set_mode(drv.device.modes[0], True)
check(len(hid.sent) == 9 and hid.sent[0][1][:4] == bytes((0x44, 3, 1, 6)) and hid.sent[1][1][:3] == bytes((0x44, 4, 0))
      and hid.sent[7][1][:7] == bytes((0x42, 2, 0, 0, 0, 0xE8, 3)) and hid.sent[8][1][:5] == bytes((0x40, 1, 0, 0, 0xFF)),
      "QuadCast 2 S: save sequence")

# --- detection rules / probe() ------------------------------------------------------------------------------------
check((0x0951, 0x16E5) in hyperx.USB_IDS and (0x03F0, 0x02B5) in hyperx.USB_IDS and len(hyperx.USB_IDS) == 37,
      f"USB_IDS lists every PID ({len(hyperx.USB_IDS)})")
desc_ff01 = bytes((0x06, 0x01, 0xFF, 0x09, 0x01))
rule = next(r for r in hyperx.RULES if r.pid == 0x16E4)
check(hyperx._rule_matches(rule, HidInfo("/dev/hidraw1", 0x0951, 0x16E4, "", 1, desc_ff01)), "rule: Raid iface 1 / FF01 / 01")
check(not hyperx._rule_matches(rule, HidInfo("/dev/hidraw1", 0x0951, 0x16E4, "", 0, desc_ff01)), "rule: wrong interface rejected")
check(not hyperx._rule_matches(rule, HidInfo("/dev/hidraw1", 0x0951, 0x16E4, "", 1, bytes((0x05, 0x01)))), "rule: wrong page rejected")
check(hyperx._rule_matches(rule, HidInfo("/dev/hidraw1", 0x0951, 0x16E4, "", -1, b"")), "rule: unknown iface/descriptor accepted")

saved_enum, saved_hid = hyperx.enumerate_hid, hyperx.HidDevice
try:
    hyperx.enumerate_hid = lambda *a, **k: [HidInfo("/dev/hidraw77", 0x0951, 0x16E5, "x", 0, b"")]

    class Boom:
        def __init__(self, path):
            raise DeviceError("permission denied (test)")

    hyperx.HidDevice = Boom
    check(hyperx.probe() == [], "probe(): unopenable device is skipped, no exception")

    class FakeOpen(FakeHid):
        def __init__(self, path):
            super().__init__()
            self.path = path

    hyperx.HidDevice = FakeOpen
    hyperx.enumerate_hid = lambda *a, **k: [HidInfo("/dev/hidraw77", 0x0951, 0x16E5, "x", 0, b""),
                                           HidInfo("/dev/hidraw78", 0x0951, 0x16E5, "x", 1, b"")]
    got = hyperx.probe()
    check(len(got) == 1 and got[0].device.name == "HyperX Alloy Origins" and got[0].device.type_name == "Keyboard",
          "probe(): one driver for the interface-0 node of an Alloy Origins")

    def raising(*a, **k):
        raise OSError("sysfs gone")

    hyperx.enumerate_hid = raising
    check(hyperx.probe() == [], "probe(): enumeration failure returns []")
finally:
    hyperx.enumerate_hid, hyperx.HidDevice = saved_enum, saved_hid

print()
if fails:
    print(f"{len(fails)} FAILED:")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("all hyperx checks passed")
