# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""Hardware-free checks for rgbdeck.hw.razer: packets are built through the driver with a fake HidDevice and
compared byte-for-byte against what OpenRGB's RazerController.cpp would send."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.models import DeviceError, NO_LED, MODE_COLORS_RANDOM, rgb_to_u32  # noqa: E402
from rgbdeck.hw.hidraw import HidInfo  # noqa: E402
from rgbdeck.hw import razer  # noqa: E402
from rgbdeck.hw.razer import RazerDevice, KeyboardLayout, razer_crc, descriptor_usages, build_keyboard_layout  # noqa: E402
from rgbdeck.hw.razer_devices import DEVICES, PIDS, USB_IDS, VID  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ----------------------------------------------------------------------------------------------- fake transport
class FakeHid:
    """Records feature reports; answers get_feature with a SUCCESS reply echoing the last command's ids."""

    def __init__(self, effects=None, regions=(0x05,), layout=1, paired=(), fail=False):
        self.sent = []
        self.gets = []
        self.closed = False
        self.effects = effects  # None -> firmware reports no effect list
        self.regions = regions
        self.layout = layout
        self.paired = paired
        self.fail = fail

    def send_feature(self, data):
        if self.fail:
            raise DeviceError("nope")
        self.sent.append(bytes(data))

    def get_feature(self, report_id, size):
        self.gets.append(report_id)
        req = self.sent[-1]
        r = bytearray(size)
        r[0], r[1], r[2], r[6], r[7], r[8] = report_id, 0x02, req[2], req[6], req[7], req[8]
        a = 9
        cls, cid = req[7], req[8]
        if (cls, cid) == (0x00, 0x81):
            r[a], r[a + 1] = 1, 7
        elif (cls, cid) == (0x00, 0x82):
            r[a : a + 22] = b"PM2012345678901234567\x01"
        elif (cls, cid) == (0x00, 0x86):
            r[a], r[a + 1] = self.layout, 0x00
        elif (cls, cid) == (0x00, 0xBF):
            r[a] = len(self.paired)
            for i, pid in enumerate(self.paired):
                r[a + 1 + 3 * i : a + 4 + 3 * i] = bytes((1, pid >> 8, pid & 0xFF))
        elif (cls, cid) == (0x0F, 0x80):
            if self.effects is None:
                r[1] = 0x05
            for i, reg in enumerate(self.regions):
                r[a + 5 * i] = reg
        elif (cls, cid) == (0x0F, 0x81):
            if self.effects is None:
                r[1] = 0x05
            else:
                r[6] = 1 + len(self.effects)
                r[a + 1 : a + 1 + len(self.effects)] = bytes(self.effects)
        return bytes(r)

    def close(self):
        self.closed = True


def pkt(tid, cls, cid, size, args, report_id=0):
    """Expected razer_report (91 bytes incl. hidraw report id) with the crc OpenRGB computes."""
    r = bytearray(91)
    r[0], r[2], r[6], r[7], r[8] = report_id, tid, size, cls, cid
    for off, val in args.items():
        if isinstance(val, (bytes, bytearray)):
            r[9 + off : 9 + off + len(val)] = val
        else:
            r[9 + off] = val
    crc = 0
    for b in r[3:89]:
        crc ^= b
    r[89] = crc
    return bytes(r)


def make(pid, **kw):
    hid = FakeHid(**kw)
    drv = RazerDevice(hid, pid, "/dev/hidrawX", DEVICES[pid])
    hid.sent.clear()
    hid.gets.clear()
    return drv, hid


razer.MIN_GAP = 0.0  # no pacing in the tests
razer.time.sleep = lambda s: None

# ----------------------------------------------------------------------------------------------- table
check(
    all(v == VID for v, _ in USB_IDS) and (VID, 0x0241) in USB_IDS and len(USB_IDS) > 170,
    f"USB_IDS lists {len(USB_IDS)} Razer PIDs",
)
check(
    0x0504 not in DEVICES and 0x0F1F not in DEVICES and 0x0F35 not in DEVICES,
    "Kraken / ARGB controller / Hanbo PIDs (other OpenRGB protocols) are excluded",
)
check(
    {d["type"] for d in DEVICES.values()}
    <= {"Keyboard", "Mouse", "Mousemat", "Headset", "Keypad", "Accessory", "LED Strip"},
    "device types are the RGB Deck type names",
)

# ----------------------------------------------------------------------------------------------- crc
static_ext = pkt(0x1F, 0x0F, 0x02, 0x09, {0: 0, 1: 5, 2: 1, 5: 1, 6: 0xFF})
check(static_ext[89] == 0xFE and razer_crc(static_ext) == 0xFE, "crc: XOR of bytes 3..88 (hand computed 0xFE)")
check(razer_crc(bytes(91)) == 0, "crc of an empty report is 0")

# ----------------------------------------------------------------------------------------------- BlackWidow 2019 (extended, led 5)
bw19, hid = make(0x0241)
d = bw19.device
check(
    d.type_name == "Keyboard" and d.version == "v1.7" and d.serial == "PM2012345678901234567",
    f"identity from firmware/serial replies: {d.version} {d.serial!r}",
)
check(
    [m.name for m in d.modes] == ["Direct", "Off", "Static", "Breathing", "Spectrum Cycle"],
    f"modes without a firmware effect list and no wave quirk: {[m.name for m in d.modes]}",
)
static = d.mode_named("Static")
static.colors = [rgb_to_u32(255, 0, 0)]
static.brightness = 200
bw19.set_mode(static, False)
check(hid.sent[0] == static_ext, "extended static report 0x0F:0x02 [0, led, 1, 0, 0, 1, r, g, b]")
check(hid.sent[1] == pkt(0x1F, 0x0F, 0x04, 0x03, {0: 0, 1: 5, 2: 200}), "extended brightness 0x0F:0x04 after the mode")
check(len(hid.sent) == 2, "exactly mode + brightness sent")

hid.sent.clear()
bw19.set_mode(d.mode_named("Off"), False)
check(
    hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x06, {1: 5, 2: 0}) and hid.sent[1][9 + 2] == 255,
    "Off: extended none report, brightness 255 for modes without a brightness slider",
)

breathing = d.mode_named("Breathing")
hid.sent.clear()
breathing.colors = [rgb_to_u32(1, 2, 3)]
bw19.set_mode(breathing, False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x09, {1: 5, 2: 2, 3: 1, 5: 1, 6: bytes((1, 2, 3))}), "breathing one colour")
hid.sent.clear()
breathing.colors = [rgb_to_u32(1, 2, 3), rgb_to_u32(4, 5, 6)]
bw19.set_mode(breathing, False)
check(
    hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x0C, {1: 5, 2: 2, 3: 2, 5: 2, 6: bytes((1, 2, 3, 4, 5, 6))}),
    "breathing two colours",
)
hid.sent.clear()
breathing.color_mode = MODE_COLORS_RANDOM
bw19.set_mode(breathing, False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x06, {1: 5, 2: 2}), "breathing random")
hid.sent.clear()
bw19.set_mode(d.mode_named("Spectrum Cycle"), False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x06, {1: 5, 2: 3}), "spectrum cycle extended")

# keyboard grid: BlackWidow 2019 layout = full size, every row shifted right by one, Logo inserted at (5,11)
z = d.zones[0]
check(
    z.matrix is not None and len(z.matrix) == 6 and len(z.matrix[0]) == 22 and z.start_idx == 0,
    "keyboard zone has a 6x22 matrix map",
)
check(
    z.matrix[0][0] == NO_LED and d.led_names[z.matrix[0][1]] == "Key: Escape", "Escape moved to (0,1), (0,0) is a gap"
)
check(d.led_names[z.matrix[3][2]] == "Key: A" and d.led_names[z.matrix[5][11]] == "Logo", "A at (3,2), Logo at (5,11)")
check(
    len(d.led_names) == len(d.colors) == z.leds_count == sum(1 for row in z.matrix for v in row if v != NO_LED),
    f"colors/led_names/zone agree ({len(d.led_names)} LEDs, gaps excluded)",
)
check(
    "Key: Number Pad 7" in d.led_names
    and "Key: Left Shift" in d.led_names
    and "Key: \\ (ANSI)" in d.led_names
    and "Key: # (ISO)" not in d.led_names
    and "Key: #" not in d.led_names,
    "ANSI names: numpad, modifiers, no ISO keys",
)

# direct frame: one extended custom-frame row per report, then the custom effect
hid.sent.clear()
colors = [0] * len(d.led_names)
colors[d.led_names.index("Key: A")] = rgb_to_u32(10, 20, 30)
bw19.set_mode(d.mode_named("Direct"), False)
hid.sent.clear()
bw19.set_colors(colors)
check(len(hid.sent) == 7, f"6 rows + custom-mode report ({len(hid.sent)} reports)")
row3 = bytearray(22 * 3)
row3[2 * 3 : 2 * 3 + 3] = (10, 20, 30)
check(
    hid.sent[3] == pkt(0x1F, 0x0F, 0x03, 22 * 3 + 5, {2: 3, 3: 0, 4: 21, 5: bytes(row3)}),
    "extended custom frame row 3 carries A's colour at column 2",
)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x03, 22 * 3 + 5, {2: 0, 3: 0, 4: 21}), "row 0 is all black")
check(hid.sent[6] == pkt(0x1F, 0x0F, 0x02, 0x0C, {2: 8}), "custom-frame effect select 0x0F:0x02 [0,0,8]")
hid.sent.clear()
bw19.set_mode(static, False)
hid.sent.clear()
bw19.set_colors(colors)
check(hid.sent == [], "colors are not pushed while an onboard effect is active (would switch to custom frame)")

# ----------------------------------------------------------------------------------------------- firmware effect list
bw19b, hid = make(0x0241, effects=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
names = [m.name for m in bw19b.device.modes]
check(
    names
    == ["Direct", "Off", "Static", "Breathing", "Spectrum Cycle", "Wave", "Reactive", "Starlight", "Ripple", "Fire"],
    f"effect list adds Wave/Reactive/Starlight/Ripple/Fire: {names}",
)
dd = bw19b.device
wave = dd.mode_named("Wave")
wave.direction = 0
bw19b.set_mode(wave, False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x06, {1: 5, 2: 4, 3: 2, 4: 0x28}), "wave left = direction 2, rate 0x28")
hid.sent.clear()
wave.direction = 1
bw19b.set_mode(wave, False)
check(hid.sent[0][9 + 3] == 1, "wave right = direction 1")
react = dd.mode_named("Reactive")
check(react.speed_min == 3 and react.speed_max == 1 and react.speed == 2, "reactive speed range LONG(3)..SHORT(1)")
react.colors, react.speed = [rgb_to_u32(9, 8, 7)], 3
hid.sent.clear()
bw19b.set_mode(react, False)
check(
    hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x09, {1: 5, 2: 5, 3: 0, 4: 3, 5: 1, 6: bytes((9, 8, 7))}),
    "reactive: effect 5, flags 0, duration 3 (long), one colour",
)
star = dd.mode_named("Starlight")
star.colors, star.speed, star.color_mode = [rgb_to_u32(1, 1, 1), rgb_to_u32(2, 2, 2)], 1, MODE_COLORS_RANDOM
hid.sent.clear()
bw19b.set_mode(star, False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x06, {1: 5, 2: 7, 3: 0, 4: 1, 5: 0}), "starlight random: no colours")
star.color_mode = 2
hid.sent.clear()
bw19b.set_mode(star, False)
check(
    hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x0C, {1: 5, 2: 7, 3: 2, 4: 1, 5: 2, 6: bytes((1, 1, 1, 2, 2, 2))}),
    "starlight two colours: flags 2",
)
hid.sent.clear()
bw19b.set_mode(dd.mode_named("Fire"), False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x09, {1: 5, 2: 9, 5: 1, 6: bytes((0xFF, 0, 0))}), "fire always sends red")
hid.sent.clear()
rip = dd.mode_named("Ripple")
rip.colors = [rgb_to_u32(3, 2, 1)]
bw19b.set_mode(rip, False)
check(hid.sent[0] == pkt(0x1F, 0x0F, 0x02, 0x09, {1: 5, 2: 6, 4: 0x1E, 5: 1, 6: bytes((3, 2, 1))}), "ripple rate 0x1E")

# a device without input (Firefly V2 mousemat, LED id 0 -> region list) never gets Reactive/Ripple
ff, hid = make(0x0C04, effects=[1, 2, 3, 4, 5, 6, 7], regions=(0x05, 0x10))
names = [m.name for m in ff.device.modes]
check(
    "Reactive" not in names and "Ripple" not in names and "Starlight" in names and "Wave" in names,
    f"mousemat: no input -> no Reactive/Ripple: {names}",
)
check(
    ff.device.type_name == "Mousemat"
    and ff.device.led_names[:2] == ["LED Strip 1", "LED Strip 2"]
    and len(ff.device.led_names) == 19,
    "mousemat LEDs named '<zone> <n>'",
)
ff.set_mode(ff.device.mode_named("Static"), False)
check(hid.sent[0][9 + 1] == 0 and hid.sent[1][9 + 1] == 0, "LED id 0 devices address region 0")

# ----------------------------------------------------------------------------------------------- BlackWidow Chroma (standard matrix)
bwc, hid = make(0x0203)
d = bwc.device
check(
    [m.name for m in d.modes] == ["Direct", "Off", "Static", "Breathing", "Spectrum Cycle", "Wave"],
    "standard-matrix keyboard: wave from the PID quirk list, no firmware query",
)
check(hid.sent == [], "standard-matrix devices are not asked for an effect list")
bwc.set_mode(d.mode_named("Static"), False)
check(
    hid.sent[0] == pkt(0x3F, 0x03, 0x0A, 0x04, {0: 6, 1: bytes((0, 255, 0))}), "standard static 0x03:0x0A [6, r, g, b]"
)
check(hid.sent[1] == pkt(0x3F, 0x03, 0x03, 0x03, {0: 0, 1: 5, 2: 255}), "standard brightness 0x03:0x03")
for mode, exp in (
    ("Off", pkt(0x3F, 0x03, 0x0A, 0x01, {0: 0})),
    ("Spectrum Cycle", pkt(0x3F, 0x03, 0x0A, 0x01, {0: 4})),
):
    hid.sent.clear()
    bwc.set_mode(d.mode_named(mode), False)
    check(hid.sent[0] == exp, f"standard {mode}")
hid.sent.clear()
bwc.set_mode(d.mode_named("Wave"), False)
check(hid.sent[0] == pkt(0x3F, 0x03, 0x0A, 0x02, {0: 1, 1: 1}), "standard wave right")
br = d.mode_named("Breathing")
br.colors = [rgb_to_u32(1, 2, 3), rgb_to_u32(4, 5, 6)]
hid.sent.clear()
bwc.set_mode(br, False)
check(
    hid.sent[0] == pkt(0x3F, 0x03, 0x0A, 0x08, {0: 3, 1: 2, 2: bytes((1, 2, 3, 4, 5, 6))}),
    "standard breathing two colours",
)
br.color_mode = MODE_COLORS_RANDOM
hid.sent.clear()
bwc.set_mode(br, False)
check(hid.sent[0] == pkt(0x3F, 0x03, 0x0A, 0x08, {0: 3, 1: 3}), "standard breathing random")

z = d.zones[0]
check(
    d.led_names[z.matrix[1][0]] == "Key: M1"
    and d.led_names[z.matrix[5][0]] == "Key: M5"
    and d.led_names[z.matrix[0][20]] == "Logo"
    and d.led_names[z.matrix[0][1]] == "Key: Escape",
    "BlackWidow Chroma: macro column, Logo at (0,20)",
)
bwc.set_mode(d.mode_named("Direct"), False)
hid.sent.clear()
colors = [0] * len(d.led_names)
colors[d.led_names.index("Key: M1")] = rgb_to_u32(7, 7, 7)
bwc.set_colors(colors)
row1 = bytearray(66)
row1[0:3] = (7, 7, 7)
check(
    hid.sent[1] == pkt(0x3F, 0x03, 0x0B, 66 + 4, {0: 0xFF, 1: 1, 2: 0, 3: 21, 4: bytes(row1)}),
    "standard custom frame row 1: 0x03:0x0B [0xFF, row, 0, 21, rgb...]",
)
check(hid.sent[6] == pkt(0x3F, 0x03, 0x0A, 0x02, {0: 5, 1: 0}), "standard custom effect select [5, 0]")

# ISO keyboard: BlackWidow V3 reporting UK layout
v3, hid = make(0x024E, layout=6)
names = v3.device.led_names
check(
    "Key: #" in names
    and "Key: \\ (ISO)" in names
    and "Key: \\ (ANSI)" not in names
    and "UK (ISO)" in v3.device.description,
    "UK layout code -> ISO grid (# and ISO backslash, no ANSI backslash)",
)
fresh = FakeHid(layout=6)
RazerDevice(fresh, 0x024E, "/dev/hidrawX", DEVICES[0x024E])
check(
    any(p[7:9] == b"\x00\x86" for p in fresh.sent) and fresh.gets and all(g == 0 for g in fresh.gets),
    "keyboard info (0x00:0x86) queried, replies read with report id 0",
)

# 60% board
mini, hid = make(0x0257)
z = mini.device.zones[0]
check(
    len(z.matrix) == 5
    and not any(n.startswith("Key: F") and n[6:].isdigit() for n in mini.device.led_names)
    and mini.device.led_names[z.matrix[0][1]] == "Key: Escape",
    "Huntsman Mini: 5 rows, no F row, Escape on the number row",
)

# multi-zone keyboard: BlackWidow V4 = keyboard 6x23 + two 9-LED underglow strips in an 8x23 device matrix
v4, hid = make(0x0287)
d = v4.device
check(
    [(zz.name, zz.leds_count) for zz in d.zones][1:] == [("Underglow Left", 9), ("Underglow Right", 9)]
    and d.zones[1].start_idx == d.zones[0].leds_count
    and d.zones[1].matrix is None,
    "V4 zones and start indices",
)
v4.set_mode(d.mode_named("Direct"), False)
hid.sent.clear()
colors = [0] * len(d.led_names)
colors[d.zones[1].start_idx] = rgb_to_u32(1, 0, 0)  # Underglow Left 1
colors[d.zones[2].start_idx + 8] = rgb_to_u32(0, 0, 9)  # Underglow Right 9
v4.set_colors(colors)
check(len(hid.sent) == 9, "8 rows + custom select")
row6 = bytearray(23 * 3)
row6[0:3] = (1, 0, 0)
row6[17 * 3 : 17 * 3 + 3] = (0, 0, 9)
check(
    hid.sent[6] == pkt(0x1F, 0x0F, 0x03, 23 * 3 + 5, {2: 6, 3: 0, 4: 22, 5: bytes(row6)}),
    "underglow LEDs follow the keyboard cells in the flat frame (row 6, cols 0 and 17)",
)

# ----------------------------------------------------------------------------------------------- Leviathan V2: report id 7, 2x9
lev, hid = make(0x0532)
check(len(lev.device.led_names) == 18 and lev.device.type_name == "Accessory", "Leviathan V2: 18 LEDs, Accessory")
lev.set_mode(lev.device.mode_named("Direct"), False)
hid.sent.clear()
hid.gets.clear()
cols = [rgb_to_u32(i, 0, 0) for i in range(18)]
lev.set_colors(cols)
exp_rows = [
    pkt(
        0x1F,
        0x0F,
        0x03,
        9 * 3 + 5,
        {2: r, 3: 0, 4: 8, 5: bytes(sum(((9 * r + c, 0, 0) for c in range(9)), ()))},
        report_id=0x07,
    )
    for r in range(2)
]
check(hid.sent[0] == exp_rows[0] and hid.sent[1] == exp_rows[1], "2-row custom frame with report id 0x07 in byte 0")
check(hid.sent[2] == pkt(0x1F, 0x0F, 0x02, 0x0C, {2: 8}, report_id=0x07) and len(hid.sent) == 3, "then custom select")
hid2 = FakeHid()
RazerDevice(hid2, 0x0532, "/dev/hidrawX", DEVICES[0x0532])
check(hid2.gets and all(g == 7 for g in hid2.gets), "Leviathan replies are read with report id 7")

# ----------------------------------------------------------------------------------------------- old per-LED devices
da, hid = make(PIDS["DEATHADDER_CHROMA"])
check(
    da.device.led_names == ["Logo", "Scroll Wheel"] and da.device.modes[0].name == "Direct", "DeathAdder Chroma zones"
)
hid.sent.clear()
da.set_colors([rgb_to_u32(1, 2, 3), rgb_to_u32(4, 5, 6)])
check(
    hid.sent[0] == pkt(0x1F, 0x03, 0x01, 0x05, {0: 0, 1: 1, 2: bytes((1, 2, 3))})
    and hid.sent[1] == pkt(0x1F, 0x03, 0x01, 0x05, {0: 0, 1: 4, 2: bytes((4, 5, 6))}),
    "DeathAdder Chroma: set LED rgb 0x03:0x01 for scroll wheel (1) then logo (4), as OpenRGB does",
)
check(
    hid.sent[2] == pkt(0x1F, 0x03, 0x02, 0x03, {1: 1, 2: 0})
    and hid.sent[3] == pkt(0x1F, 0x03, 0x02, 0x03, {1: 4, 2: 0}),
    "then LED effect 0 (static) on both",
)
hid.sent.clear()
da.set_mode(da.device.mode_named("Spectrum Cycle"), False)
check(
    hid.sent[0] == pkt(0x1F, 0x03, 0x02, 0x03, {1: 1, 2: 4}) and len(hid.sent) == 2,
    "per-LED spectrum = effect 4, no brightness report for this matrix type",
)

orb, hid = make(PIDS["ORBWEAVER_CHROMA"])
hid.sent.clear()
orb.set_colors([rgb_to_u32(n, 0, 0) for n in range(1, 21)])
r3 = bytes((16, 0, 0, 0, 0, 0, 17, 0, 0, 18, 0, 0, 19, 0, 0, 20, 0, 0))
check(
    hid.sent[0]
    == pkt(
        0x3F, 0x03, 0x0B, 15 + 4, {0: 0xFF, 1: 1, 2: 1, 3: 5, 4: bytes((1, 0, 0, 2, 0, 0, 3, 0, 0, 4, 0, 0, 5, 0, 0))}
    )
    and hid.sent[3] == pkt(0x3F, 0x03, 0x0B, 18 + 4, {0: 0xFF, 1: 4, 2: 1, 3: 6, 4: r3}),
    "Orbweaver: rows/cols shifted by one, hole in the last row",
)
check(hid.sent[4] == pkt(0x3F, 0x03, 0x0A, 0x02, {0: 5}), "Orbweaver custom select uses the standard report")

# ----------------------------------------------------------------------------------------------- brightness cap, device mode
dev2, hid = make(PIDS["DEATHADDER_ESSENTIAL_V2"])
check(dev2.device.modes[0].brightness_max == 100, "DeathAdder Essential V2 brightness tops out at 100")
cp = FakeHid()
RazerDevice(cp, PIDS["CHARGING_PAD_CHROMA"], "/dev/hidrawX", DEVICES[PIDS["CHARGING_PAD_CHROMA"]])
check(cp.sent[0] == pkt(0x1F, 0x00, 0x04, 0x02, {0: 3, 1: 0}), "Charging Pad is switched to software mode first")
blade = FakeHid()
b14 = RazerDevice(blade, PIDS["BLADE_14_2021"], "/dev/hidrawX", DEVICES[PIDS["BLADE_14_2021"]])
blade.sent.clear()
b14.keepalive()
check(blade.sent[0] == pkt(0x3F, 0x00, 0x84, 0x02, {}), "Blade 14 keepalive = get device mode")
check(not hasattr(bw19, "keepalive"), "other devices expose no keepalive")

# ----------------------------------------------------------------------------------------------- layout engine
kb = KeyboardLayout(1, 15)
check(
    len(kb.keys) == 104 and kb.rows == 6 and kb.cols == 21, f"full ANSI base: {len(kb.keys)} keys {kb.rows}x{kb.cols}"
)
check(
    kb.name_at(2, 13) == "Key: \\ (ANSI)" and kb.name_at(3, 12) == "" and kb.name_at(4, 1) == "",
    "ANSI drops # and ISO \\",
)
kb = KeyboardLayout(2, 15)
check(
    kb.name_at(2, 13) == "" and kb.name_at(3, 12) == "Key: #" and kb.name_at(4, 1) == "Key: \\ (ISO)", "ISO keeps them"
)
kb = KeyboardLayout(1, 1)
check(
    kb.rows == 5 and kb.name_at(0, 0) == "Key: Escape" and kb.name_at(0, 1) == "Key: 1", "60%: Escape replaces backtick"
)
kb = build_keyboard_layout("razer_tartarus_v2_layout", 1)
check(
    [k.name for k in kb.keys][:3] == ["Key: 01", "Key: 02", "Key: 03"] and kb.name_at(3, 4) == "Key: Scroll Wheel",
    "empty base + inserted keys (Tartarus V2)",
)

# ----------------------------------------------------------------------------------------------- detection
desc_mouse = bytes.fromhex("05010902a101c0")
desc_cc = bytes.fromhex("050c0901a101c0")
check(
    descriptor_usages(desc_mouse) == {(1, 2)} and descriptor_usages(desc_cc) == {(0x0C, 1)}, "descriptor usage parsing"
)
info_ok = HidInfo("/dev/hidraw9", VID, 0x0241, "Razer", 2, desc_mouse)
check(razer.node_matches(info_ok, DEVICES[0x0241]["detect"]), "BlackWidow 2019 on interface 2 usage 1/2 matches")
check(
    not razer.node_matches(HidInfo("/dev/hidraw9", VID, 0x0241, "", 0, desc_mouse), DEVICES[0x0241]["detect"]),
    "wrong interface rejected",
)
check(
    not razer.node_matches(HidInfo("/dev/hidraw9", VID, 0x0241, "", 2, desc_cc), DEVICES[0x0241]["detect"]),
    "wrong usage rejected",
)
check(
    razer.node_matches(
        HidInfo("/dev/hidraw9", VID, PIDS["SEIREN_EMOTE"], "", 1, desc_cc), DEVICES[PIDS["SEIREN_EMOTE"]]["detect"]
    ),
    "usage-only rule (Seiren Emote) matches any interface",
)

made = []


class ProbeHid(FakeHid):
    def __init__(self, path):
        super().__init__(paired=(PIDS["BASILISK_V3_PRO_WIRELESS"],) if "dock" in path else (), fail="bad" in path)
        made.append(path)


razer.HidDevice = ProbeHid
razer.enumerate_hid = lambda vid=None, pid=None: [
    HidInfo("/dev/hidraw-kb", VID, 0x0241, "", 2, desc_mouse),
    HidInfo("/dev/hidraw-kb-other", VID, 0x0241, "", 0, desc_mouse),  # keyboard interface, skipped
    HidInfo("/dev/hidraw-bad", VID, 0x0203, "", 2, desc_mouse),  # fails to answer
    HidInfo("/dev/hidraw-dock", VID, PIDS["MOUSE_DOCK_PRO"], "", 0, desc_mouse),
    HidInfo("/dev/hidraw-unknown", VID, 0x9999, "", 0, desc_mouse),
    HidInfo("/dev/hidraw-kraken", VID, 0x0504, "", 3, desc_mouse),
]
found = razer.probe()
names = [f.device.name for f in found]
check(
    names
    == ["Razer BlackWidow 2019", "Razer Mouse Dock Pro", "Razer Basilisk V3 Pro (Wireless) (Razer Mouse Dock Pro)"],
    f"probe: skips wrong interface, failing and unknown nodes, adds paired devices: {names}",
)
check(
    "/dev/hidraw-kb-other" not in made and "/dev/hidraw-unknown" not in made and "/dev/hidraw-kraken" not in made,
    "nodes that do not match are never opened",
)
check(
    found[2].wait_for_response
    and found[2].transaction_id == DEVICES[PIDS["BASILISK_V3_PRO_WIRELESS"]]["transaction_id"]
    and found[2].path == "/dev/hidraw-dock",
    "paired device talks through the dock with its own transaction id",
)

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
