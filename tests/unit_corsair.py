"""Hardware-free checks for the Corsair drivers: a fake HidDevice records every write, tests assert exact bytes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.models import NO_LED, rgb_to_u32  # noqa: E402
from rgbdeck.hw import corsair_peripheral as cp  # noqa: E402
from rgbdeck.hw import corsair_lighting_node as cn  # noqa: E402
from rgbdeck.hw.hidraw import HidInfo  # noqa: E402

fails = []
RED, GREEN, BLUE, WHITE = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255), rgb_to_u32(255, 255, 255)


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def pkt(*head, size=65):
    b = bytearray(size)
    b[:len(head)] = bytes(head)
    return bytes(b)


class FakeHid:
    def __init__(self, replies=None):
        self.writes, self.features, self.replies, self.closed = [], [], list(replies or []), False

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size=64, timeout=0.5):
        return self.replies.pop(0) if self.replies else None

    def drain(self):
        pass

    def send_feature(self, data):
        self.features.append(bytes(data))

    def get_feature(self, report_id, size):
        return bytes(size)

    def close(self):
        self.closed = True


def v1_fw_reply(dev_type, pid_hi, pid_lo, layout=0, fw=(3, 8)):
    r = bytearray(64)
    r[0x14], r[0x0E], r[0x0F], r[0x17], r[0x09], r[0x08] = dev_type, pid_hi, pid_lo, layout, fw[0], fw[1]
    return bytes(r)


# --- table sanity -------------------------------------------------------------------------------
check(len(cp.V1_IDS_NORMAL) == 113 and len(cp.V1_NAMES_NORMAL) == 113, "V1 normal layout: 113 ids / 113 names")
check(len(cp.V1_IDS_K70_MK2) == 116 and len(cp.V1_NAMES_K70_MK2) == 116, "V1 K70 MK.2 layout: 116 ids / names")
check(len(cp.V1_IDS_K95_PLAT) == 139 and len(cp.V1_NAMES_K95_PLAT) == 139, "V1 K95 Platinum: 139 ids / names (120 + 19)")
check(len(cp.V1_IDS_K95) == 135 and len(cp.V1_NAMES_K95) == 135, "V1 K95: 135 ids / names")
check(all(len(r) == 23 for r in cp.V1_MATRIX_NORMAL) and len(cp.V1_MATRIX_NORMAL) == 6, "V1 normal matrix 6x23")
check(all(len(r) == 23 for r in cp.V1_MATRIX_K70_MK2) and len(cp.V1_MATRIX_K70_MK2) == 7, "V1 K70 MK.2 matrix 7x23")
check(all(len(r) == 24 for r in cp.V1_MATRIX_K95_PLAT) and len(cp.V1_MATRIX_K95_PLAT) == 7, "V1 K95 Platinum matrix 7x24")
check(all(len(r) == 26 for r in cp.V1_MATRIX_K95) and len(cp.V1_MATRIX_K95) == 7, "V1 K95 matrix 7x26")
check(len(cp.XT_IDS) == 111 and len(cp.XT_NAMES) == 111 and all(len(r) == 24 for r in cp.XT_MATRIX), "K55 PRO XT tables 111 / 6x24")
check(len(cp.V2_TKL_VALUES) == 89 and len(cp.V2_FULL_VALUES) == 106, "V2 key value lists match base map sizes")
check(len(cp.USB_IDS) == len(set(cp.USB_IDS)) and (0x1B1C, 0x1B49) in cp.USB_IDS and (0x1B1C, 0x1BC4) in cp.USB_IDS,
      f"USB_IDS lists every PID once ({len(cp.USB_IDS)} entries)")

# --- V1 keyboard: K70 RGB MK.2 (software mode packets, per-key frame, hardware mode) ----------------
hid = FakeHid([v1_fw_reply(0xC0, 0x1B, 0x49)])
kb = cp.CorsairLegacy("/dev/hidrawX", 0x1B49, "Corsair K70 RGB MK.2", hid=hid)
w = hid.writes
check(w[0] == pkt(0, 0x0E, 0x01), "V1: firmware info query 0x0E 0x01")
check(w[1] == pkt(0, 0x07, 0x04, 0x02), "V1: special function -> software")
check(w[2] == pkt(0, 0x07, 0x05, 0x02, 0, 0x03), "V1: lighting control software, keyboard byte 3")
check(w[3] == pkt(0, 0x07, 0x05, 0x08, 0, 0x01), "V1: K55/K95/K70 MK.2 extra lighting control 0x08")
exp = bytearray(pkt(0, 0x07, 0x40, 0x1E))
for j in range(30):
    exp[5 + 2 * j], exp[6 + 2 * j] = j, 0xC0
check(w[4] == bytes(exp), "V1: first key-mapping packet ids 0..29 with 0xC0")
exp = bytearray(pkt(0, 0x07, 0x40, 0x1E))
ids = [i for i in range(30, 61) if i != 0x31]
for j in range(30):
    exp[5 + 2 * j], exp[6 + 2 * j] = ids[j], 0xC0
check(w[5] == bytes(exp), "V1: second key-mapping packet skips id 0x31 (ANSI skip list)")
check(len(w) == 8, f"V1: init sends 8 packets ({len(w)})")
check(kb.device.type_name == "Keyboard" and kb.device.version == "3.8", "V1: type keyboard, firmware 3.8")
check(len(kb.device.led_names) == 116 and kb.device.led_names[13] == "Key: A" and kb.device.led_names[0] == "Key: Escape",
      "V1: K70 MK.2 has 116 LEDs with OpenRGB names")
check(len(kb.device.modes) == 10 and [m.name for m in kb.device.modes[:3]] == ["Direct", "ColorPulse", "ColorShift"],
      "V1: K70 MK.2 exposes Direct + 9 hardware modes")
check(kb.device.zones[0].matrix[0][3] == 115 and len(kb.device.zones[0].matrix) == 7, "V1: K70 MK.2 matrix map attached")
hid.writes.clear()
colors = [0] * 116
colors[0], colors[13], colors[9] = RED, GREEN, BLUE          # Escape (id 0), A (id 0x0F), Number Pad 7 (id 9)
kb.set_colors(colors)
w = hid.writes
check(len(w) == 12, f"V1: per-key frame is 12 packets ({len(w)})")
exp = bytearray(pkt(0, 0x7F, 1, 60))
exp[5 + 0] = 255
check(w[0] == bytes(exp), "V1: red plane stream packet 1 carries Escape at id 0")
check(w[1] == pkt(0, 0x7F, 2, 60) and w[2] == pkt(0, 0x7F, 3, 24), "V1: red plane packets 2/3 empty, 24-byte tail")
check(w[3] == pkt(0, 0x07, 0x28, 1, 3, 1), "V1: submit red channel")
exp = bytearray(pkt(0, 0x7F, 1, 60))
exp[5 + 15] = 255
check(w[4] == bytes(exp), "V1: green plane has A at id 0x0F")
exp = bytearray(pkt(0, 0x7F, 1, 60))
exp[5 + 9] = 255
check(w[8] == bytes(exp) and w[11] == pkt(0, 0x07, 0x28, 3, 3, 2), "V1: blue plane has Number Pad 7 at id 9, final submit finish=2")
hid.writes.clear()
rw = kb.device.mode_named("RainbowWave")
rw.speed, rw.brightness, rw.direction = 2, 3, 0
kb.set_mode(rw)
w = hid.writes
check(w[0] == pkt(0, 0x07, 0x05, 0x02, 0, 3), "V1 HW mode: brightness packet")
check(w[1][:5] == bytes([0, 0x07, 0x17, 0x05, 0]) and w[1][5:14] == b"lght_00.d" and sum(w[1][14:]) == 0,
      "V1 HW mode: lght_00.d packet")
exp = bytearray(pkt(0, 0x7F, 0x01, 0x0D))
exp[5], exp[6], exp[8] = 0x03, 2, 1
check(w[2] == bytes(exp), "V1 HW mode: rainbow wave stream (speed byte 6, direction left -> 1)")
check(w[3] == pkt(0, 0x07, 0x17, 0x09) and w[4] == pkt(0, 0x07, 0x17, 0x08), "V1 HW mode: commit pair")
check(w[5] == pkt(0, 0x07, 0x04, 0x01), "V1 HW mode: back to hardware special function")
cs = kb.device.mode_named("ColorShift")
cs.color_mode, cs.colors, cs.speed, cs.brightness = 2, [RED, BLUE], 1, 2
hid.writes.clear()
kb.set_mode(cs)
exp = bytearray(pkt(0, 0x7F, 0x01, 0x0D))
exp[5], exp[6], exp[7] = 0x00, 1, 0x03
exp[10:18] = bytes([255, 0, 0, 0xFF, 0, 0, 255, 0xFF])
check(hid.writes[2] == bytes(exp), "V1 HW mode: mode-specific colors RGB+FF pairs")
hid.writes.clear()
kb.set_mode(kb.device.modes[0])
check(hid.writes == [pkt(0, 7, 4, 2), pkt(0, 7, 5, 2, 0, 3)] + hid.writes[2:] and len(hid.writes) == 7,
      "V1: switching back to Direct replays the software-mode sequence")
kb.close()
check(hid.writes[-1] == pkt(0, 0x07, 0x04, 0x01) and hid.closed, "V1: close hands control back to hardware")

# --- V1 keyboard: K55 RGB zones, K95 Platinum two zones -----------------------------------------
hid = FakeHid([v1_fw_reply(0xC0, 0x1B, 0x3D)])
k55 = cp.CorsairLegacy("/dev/hidrawX", 0x1B3D, "Corsair K55 RGB", hid=hid)
check(len(k55.device.zones) == 3 and k55.device.led_names == ["LEFT", "MIDDLE", "RIGHT"], "V1 K55: three single-LED zones")
hid.writes.clear()
k55.set_colors([RED, GREEN, BLUE])
check(hid.writes == [pkt(0, 0x07, 0x25, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255)], "V1 K55: 0x25 zone colour packet")
hid = FakeHid([v1_fw_reply(0xC0, 0x1B, 0x2D, layout=1)])
k95 = cp.CorsairLegacy("/dev/hidrawX", 0x1B2D, "Corsair K95 RGB PLATINUM", hid=hid)
check([(z.name, z.leds_count, z.start_idx) for z in k95.device.zones] == [("Keyboard", 120, 0), ("Light Bar", 19, 120)],
      "V1 K95 Platinum: Keyboard(120) + Light Bar(19)")
check(k95.device.led_names[120] == "Light Bar 1" and k95.device.led_names[113] == "Key: Macro G1", "V1 K95 Platinum LED names")
ids = [i for i in range(0, 61) if i not in cp.V1_SKIP_K95_PLAT_ISO][:60]
check([hid.writes[4][5 + 2 * j] for j in range(30)] == ids[:30] and [hid.writes[5][5 + 2 * j] for j in range(30)] == ids[30:60],
      "V1 K95 Platinum ISO: key-mapping packets use the ISO skip list")
hid.writes.clear()
k95.set_colors([RED] * 139)
check(hid.writes[2][3] == 48 and hid.writes[2][5 + 24] == 255, "V1 K95 Platinum: 48-byte third stream packet (light bar id 144)")

# --- V1 mouse / mousemat / headset stand ---------------------------------------------------------
hid = FakeHid([v1_fw_reply(0xC1, 0x1B, 0x3C)])
mouse = cp.CorsairLegacy("/dev/hidrawX", 0x1B3C, "Corsair Harpoon RGB", hid=hid)
check(hid.writes == [pkt(0, 0x0E, 0x01), pkt(0, 0x07, 0x04, 0x02), pkt(0, 0x07, 0x05, 0x02, 0, 0x01)],
      "V1 mouse: firmware query, special function, lighting control byte 1")
check(mouse.device.type_name == "Mouse" and len(mouse.device.led_names) == 15 and mouse.device.led_names[0] == "Mouse LED 1",
      "V1 mouse: 15 generic LEDs")
hid.writes.clear()
mouse.set_colors([RED] * 15)
exp = bytearray(pkt(0, 0x07, 0x22, 15, 0))
for z in range(15):
    exp[5 + 4 * z:9 + 4 * z] = bytes([z, 255, 0, 0])
check(hid.writes == [bytes(exp)], "V1 mouse: static red 0x22 packet with <zone r g b> entries")
hid = FakeHid([v1_fw_reply(0xC1, 0x1B, 0x5A)])
m65 = cp.CorsairLegacy("/dev/hidrawX", 0x1B5A, "Corsair M65 RGB Elite", hid=hid)
check(m65.device.led_names[2:5] == ["Logo", "DPI", "Scroll Wheel"], "V1 M65 Elite: named LEDs")

r = bytearray(v1_fw_reply(0xC2, 0, 0))
r[0x0F], r[0x0E] = 0x0A, 0x34
hid = FakeHid([bytes(r)])
st = cp.CorsairLegacy("/dev/hidrawX", 0x0A34, "Corsair ST100 RGB", hid=hid)
check(st.device.type_name == "Headset Stand" and [z.leds_count for z in st.device.zones] == [8, 1], "V1 ST100: strip + logo zones")
check(hid.writes[2] == pkt(0, 0x07, 0x05, 0x02, 0, 0x03), "V1 ST100: lighting control byte 3")
hid.writes.clear()
cols = [rgb_to_u32(i, 0, 0) for i in range(1, 10)]
st.set_colors(cols)
exp = bytearray(pkt(0, 0x07, 0x22, 9, 0))
order = [1, 2, 3, 4, 9, 5, 6, 7, 8]          # logo (LED 9) is wired in the middle of the strip
for i, red in enumerate(order):
    exp[5 + 3 * i:8 + 3 * i] = bytes([red, 0, 0])
check(hid.writes == [bytes(exp)], "V1 ST100: logo remapped into the middle of the mousemat-style packet")
hid = FakeHid([v1_fw_reply(0xC2, 0x3B, 0x1B)])
mm = cp.CorsairLegacy("/dev/hidrawX", 0x1B3B, "Corsair MM800 RGB Polaris", hid=hid)
check(mm.device.type_name == "Mousemat" and hid.writes[2][5] == 0x04, "V1 MM800: mousemat, lighting control byte 4")

hid = FakeHid([bytes(64)] * 10)
try:
    cp.CorsairLegacy("/dev/hidrawX", 0x1B13, "Corsair K70 RGB", hid=hid)
    check(False, "V1: unknown device type raises")
except cp.DeviceError:
    check(hid.closed and len(hid.writes) == 10, "V1: 10 firmware-info attempts then DeviceError and close")

# --- K55 RGB PRO XT -------------------------------------------------------------------------------
hid = FakeHid()
xt = cp.CorsairK55ProXT("/dev/hidrawX", 0x1BA1, "Corsair K55 RGB PRO XT", hid=hid)
check(hid.writes == [pkt(0, 8, 1, 3, 0, 2), pkt(0, 8, 2, 0x5F), pkt(0, 8, 0x0D, 0, 1)], "PRO XT: software-mode init packets")
check(len(xt.device.led_names) == 111 and xt.device.led_names[6] == "Key: Escape" and xt.device.led_names[0] == "Key: G1",
      "PRO XT: 111 LEDs, G keys first")
check([m.name for m in xt.device.modes][:3] == ["Direct", "Static", "ColorPulse"] and len(xt.device.modes) == 12,
      "PRO XT: Direct + 11 hardware modes")
hid.writes.clear()
colors = [0] * 111
colors[0], colors[6] = RED, GREEN                    # G1 (id 127), Escape (id 37)
xt.set_colors(colors)
w = hid.writes
check(len(w) == 7 and w[0][:12] == bytes([0, 8, 6, 0, 0x9B, 1, 0, 0, 0, 0, 0, 0]), "PRO XT: 1 header + 6 continuation packets")
check(all(p[:4] == bytes([0, 8, 7, 0]) for p in w[1:]), "PRO XT: continuation packets are 0x08 0x07")
check(w[2][4 + 13] == 255 and w[2][4 + 60] == 255 and sum(w[0][12:]) == 0 and sum(w[1][4:]) == 0 and sum(w[2][4:]) == 510,
      "PRO XT: red of id 127 and green of id 37 land in packet 3")
hid.writes.clear()
static = xt.device.mode_named("Static")
static.colors = [rgb_to_u32(10, 20, 30)]
xt.set_mode(static)
w = hid.writes
check(w[:5] == [pkt(0, 8, 1, 3, 0, 2), pkt(0, 8, 2, 0x5F), pkt(0, 8, 0x0D, 0, 1), pkt(0, 8, 0x0D, 1, 0x61, 0x6D), pkt(0, 8, 9, 1)],
      "PRO XT HW mode: init + 0x0D 0x01 0x61 0x6D + 0x09 0x01")
m = w[5]
check(m[:5] == bytes([0, 8, 6, 1, 0x7C]) and m[8:13] == bytes([0x7E, 0x20, 1, 0, 0]) and m[0x0E] == 1
      and m[0x0F:0x13] == bytes([0xFF, 30, 20, 10]) and m[0x13:] == cp.XT_FILLER[:65 - 0x13],
      "PRO XT HW mode: static mode packet (id 0x207E, predefined colour BGR, filler)")
check(w[6][:4] == bytes([0, 8, 7, 1]) and w[6][4:] == cp.XT_FILLER[46:46 + 61], "PRO XT HW mode: filler packet 2")
check(w[7][:4] == bytes([0, 8, 7, 1]) and w[7][4:4 + 6] == cp.XT_FILLER[107:] and w[8] == pkt(0, 8, 5, 1, 1)
      and w[9] == pkt(0, 0, 1, 3, 0, 1), "PRO XT HW mode: filler tail, 0x05 0x01 0x01 commit, hardware switch")
xt.last_update -= 60
hid.writes.clear()
xt.set_mode(xt.device.modes[0])
hid.writes.clear()
xt.keepalive()
check(len(hid.writes) == 7, "PRO XT: keepalive re-sends the frame after 50 s in Direct")

# --- K65 Mini -------------------------------------------------------------------------------------
hid = FakeHid()
mini = cp.CorsairK65Mini("/dev/hidrawX", 0x1BAF, "Corsair K65 Mini", hid=hid)
check(hid.writes == [pkt(0, 8, 1, 3, 0, 2, size=1025), pkt(0, 8, 2, 0x6E, size=1025), pkt(0, 8, 0x0D, 1, 0x22, size=1025)],
      "K65 Mini: three 1024-byte init packets")
check(len(mini.device.led_names) == 64 and mini.device.led_names[:2] == ["Key: Escape", "Key: Tab"]
      and mini.device.zones[0].matrix[0][1] == 5, "K65 Mini: 64 LEDs walked column-major, matrix 5x15")
hid.writes.clear()
mini.set_colors([RED, GREEN] + [0] * 62)
exp = bytearray(pkt(0, 8, 6, 1, 0x73, 1, 0, 0, 0x12, size=1025))
exp[0x0A + 41 * 3] = 255
exp[0x0A + 43 * 3 + 1] = 255
check(hid.writes == [bytes(exp)], "K65 Mini: direct frame, Escape id 41 red, Tab id 43 green")

# --- KeyboardLayoutManager port --------------------------------------------------------------------
kl = cp.KeyboardLayout(cp.V2_KB_ANSI, cp.SIZE_FULL, cp.V2_FULL_VALUES)
check(len(kl.keys) == 104 and kl.rows == 6 and kl.cols == 21, f"KLM: ANSI full = 104 keys 6x21 ({len(kl.keys)})")
check(len(cp.KeyboardLayout(cp.V2_KB_ISO, cp.SIZE_FULL, cp.V2_FULL_VALUES).keys) == 105, "KLM: ISO full = 105 keys")
check(len(cp.KeyboardLayout(cp.V2_KB_ANSI, cp.SIZE_TKL, cp.V2_TKL_VALUES).keys) == 87, "KLM: ANSI TKL = 87 keys")
named = {k[3]: k for k in kl.keys}
check(named["Key: Escape"][2] == 41 and named["Key: Space"][2] == 44 and named["Key: Number Pad ."][2] == 99,
      "KLM: Corsair key values follow the base map order")
check("Key: #" not in named and "Key: \\ (ISO)" not in named and "Key: \\ (ANSI)" in named, "KLM: ANSI overlay drops # and ISO \\")

# --- V2 hardware-flavour keyboard: K70 RGB PRO -----------------------------------------------------
def v2_reply(*vals):
    r = bytearray(64)
    r[3:3 + len(vals)] = bytes(vals)
    return bytes(r)


hid = FakeHid([v2_reply(0xC4, 0x1B), bytes(64), bytes(64), bytes(64), v2_reply(1), bytes(64), bytes(64)])
v2 = cp.CorsairV2("/dev/hidrawX", 0x1BC4, "Corsair K70 RGB PRO", hid=hid, flavour="hw")
w = hid.writes
check(w[0] == pkt(0, 8, 2, 0x12) and w[1] == pkt(0, 8, 2, 0x11), "V2: PID then VID queries")
check(w[2] == pkt(0, 8, 0x0D, 0, 0x22) and w[3] == pkt(0, 8, 5, 1, 0), "V2: lighting endpoint probe start/stop on 0x22")
check(w[4] == pkt(0, 8, 2, 0x41), "V2: keyboard layout query 0x41")
check(w[5] == pkt(0, 8, 1, 3, 0, 2) and w[6] == pkt(0, 8, 2, 0x5F, 0, 0), "V2: software render mode + lighting control 0x5F")
check(v2.light_ctrl == 0x22 and v2.device.type_name == "Keyboard", "V2: endpoint 0x22 accepted")
z = v2.device.zones[0]
check(z.leds_count == 115 and len(z.matrix) == 7 and len(z.matrix[0]) == 21, f"V2 K70 PRO: 115 LEDs, 7x21 matrix ({z.leds_count})")
check(v2.device.led_names[:3] == ["Profile", "Light", "Lock"] and z.matrix[0][0] == 0 and "Key: Media Mute" in v2.device.led_names,
      "V2 K70 PRO: overlay keys inserted in a new top row")
check(v2.values[v2.device.led_names.index("Key: Escape")] == 41 and v2.buffer_len == 142, "V2 K70 PRO: buffer spans values 0..141")
hid.writes.clear()
hid.replies = [bytes(64), bytes(64), bytes(64)]
colors = [0] * 115
colors[v2.device.led_names.index("Key: Escape")] = RED
v2.set_colors(colors)
w = hid.writes
check(w[0] == pkt(0, 8, 0x0D, 0, 0x22) and w[-1] == pkt(0, 8, 5, 1, 0), "V2: start / stop transaction around the block")
check(w[1][:10] == bytes([0, 8, 6, 0, 0xAC, 0x01, 0, 0, 0x12, 0x00]), "V2 HW: block header, 428 bytes, 0x0012 direct marker")
check(len(w) == 10 and all(p[:4] == bytes([0, 8, 7, 0]) for p in w[2:9]), "V2: 7 continuation packets")
check(w[3][4 + 7] == 255 and sum(w[1][10:]) == 0 and sum(w[2][4:]) == 0 and sum(w[3][4:]) == 255,
      "V2 HW: Escape (value 41) red at data offset 125 = 2nd continuation packet, byte 7")
hid.writes.clear()
v2.close()
check(hid.writes == [pkt(0, 8, 1, 3, 0, 1)] and hid.closed, "V2: close restores hardware render mode")

# --- V2 software flavour: K55 RGB PRO (colour blocks) + endpoint fallback + wireless receiver ----------
hid = FakeHid([v2_reply(0xA4, 0x1B), bytes(64), bytes([0, 0, 5]), bytes(64), bytes(64), bytes(64), bytes(64)])
sw = cp.CorsairV2("/dev/hidrawX", 0x1BA4, "Corsair K55 RGB PRO", hid=hid, flavour="sw")
check(sw.light_ctrl == 1 and hid.writes[2] == pkt(0, 8, 0x0D, 0, 0x22) and hid.writes[3] == pkt(0, 8, 0x0D, 0, 1),
      "V2: endpoint 0x22 rejected -> retry start on endpoint 1")
check(sw.device.led_names == [f"Keyboard {i}" for i in range(6)] and sw.device.zones[0].type == 1, "V2 K55 PRO: 6 linear LEDs")
hid.writes.clear()
sw.set_colors([RED, GREEN, BLUE, 0, 0, 0])
exp = bytearray(pkt(0, 8, 6, 0, 18, 0))
exp[8:26] = bytes([255, 0, 0, 0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 255, 0, 0, 0])
check(hid.writes == [pkt(0, 8, 0x0D, 0, 1), bytes(exp), pkt(0, 8, 5, 1, 0)], "V2 SW: R-plane, G-plane, B-plane block of 18 bytes")

hid = FakeHid([v2_reply(0xA6, 0x1B), v2_reply(0x93, 0x1B), bytes(64), bytes(64), bytes(64), bytes(64), bytes(64)])
rx = cp.CorsairV2("/dev/hidrawX", 0x1BA6, "Corsair Slipstream Wireless Receiver HW", hid=hid, flavour="hw")
check(hid.writes[1] == pkt(0, 9, 2, 0x12) and rx.write_cmd == 9 and rx.device.name == "Corsair Katar Pro",
      "V2: receiver switches to write id 0x09 and adopts the paired mouse")
check([z.name for z in rx.device.zones] == ["Scroll Wheel", "DPI"], "V2 Katar Pro zones")

hid = FakeHid([v2_reply(0x99, 0x99)] + [bytes(64)] * 20)
try:
    cp.CorsairV2("/dev/hidrawX", 0x1BC4, "x", hid=hid, flavour="hw")
    check(False, "V2: unknown reported PID raises")
except cp.DeviceError:
    check(hid.closed, "V2: unknown reported PID -> DeviceError, hid closed")

hid = FakeHid([v2_reply(0x7C, 0x1B), bytes(64), v2_reply(2), bytes(64), bytes(64)])
k100 = cp.CorsairV2("/dev/hidrawX", 0x1B7C, "Corsair K100 RGB Optical", hid=hid, flavour="hw")
check(hid.writes[2] == pkt(0, 8, 2, 0x41) and k100.light_ctrl == 0x22, "V2 K100: endpoint pinned to 0x22, no probe")
check(k100.device.zones[0].leds_count == 105 + 4 + 7 + 6 + 44 + 8 and len(k100.device.zones[0].matrix) == 12,
      f"V2 K100 ISO: {k100.device.zones[0].leds_count} LEDs in a 12x24 matrix")

# --- probe() never raises and never opens anything here ------------------------------------------------
cp.enumerate_hid = lambda vid=None, pid=None: []
check(cp.probe() == [], "corsair_peripheral.probe(): empty without devices")
cp.enumerate_hid = lambda vid=None, pid=None: [HidInfo("/dev/hidraw99", 0x1B1C, 0x1B49, "x", 1, b"\x06\xc2\xff")]
cp.HidDevice = lambda path: (_ for _ in ()).throw(cp.DeviceError("no such device"))
check(cp.probe() == [], "corsair_peripheral.probe(): swallows DeviceError from a failing open")
cn.enumerate_hid = lambda vid=None, pid=None: []
check(cn.probe() == [], "corsair_lighting_node.probe(): empty without devices")

# --- Lighting Node --------------------------------------------------------------------------------
hid = FakeHid([bytes([0x02, 0, 9, 212])])
node = cn.CorsairLightingNode("/dev/hidrawX", 0x0C0B, "Corsair Lighting Node Pro", hid=hid, led_counts=[0, 0])
check(hid.writes == [pkt(0, 0x02)] and node.device.version == "0.9.212", "Node: firmware request / version string")
check([(z.leds_min, z.leds_max, z.leds_count) for z in node.device.zones] == [(0, 204, 0), (0, 204, 0)], "Node: two resizable zones at 0 LEDs")
check([m.name for m in node.device.modes] == ["Direct", "Rainbow Wave", "Color Shift", "Color Pulse", "Color Wave", "Static",
                                              "Temperature", "Visor", "Marquee", "Blink", "Sequential", "Rainbow"], "Node: mode list")
rwm = node.device.mode_named("Rainbow Wave")
check((rwm.speed_min, rwm.speed_max, rwm.speed, rwm.brightness_max, rwm.direction) == (2, 0, 1, 100, 1), "Node: inverted speed range, right")
node.resize_zone(0, 4)
check(node.device.zones[0].leds_count == 4 and node.device.zones[1].start_idx == 4 and len(node.device.colors) == 4
      and node.device.led_names[3] == "Corsair RGB Header 1, LED 4", "Node: resize_zone rebuilds LEDs")
hid.writes.clear()
node.set_colors([RED, GREEN, BLUE, WHITE])
w = hid.writes
check(w[0] == pkt(0, 0x38, 0, 2), "Node direct: channel 0 software port state")
check(w[1] == pkt(0, 0x32, 0, 0, 4, 0, 255, 0, 0, 255), "Node direct: red plane for 4 LEDs")
check(w[2] == pkt(0, 0x32, 0, 0, 4, 1, 0, 255, 0, 255), "Node direct: green plane")
check(w[3] == pkt(0, 0x32, 0, 0, 4, 2, 0, 0, 255, 255), "Node direct: blue plane")
check(w[4] == pkt(0, 0x33, 0xFF) and w[5] == pkt(0, 0x38, 1, 2) and w[6] == pkt(0, 0x33, 0xFF) and len(w) == 7,
      "Node direct: commit, then empty channel 1 (port state + commit)")
node.resize_zone(0, 120)
hid.writes.clear()
node.set_colors([RED] * 120)
check(len(hid.writes) == 1 + 3 * 3 + 1 + 2 and hid.writes[7][:6] == bytes([0, 0x32, 0, 100, 20, 0]),
      "Node direct: 120 LEDs go out as 50 + 50 + 20 blocks")
node.resize_zone(0, 4)
hid.writes.clear()
cw = node.device.mode_named("Color Wave")
cw.speed, cw.direction, cw.colors, cw.brightness = 1, 1, [RED, GREEN], 60
node.set_mode(cw)
w = hid.writes
check(w[0] == pkt(0, 0x37, 0) and w[1] == pkt(0, 0x34, 0) and w[2] == pkt(0, 0x38, 0, 1), "Node effect: reset, begin, hardware port state")
check(w[3] == pkt(0, 0x35, 0, 0, 4, 3, 1, 1, 0, 0, 255, 0, 0, 0, 255, 0), "Node effect: Color Wave config, 4 LEDs, medium, right, 2 colours")
check(w[4] == pkt(0, 0x33, 0xFF) and w[8] == pkt(0, 0x35, 1, 0, 0, 3, 1, 1, 0, 0, 255, 0, 0, 0, 255, 0), "Node effect: commit, channel 1 config")
check(w[10] == pkt(0, 0x39, 0, 60) and w[11] == pkt(0, 0x39, 1, 60) and len(w) == 12, "Node effect: brightness on both channels")
rb = node.device.mode_named("Rainbow")
rb.color_mode = 3
hid.writes.clear()
node.set_mode(rb)
check(hid.writes[3] == pkt(0, 0x35, 0, 0, 4, 0x0A, 1, 0, 1), "Node effect: random flag in change-style byte")
hid.writes.clear()
node.set_mode(node.device.modes[0])
check(hid.writes[0] == pkt(0, 0x38, 0, 2) and hid.writes[-1] == pkt(0, 0x39, 1, 100), "Node: Direct re-sends colours, brightness 100")
hid.writes.clear()
node.keepalive()
check(hid.writes == [], "Node keepalive: nothing within 5 s of a commit")
node.last_commit -= 6
node.keepalive()
check(hid.writes == [pkt(0, 0x33, 0xFF)], "Node keepalive: commit after 5 s idle")

os.environ[cn.ENV_LEDS] = "12,abc"
hid = FakeHid([bytes([0x02, 0, 1, 1])])
node2 = cn.CorsairLightingNode("/dev/hidrawX", 0x0C1A, "Corsair Lighting Node Core", hid=hid)
check([z.leds_count for z in node2.device.zones] == [12, 0], "Node: RGBDECK_CORSAIR_NODE_LEDS presets channel sizes")

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("all passed")
