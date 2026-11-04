"""Hardware-free checks for rgbdeck.hw.steelseries: a fake HidDevice records every write / feature report
and we assert the exact bytes OpenRGB's C++ would have produced."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.models import NO_LED, rgb_to_u32  # noqa: E402
from rgbdeck.hw import steelseries as ss  # noqa: E402
from rgbdeck.hw.hidraw import HidInfo  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeHid:
    def __init__(self, replies=None):
        self.writes = []
        self.features = []
        self.replies = list(replies or [])
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def send_feature(self, data):
        self.features.append(bytes(data))

    def get_feature(self, report_id, size):
        return bytes(size)

    def read(self, size=64, timeout=0.5):
        return self.replies.pop(0) if self.replies else None

    def drain(self):
        pass

    def close(self):
        self.closed = True

    def clear(self):
        self.writes.clear()
        self.features.clear()


def pad(seq, n):
    return bytes(seq) + bytes(n - len(seq))


RED, GREEN, BLUE, WHITE = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255), rgb_to_u32(255, 255, 255)

# --- detection table ------------------------------------------------------------------------------
check(len(ss.USB_IDS) == len(set(ss.USB_IDS)) == len(ss.DEVICES), f"USB_IDS unique ({len(ss.USB_IDS)} PIDs)")
check(all(v == 0x1038 for v, _p in ss.USB_IDS), "all IDs use the SteelSeries VID")
check(
    (0x1038, 0x184C) in ss.USB_IDS and (0x1038, 0x1612) in ss.USB_IDS and (0x1038, 0x150D) in ss.USB_IDS,
    "Rival 3, Apex 7 and QcK Prism XL are listed",
)

# --- Rival 3: static color + save -----------------------------------------------------------------
hid = FakeHid()
drv = ss.Rival3Mouse(hid, 0x184C, "SteelSeries Rival 3", "/dev/hidraw9")
check(hid.writes == [bytes([0x00, 0x10, 0x00])], "Rival 3 asks for its firmware version on open")
check(
    drv.device.type_name == "Mouse"
    and [m.name for m in drv.device.modes] == ["Direct", "Breathing", "Spectrum Cycle", "Rainbow Breathing"],
    "Rival 3 mode list matches OpenRGB",
)
check(drv.device.led_names == ["Front", "Middle", "Rear", "Logo"], "Rival 3 LED names")
hid.clear()
drv.set_colors([RED, GREEN, BLUE, WHITE])
expect = [
    bytes([0x00, 0x05, 0x00, 0x01, 255, 0, 0, 0x64]),
    bytes([0x00, 0x05, 0x00, 0x02, 0, 255, 0, 0x64]),
    bytes([0x00, 0x05, 0x00, 0x03, 0, 0, 255, 0x64]),
    bytes([0x00, 0x05, 0x00, 0x04, 255, 255, 255, 0x64]),
    bytes([0x00, 0x06, 0x00, 0x04]),
]
check(hid.writes == expect, "Rival 3 direct frame: 4 x color packet + effect 0x04")
hid.clear()
drv.set_mode(drv.device.modes[0], save=True)
check(
    hid.writes[:5] == expect and hid.writes[5] == pad([0x00, 0x09], 10) and len(hid.writes) == 6,
    "Rival 3 Direct + save -> colors, effect, 10-byte 0x09 save",
)
hid.clear()
breathing = drv.device.modes[1]
breathing.speed = 2
drv.set_mode(breathing, save=False)
check(hid.writes[-1] == bytes([0x00, 0x06, 0x00, 0x01]), "Rival 3 Breathing speed 2 -> effect 0x01 (fastest)")
hid.clear()
drv.set_mode(drv.device.modes[2], save=False)
check(hid.writes == [bytes([0x00, 0x06, 0x00, 0x00])], "Rival 3 Spectrum Cycle sends only the effect byte")

# --- Rival 600: zone colors -----------------------------------------------------------------------
hid = FakeHid()
drv = ss.RivalMouse(hid, 0x1724, "SteelSeries Rival 600", "/dev/hidraw4", ss.RIVAL_600)
check(hid.writes == [], "Rival 600 does not query firmware")
check(
    drv.device.led_names
    == ["Logo", "Scroll Wheel", "Left top", "Left mid", "Left bottom", "Right top", "Right mid", "Right bottom"],
    "Rival 600 LED names",
)
check([z.name for z in drv.device.zones] == ["Logo", "Scroll Wheel", "Mouse"], "Rival 600 zones")
drv.set_colors([RED, GREEN, BLUE, 0, 0, 0, 0, WHITE])
check(
    hid.writes[0] == bytes([0x1C, 0x27, 0x00, 0x02, 255, 0, 0]),
    "Rival 600 logo (id 1) -> raw 7-byte 0x1C write, mask 1<<1",
)
check(hid.writes[1] == bytes([0x1C, 0x27, 0x00, 0x01, 0, 255, 0]), "Rival 600 wheel (id 0) -> mask 1<<0")
check(hid.writes[2] == bytes([0x1C, 0x27, 0x00, 0x04, 0, 0, 255]), "Rival 600 left top (id 2) -> mask 1<<2")
check(hid.writes[7] == bytes([0x1C, 0x27, 0x00, 0x80, 255, 255, 255]), "Rival 600 right bottom (id 7) -> mask 0x80")
hid.clear()
drv.set_mode(drv.device.modes[0], save=True)
check(
    len(hid.writes) == 9 and hid.writes[-1] == pad([0x00, 0x09], 10), "Rival 600 save -> 0x09 after the 8 color writes"
)

# --- Rival 300 / 100 / 700 -----------------------------------------------------------------------
hid = FakeHid(replies=[bytes([0x2A, 0x01])])
drv = ss.RivalMouse(hid, 0x1710, "SteelSeries Rival 300", "/dev/hidraw4", ss.RIVAL_300)
check(
    hid.writes == [bytes([0x00, 0x10, 0x00])] and drv.device.version == "298", "Rival 300 firmware query + LE16 version"
)
hid.clear()
pulsate = drv.device.modes[1]
pulsate.speed = 3
drv.device.colors = [RED, BLUE]
drv.set_mode(pulsate, save=False)
check(
    hid.writes
    == [
        pad([0x00, 0x07, 0x01, 0x03], 10),
        pad([0x00, 0x07, 0x02, 0x03], 10),
        pad([0x00, 0x08, 0x01, 255, 0, 0], 10),
        pad([0x00, 0x08, 0x02, 0, 0, 255], 10),
    ],
    "Rival 300 Pulsate mid: per-zone effect then per-zone color (zone+1)",
)
hid = FakeHid()
drv = ss.RivalMouse(hid, 0x1702, "SteelSeries Rival 100", "/dev/hidraw4", ss.RIVAL_100)
drv.set_mode(drv.device.modes[0], save=False)
check(
    hid.writes == [pad([0x00, 0x07, 0x00, 0x01], 10), pad([0x00, 0x05, 0x00, 0, 0, 0], 10)],
    "Rival 100 direct: 0x07 effect + 0x05 color",
)
hid = FakeHid()
drv = ss.RivalMouse(hid, 0x1700, "SteelSeries Rival 700", "/dev/hidraw4", ss.RIVAL_700)
hid.clear()
drv.set_colors([RED, GREEN])
f = hid.features[1]
check(
    len(hid.features) == 2
    and len(f) == 579
    and f[1] == 0x05
    and f[3] == 1
    and f[4:7] == bytes([0, 255, 0])
    and f[0x0C] == 1
    and f[0x0D] == 1
    and not hid.writes,
    "Rival 700 wheel -> 579-byte feature report",
)

# --- Rival 650 -------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.RivalMouse(hid, 0x172B, "SteelSeries Rival 650", "/dev/hidraw4", ss.RIVAL_650)
drv.set_colors([RED] + [0] * 7)
check(len(hid.writes) == 32 and all(len(w) == 61 for w in hid.writes), "Rival 650: 4 x 61-byte packets per LED")
w = hid.writes[0]
check(
    w[1] == 0x03
    and w[0x1F + 1 : 0x22 + 1] == bytes([255, 0, 0])
    and w[0x2F:0x32]
    == bytes(
        [
            255,
            0,
            0,
        ]
    )[:3]
    and w[0x32] == 0x56,
    "Rival 650 first packet carries the color twice",
)
check(
    hid.writes[2][1] == 0x05 and hid.writes[2][3] == 0x11 and hid.writes[2][4] == 0xFF and hid.writes[2][9] == 0x5C,
    "Rival 650 third packet selects the logo (0x11)",
)

# --- Sensei TEN --------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.SenseiMouse(hid, 0x1832, "SteelSeries Sensei TEN", "/dev/hidraw4")
drv.set_colors([RED, BLUE])
w = hid.writes[1]
check(
    len(hid.writes) == 2
    and len(w) == 66
    and w[1] == 0x5B
    and w[3] == 1
    and w[0x14] == 1
    and w[0x1C] == 1
    and w[0x1D:0x20] == bytes([0, 0, 255])
    and w[0x20:0x23] == bytes([0, 0, 255]),
    "Sensei direct color packet (wheel)",
)
hid.clear()
rainbow = drv.device.modes[2]
drv.set_mode(rainbow, save=False)
w = hid.writes[0]
check(
    w[1] == 0x5B and w[5] == 0x27 and w[4] == 0x10 and w[0x1C] == 0x07 and w[0x1D] == 0xFF and w[0x3B] == 0x14,
    "Sensei rainbow mid speed packet",
)

# --- Aerox 3 wireless (2.4 GHz) ----------------------------------------------------------------------
hid = FakeHid()
drv = ss.AeroxWirelessMouse(hid, 0x1838, "SteelSeries Aerox 3 Wireless", "/dev/hidraw4", ss.AEROX_3_WIRELESS)
check(
    hid.features[0] == pad([0x00, 0x2D], 64) and hid.features[1] == pad([0x00, 0x90, 0x00], 64),
    "Aerox init + version feature reports",
)
check([m.name for m in drv.device.modes] == ["Direct", "Rainbow Breathing"], "Aerox wireless modes")
hid.clear()
drv.set_colors([RED, GREEN, BLUE])
check(
    hid.writes[0] == pad([0x00, 0x61, 0x01, 0x00, 255, 0, 0], 64),
    "Aerox wireless color has the 0x40 flag (0x21 -> 0x61)",
)
check(hid.writes[1] == pad([0x00, 0x63, 0x0A], 64), "Aerox wireless brightness 100 -> 10 steps, sent once")
check(len(hid.writes) == 4, "Aerox wireless: brightness only on change")
hid.clear()
drv.set_mode(drv.device.modes[1], save=True)
check(
    hid.writes == [pad([0x00, 0x62, 0xFF], 64), pad([0x00, 0x51, 0x00], 64)], "Aerox wireless Rainbow Breathing + save"
)
hid = FakeHid()
drv = ss.AeroxWirelessMouse(hid, 0x1858, "SteelSeries Aerox 9 Wireless", "/dev/hidraw4", ss.AEROX_9_WIRELESS_WIRED)
hid.clear()
drv.set_colors([0, 0, 0, RED])
check(
    hid.writes[4] == pad([0x00, 0x26, 0x01, 0x00, 255, 0, 0], 64)
    and hid.writes[1] == pad([0x00, 0x23, 0x0A], 64)
    and len(hid.writes) == 5,
    "Aerox 9 wired: reactive LED uses 0x26, no wireless flag",
)

# --- Aerox 3 / 5 wired -------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.Aerox3Mouse(hid, 0x1836, "SteelSeries Aerox 3 Wired", "/dev/hidraw4")
hid.clear()
drv.set_colors([RED, GREEN, BLUE])
check(
    hid.writes[0] == pad([0x00, 0x21, 0x01, 255, 0, 0], 65)
    and hid.writes[1] == pad([0x00, 0x23, 0x64], 65)
    and hid.writes[2] == pad([0x00, 0x21, 0x02, 0, 0, 0, 0, 255, 0], 65)
    and hid.writes[3] == pad([0x00, 0x21, 0x04, 0, 0, 0, 0, 0, 0, 0, 0, 255], 65),
    "Aerox 3 wired: bitmask + zone offset",
)
hid = FakeHid()
drv = ss.Aerox5Mouse(hid, 0x1850, "SteelSeries Aerox 5 Wired", "/dev/hidraw4")
hid.clear()
drv.set_colors([RED, GREEN, BLUE, WHITE])
check(
    hid.writes[2] == pad([0x00, 0x21, 0x02, 0, 0, 0, 0, 255, 0], 65)
    and hid.writes[3] == pad([0x00, 0x21, 0x04] + [0] * 6 + [0, 0, 255], 65)
    and hid.writes[4] == pad([0x00, 0x26, 0x01, 0x00, 255, 255, 255], 65),
    "Aerox 5 wired zones incl. reactive 0x26",
)

# --- Rival 5 -------------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.Rival5Mouse(hid, 0x183C, "SteelSeries Rival 5", "/dev/hidraw4")
drv.set_colors([RED] * 10)
check(
    hid.features[0] == pad([0x00, 0x23, 0x64], 64)
    and hid.features[1] == pad([0x00, 0x21, 0xFF, 0x03] + [255, 0, 0] * 10, 64),
    "Rival 5: brightness then 10-LED feature frame",
)
hid.clear()
drv.set_mode(drv.device.modes[1], save=False)
check(hid.features == [pad([0x00, 0x26, 0x01, 0x00, 255, 255, 255], 64)], "Rival 5 Reactive white")
hid.clear()
drv.set_mode(drv.device.modes[2], save=False)
check(hid.features == [pad([0x00, 0x22, 0xFF, 0x03], 64)], "Rival 5 Rainbow")
hid.clear()
drv.set_mode(drv.device.modes[0], save=False)
check(
    hid.features[0] == pad([0x00, 0x26], 64) and len(hid.features) == 2,
    "Rival 5 back to Direct disables reactive, no brightness resend",
)

# --- Apex 7: per-key frame ---------------------------------------------------------------------------
hid = FakeHid()
drv = ss.ApexKeyboard(hid, 0x1612, "SteelSeries Apex 7", "/dev/hidraw7")
check(
    hid.writes == [pad([0x00, 0xFF], 65), pad([0x00, 0x90], 65), pad([0x00, 0x90, 0x01], 65), pad([0x00, 0xA3], 65)]
    and not hid.features,
    "Apex 7 (Gen 1) open: serial, firmware x2, brightness query; no init feature report",
)
check(drv.device.type_name == "Keyboard" and len(drv.device.led_names) == 112, "Apex 7 exposes 112 keys")
check(
    [m.name for m in drv.device.modes] == ["Direct", "Onboard"]
    and not (drv.device.modes[1].flags & ss.MODE_FLAG_HAS_BRIGHTNESS),
    "Apex 7 modes: Direct, Onboard (no brightness without a reply)",
)
names = drv.device.led_names
check(
    names[0] == "Key: A"
    and names[37] == "Key: Escape"
    and names[106] == "Key: Number Pad 7"
    and names[88] == "Key: \\ (ANSI)"
    and names[45] == "Key: # (ISO)"
    and names[80] == "Key: Left Shift",
    "Apex key names use the canonical spellings",
)
m = drv.device.zones[0].matrix
check(
    len(m) == 6
    and all(len(r) == 22 for r in m)
    and m[0][0] == 37
    and m[3][2] == 0
    and m[2][18] == 106
    and m[0][1] == NO_LED,
    "Apex ANSI matrix map copied",
)
hid.clear()
colors = [0] * 112
colors[0], colors[37], colors[106] = RED, GREEN, BLUE
drv.set_colors(colors)
f = hid.features[0]
check(
    len(hid.features) == 1 and len(f) == 643 and f[0] == 0 and f[1] == 0x3A and f[2] == 112 and not hid.writes,
    "Apex 7 direct = one 643-byte feature report, id 0x3A, 112 keys",
)
check(f[3:7] == bytes([0x04, 255, 0, 0]), "key A -> usage 0x04 red at offset 3")
check(f[3 + 37 * 4 : 7 + 37 * 4] == bytes([0x29, 0, 255, 0]), "Escape -> usage 0x29 green")
check(f[3 + 106 * 4 : 7 + 106 * 4] == bytes([0x5F, 0, 0, 255]), "Number Pad 7 -> usage 0x5F blue")
check(
    f[3 + 111 * 4 : 7 + 111 * 4] == bytes([0xFB, 0, 0, 0]) and f[3 + 87 * 4] == 0xF0,
    "last key 0xFB (media), Right Fn 0xF0",
)
check(all(b == 0 for b in f[3 + 112 * 4 :]), "frame tail is zero")
hid.clear()
drv.set_mode(drv.device.modes[1], save=False)
check(hid.writes == [pad([0x00, 0x3B], 65)], "Apex 7 Onboard mode -> 0x3B hand-back")
hid.clear()
drv.set_mode(drv.device.modes[0], save=False)
check(
    not hid.writes and len(hid.features) == 1 and hid.features[0][1] == 0x3A,
    "Gen 1 back to Direct just resends the frame",
)
drv.close()
check(hid.writes == [pad([0x00, 0x3B], 65)] and hid.closed, "close() hands the LEDs back to the firmware")

# Apex 7 with a brightness reply and a TKL serial
hid = FakeHid(
    replies=[
        b"64646XXXXXXXXXXXXXX" + bytes(45),
        b"\x90" + b"1.2.3" + bytes(58),
        b"4.5.6" + bytes(59),
        bytes([0xA3, 0x00, 0x07]),
    ]
)
drv = ss.ApexKeyboard(hid, 0x1618, "SteelSeries Apex 7 TKL", "/dev/hidraw7")
check(
    drv.device.serial == "64646XXXXXXXXXXXXXX" and drv.device.version == "KBD: 1.2.3 / LED: 4.5.6",
    "Apex serial / version parsing",
)
check(
    drv.device.modes[1].flags & ss.MODE_FLAG_HAS_BRIGHTNESS and drv.device.modes[1].brightness == 7,
    "Apex brightness read back (7/10)",
)
m = drv.device.zones[0].matrix
check(
    m[0][17] == 111 and m[1][18] == NO_LED and m[5][18] == NO_LED and m[0][0] == 37,
    "TKL SKU patch: media key at Pause, numpad gone",
)
hid.clear()
ob = drv.device.modes[1]
ob.brightness = 4
drv.set_mode(ob, save=False)
check(
    hid.writes == [pad([0x00, 0x23, 0x04], 65), pad([0x00, 0x3B], 65)], "Apex Onboard with brightness -> 0x23 then 0x3B"
)

# Apex 9 TKL (Gen 2): 513-byte frame, id 0x40, init feature report only for Gen 3
hid = FakeHid()
drv = ss.ApexKeyboard(hid, 0x1634, "SteelSeries Apex 9 TKL", "/dev/hidraw7")
check(not hid.features and drv.device.serial == "64847", "Apex 9 TKL: Gen 2, fixed SKU 64847")
hid.clear()
drv.set_colors([RED] * 112)
f = hid.features[0]
check(len(f) == 513 and f[1] == 0x40 and f[2] == 112, "Apex 9 TKL direct = 513-byte feature report, id 0x40")
# Apex Pro TKL 2023 wireless (Gen 3): 0x4B init, 0x61 frame, 0x41 hand-back
hid = FakeHid()
drv = ss.ApexKeyboard(hid, 0x1630, "SteelSeries Apex Pro TKL 2023 Wireless", "/dev/hidraw7")
check(hid.features == [pad([0x00, 0x4B], 65)], "Gen 3 init feature report 0x4B")
hid.clear()
drv.set_colors([RED] * 112)
check(hid.features[0][1] == 0x61 and len(hid.features[0]) == 643, "Gen 3 wireless frame id 0x61")
hid.clear()
drv.set_mode(drv.device.modes[1], save=False)
drv.set_mode(drv.device.modes[0], save=False)
check(
    hid.writes == [pad([0x00, 0x41], 65)] and hid.features[0] == pad([0x00, 0x4B], 65) and hid.features[1][1] == 0x61,
    "Gen 3 Onboard -> 0x41, back to Direct re-inits with 0x4B then frame",
)
# Apex Pro TKL (0x1614) on new firmware upgrades itself to Gen 3
hid = FakeHid(replies=[b"\x90" + b"1.19.7" + bytes(57)])
drv = ss.ApexKeyboard(hid, 0x1614, "SteelSeries Apex Pro TKL", "/dev/hidraw7")
check(drv.quirk == ss.APEX_GEN3 and hid.features == [pad([0x00, 0x4B], 65)], "Apex Pro TKL fw 1.19.7 -> Gen 3 protocol")

# --- Apex M750 --------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.ApexMKeyboard(hid, 0x0616, "SteelSeries Apex M750", "/dev/hidraw7")
check(
    len(hid.features) == 3
    and all(len(f) == 513 for f in hid.features)
    and hid.features[0][4:7] == bytes([1, 0, 0x85])
    and hid.features[1][4:8] == bytes([3, 1, 0, 0xFF])
    and hid.features[2][4:8] == bytes([1, 0, 0x85, 0xFF]),
    "M750 enable sequence",
)
hid.clear()
colors = [0] * 112
colors[79], colors[0] = RED, GREEN  # Left Control (slot 0), A (row 2 slot 1 = slot 45)
drv.set_colors(colors)
f = hid.features[0]
check(
    len(f) == 513
    and f[3:9] == bytes([1, 0x8E, 1, 3, 6, 0x16])
    and f[9:12] == bytes([255, 0, 0])
    and f[9 + 3 * 3 : 12 + 3 * 3] == bytes([0xFF, 0x32, 0x00])
    and f[9 + 45 * 3 : 12 + 45 * 3] == bytes([0, 255, 0])
    and f[9 + 23 * 3 : 12 + 23 * 3] == bytes([0, 0, 0]),
    "M750 frame slots",
)

# --- Apex 3 (10 zones): direct + save ------------------------------------------------------------------
hid = FakeHid()
drv = ss.ApexTZoneKeyboard(hid, 0x161A, "SteelSeries Apex 3", "/dev/hidraw7")
check(drv.device.led_names == [f"LED {i}" for i in range(10)], "Apex 3 LED names")
drv.set_colors([RED, GREEN] + [BLUE] * 8)
check(
    hid.writes[0] == pad([0x00, 0x0A, 0x00, 0x64], 33)
    and hid.writes[1] == pad([0x00, 0x0B, 0x00, 255, 0, 0, 0, 255, 0] + [0, 0, 255] * 8, 33),
    "Apex 3 brightness + 10-zone color packets (33 bytes)",
)
hid.clear()
drv.set_mode(drv.device.modes[0], save=True)
check(hid.writes[2:] == [pad([0x00, 0x06, 0x00, 0x08], 33), pad([0x00, 0x09, 0x00, 0x00], 33)], "Apex 3 save sequence")

# --- Apex 3 TKL (8 zones): static frame + Rainbow Wave effect ---------------------------------------------
hid = FakeHid()
drv = ss.Apex8ZoneKeyboard(hid, 0x1622, "SteelSeries Apex 3 TKL", "/dev/hidraw7")
check([m.name for m in drv.device.modes] == ["Direct", "Rainbow Wave"], "Apex 3 TKL modes")
drv.set_colors([RED] * 8)
check(
    hid.writes == [pad([0x00, 0x21, 0xFF] + [255, 0, 0] * 8, 65), pad([0x00, 0x23, 0x10], 65)],
    "Apex 3 TKL static frame + brightness 0x10",
)
hid.clear()
drv.set_mode(drv.device.modes[1], save=False)
check(hid.writes == [pad([0x00, 0x22, 0xFF], 65)], "Apex 3 TKL Rainbow Wave = 0x22 0xFF, brightness unchanged")

# --- Old Apex ----------------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.OldApexKeyboard(hid, 0x1202, "SteelSeries Apex (OG)/Apex Fnatic", "/dev/hidraw7")
drv.set_colors([RED, GREEN, BLUE, WHITE, 0])
check(
    hid.writes == [pad([0x00, 0x07, 0x00, 255, 0, 0, 8, 0, 255, 0, 8, 0, 0, 255, 8, 255, 255, 255, 8, 0, 0, 0, 8], 33)],
    "Old Apex 5-zone packet with brightness 8",
)

# --- QcK Prism ---------------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.QckPrismMat(hid, 0x150D, "SteelSeries QCK Prism Cloth XL", "/dev/hidraw3")
check(
    drv.device.type_name == "Mousemat" and drv.device.led_names == ["Mat Bottom LED", "Mat Top LED"], "QcK Prism model"
)
drv.set_colors([RED, BLUE])
f = hid.features[0]
exp = bytearray(525)
exp[0x01], exp[0x03] = 0x0E, 0x02
exp[0x05:0x08] = bytes([255, 0, 0])
exp[0x08], exp[0x09], exp[0x0A], exp[0x0E] = 0xFF, 0x32, 0xC8, 0x01
exp[0x11:0x14] = bytes([0, 0, 255])
exp[0x14], exp[0x15], exp[0x16] = 0xFF, 0x32, 0xC8
exp[0x19], exp[0x1A], exp[0x1C] = 0x01, 0x01, 0x01
check(len(hid.features) == 1 and f == bytes(exp), "QcK Prism 525-byte feature report")
check(hid.writes == [pad([0x00, 0x0D], 65)], "QcK Prism 0x0D apply write")

# --- Headsets ----------------------------------------------------------------------------------------------
hid = FakeHid()
drv = ss.SiberiaHeadset(hid, 0x1229, "SteelSeries Siberia 350", "/dev/hidraw3")
drv.set_colors([rgb_to_u32(10, 20, 30)])
check(
    hid.writes
    == [
        pad([0x01, 0x00, 0x95, 2, 0x80, 0xBF], 16),
        pad([0x01, 0x00, 0x80, 2, 0x52, 0x20], 16),
        pad([0x01, 0x00, 0x83, 3, 10, 20, 30], 16),
        pad([0x01, 0x00, 0x93, 2, 0x03, 0x80], 16),
    ],
    "Siberia 350 four 16-byte packets, report id 0x01",
)
hid = FakeHid()
drv = ss.Arctis5Headset(hid, 0x1250, "SteelSeries Arctis 5", "/dev/hidraw3")
drv.set_colors([RED, BLUE])
check(
    len(hid.writes) == 12 and all(len(w) == 37 and w[0] == 0x06 for w in hid.writes),
    "Arctis 5: 6 x 37-byte packets per zone",
)
check(
    hid.writes[2] == pad([0x06, 0x8A, 0x42, 0x00, 0x20, 0x41, 0x00, 255, 0, 0, 0xFF, 0x32, 0xC8, 0xC8], 37)
    and hid.writes[3] == pad([0x06, 0x8A, 0x42, 0x00, 0x20, 0x41, 0x08, 0x00, 0x01], 37)
    and hid.writes[10] == pad([0x06, 0x8A, 0x42, 0x00, 0x20, 0x60, 0x01], 37),
    "Arctis 5 color / zone-select packets",
)
hid = FakeHid()
drv = ss.ArctisNova3Headset(hid, 0x12EC, "SteelSeries Arctis Nova 3", "/dev/hidraw3")
drv.set_colors([RED, BLUE])
w = hid.writes[0]
check(
    len(hid.writes) == 6
    and len(w) == 521
    and w[:3] == bytes([0x06, 0xAA, 0x01])
    and w[0x0D] == 0x98
    and w[0x0E] == 0x3A
    and w[0x15] == 0x55
    and w[0x17] == 0x96
    and w[0x1D] == 1
    and w[0x25] == 2
    and w[0x2D:0x31] == bytes([255, 0, 0, 0xFF]),
    "Nova 3 direct packet (right)",
)
check(
    hid.writes[1] == pad([0x06, 0xA5, 0x02, 0x0A], 64)
    and hid.writes[2] == pad([0x06, 0x09], 64)
    and hid.writes[4] == pad([0x06, 0xA5, 0x01, 0x0A], 64)
    and hid.writes[5] == pad([0x06, 0xA3], 64),
    "Nova 3 select/apply per zone",
)
hid.clear()
rb = drv.device.modes[3]
drv.set_mode(rb, save=True)
w = hid.writes[0]
check(
    w[0x0D] == 0x1C
    and w[0x0E] == 0x34
    and w[0x25] == 0x07
    and w[0x26:0x29] == bytes([0xFF, 0, 0])
    and w[0x44] == 0x26
    and hid.writes[6:] == [pad([0x06, 0x09], 64), pad([0x06, 0xA3], 64)],
    "Nova 3 rainbow speed 6 + save applies both zones",
)

# --- probe() never raises -----------------------------------------------------------------------------------
orig_enum, orig_hid = ss.enumerate_hid, ss.HidDevice
try:
    desc_vendor = bytes([0x06, 0xC0, 0xFF, 0x09, 0x01])
    ss.enumerate_hid = lambda vid=None, pid=None: [
        HidInfo("/dev/hidrawA", 0x1038, 0x184C, "Rival 3", 3, b""),  # matches
        HidInfo("/dev/hidrawB", 0x1038, 0x184C, "Rival 3", 0, b""),  # wrong interface
        HidInfo("/dev/hidrawC", 0x1038, 0x1838, "Aerox", 3, b"\x05\x01"),  # right interface, wrong usage page
        HidInfo("/dev/hidrawD", 0x1038, 0x1838, "Aerox", 3, desc_vendor),  # matches
        HidInfo("/dev/hidrawE", 0x1038, 0x9999, "Unknown", 0, b""),  # unknown PID
        HidInfo("/dev/hidrawF", 0x1038, 0x1612, "Apex 7", 1, b""),  # constructor will blow up
    ]
    opened = []

    class BoomHid(FakeHid):
        def __init__(self, path):
            super().__init__()
            self.path = path
            opened.append(self)
            if path == "/dev/hidrawF":
                raise ss.DeviceError("can't open")

    ss.HidDevice = BoomHid
    drivers = ss.probe()
    check(
        [d.path for d in drivers] == ["/dev/hidrawA", "/dev/hidrawD"],
        "probe() picks the OpenRGB interface / usage page and skips failures",
    )
    check(
        isinstance(drivers[0], ss.Rival3Mouse) and isinstance(drivers[1], ss.AeroxWirelessMouse),
        "probe() builds the right classes",
    )
finally:
    ss.enumerate_hid, ss.HidDevice = orig_enum, orig_hid

print()
if fails:
    print(f"{len(fails)} FAILED:")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("all SteelSeries checks passed")
