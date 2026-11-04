# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""Hardware-free checks for rgbdeck.hw.logitech_mouse: exact HID++ bytes against a scripted fake hidraw device.

Covers: root GetFeature, the 0x8071 SetSWControl claim sequence (with one BUSY retry), a 0x8071 Direct/static color
packet, a 0x8070 static effect packet with the save bit, and receiver pairing enumeration with device index routing
plus an asleep slot answered by the receiver's 0x8F ERR_RESOURCE_ERROR frame.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.models import DeviceError, MODE_FLAG_MANUAL_SAVE, MODE_FLAG_AUTOMATIC_SAVE  # noqa: E402
from rgbdeck.hw import logitech_mouse as lm  # noqa: E402
from rgbdeck.hw.logitech_hidpp import _slider_to_period  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def hexs(b):
    return " ".join(f"{x:02X}" for x in b)


def long_frame(idx, feat, fn_sw, data=b""):
    f = bytes([0x11, idx, feat, fn_sw]) + bytes(data)
    return f + bytes(20 - len(f))


def short_frame(idx, sub, addr, data=b""):
    f = bytes([0x10, idx, sub, addr]) + bytes(data)
    return f + bytes(7 - len(f))


def error_frame(idx, feat, fn_sw, code):
    return long_frame(idx, 0xFF, feat, bytes([fn_sw, code]))


class FakeHid:
    """Stands in for rgbdeck.hw.hidraw.HidDevice: every write runs the script, replies queue up for read()."""

    def __init__(self, script, path="/dev/hidraw-fake"):
        self.script, self.path, self.writes, self.queue, self.closed = script, path, [], [], False

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        self.queue.extend(self.script(data))
        return len(data)

    def read(self, size=64, timeout=0.5):
        if self.queue:
            return self.queue.pop(0)
        time.sleep(min(timeout, 0.002))
        return None

    def drain(self):
        self.queue.clear()

    def close(self):
        self.closed = True


# ---- scripted devices -------------------------------------------------------------------------------------------
def make_mouse_8071(idx, name=b"G502 LIGHTSPEED Wireless Gaming Mouse", busy_once=True):
    """A two-cluster 0x8071 mouse (Primary + Logo), features at: 0x0001->1, 0x0005->3, 0x1004->8, 0x8071->0x0E, 0x8100->0x0F."""
    feats = {0x0001: 0x01, 0x0003: 0x02, 0x0005: 0x03, 0x1004: 0x08, 0x8071: 0x0E, 0x8100: 0x0F}
    clusters = [(0x0001, [0x0000, 0x0001, 0x0003, 0x000A]), (0x0002, [0x0000, 0x0001, 0x0003, 0x000A])]
    state = {"sw": 0}

    def script(pkt):
        if pkt[0] != 0x11 or pkt[1] != idx:
            return []
        feat, fn, d = pkt[2], pkt[3], pkt[4:]

        def reply(data):
            return [long_frame(idx, feat, fn, data)]

        if feat == 0x00 and fn == 0x07:  # IRoot GetFeature
            return reply(bytes([feats.get((d[0] << 8) | d[1], 0), 0, 0]))
        if feat == 0x03:  # 0x0005 DeviceNameType
            if fn == 0x07:
                return reply(bytes([len(name)]))
            if fn == 0x17:
                return reply(name[d[0] : d[0] + 16])
            if fn == 0x27:
                return reply(bytes([3]))  # mouse
        if feat == 0x0E:  # 0x8071 RGB effects
            if fn == 0x07:
                if d[0] == 0xFF and d[1] == 0xFF:
                    return reply(bytes([0, 0, len(clusters), 0, 0, 0, 0, 0]))
                if d[1] == 0xFF:
                    loc, effs = clusters[d[0]]
                    return reply(bytes([d[0], 0xFF, loc >> 8, loc & 0xFF, len(effs)]))
                eid = clusters[d[0]][1][d[1]]
                return reply(bytes([d[0], d[1], eid >> 8, eid & 0xFF, 0, 0, 0x13, 0x88]))
            if fn == 0x57:  # SetSWControl: BUSY on the very first call
                state["sw"] += 1
                if state["sw"] == 1 and busy_once:
                    return [error_frame(idx, feat, fn, 0x08)]
                return reply(d)
            return reply(d)  # SetEffect / power config / power mode echo
        if feat == 0x0F:  # 0x8100 onboard profiles
            return reply(b"")
        if feat == 0x08 and fn == 0x17:  # 0x1004 GetStatus: 55 %, charging on cable
            return reply(bytes([55, 0x02, 0x01, 0x01]))
        return []

    return script


def make_mouse_8070(idx=0xFF, name=b"G Pro Gaming Mouse"):
    """A single-cluster 0x8070 mouse (Logo), feature 0x8070 at index 0x0E, no profile features."""
    feats = {0x0001: 0x01, 0x0005: 0x03, 0x8070: 0x0E}
    effects = [0x0000, 0x0001, 0x0003, 0x000A]

    def script(pkt):
        if pkt[0] != 0x11 or pkt[1] != idx:
            return []
        feat, fn, d = pkt[2], pkt[3], pkt[4:]

        def reply(data):
            return [long_frame(idx, feat, fn, data)]

        if feat == 0x00 and fn == 0x07:
            return reply(bytes([feats.get((d[0] << 8) | d[1], 0), 0, 0]))
        if feat == 0x03:
            if fn == 0x07:
                return reply(bytes([len(name)]))
            if fn == 0x17:
                return reply(name[d[0] : d[0] + 16])
            if fn == 0x27:
                return reply(bytes([3]))
        if feat == 0x0E:
            if fn == 0x07:  # getInfo: zone count
                return reply(bytes([1]))
            if fn == 0x17:  # getZoneInfo
                return reply(bytes([d[0], 0x00, 0x02, len(effects)]))
            if fn == 0x27:  # getEffectInfo
                eid = effects[d[1]]
                return reply(bytes([d[0], d[1], eid >> 8, eid & 0xFF, 0, 0, 0x27, 0x10]))
            return reply(d)  # setEffect / manageSwControl echo
        return []

    return script


def make_receiver():
    """A Lightspeed receiver with two paired slots: 1 = awake G502 (0x407F), 2 = a G Pro (0x4079) that the link flag
    says is awake but that the receiver answers for with ERR_RESOURCE_ERROR (it fell asleep in between)."""
    mouse1 = make_mouse_8071(1)
    state = {"notif": None}

    def script(pkt):
        if pkt[0] == 0x10 and pkt[1] == 0xFF:
            sub, addr = pkt[2], pkt[3]
            if sub == 0x81 and addr == 0x00:  # read notifications register: off
                return [short_frame(0xFF, 0x81, 0x00, b"\x00\x00\x00")]
            if sub == 0x80 and addr == 0x00:  # enable notifications
                state["notif"] = pkt[5]
                return [short_frame(0xFF, 0x80, 0x00)]
            if sub == 0x81 and addr == 0x02:  # connection state: 2 devices
                return [short_frame(0xFF, 0x81, 0x02, b"\x00\x02\x00")]
            if sub == 0x80 and addr == 0x02 and pkt[4] == 0x02:  # fake reconnect -> 0x41 notifications
                return [
                    short_frame(0xFF, 0x80, 0x02),
                    short_frame(1, 0x41, 0x04, b"\x02\x7f\x40"),
                    short_frame(2, 0x41, 0x04, b"\x02\x79\x40"),
                ]
            if sub == 0x83 and addr == 0xB5:  # pairing info
                n = pkt[4]
                if n == 0x40:
                    return [long_frame(0xFF, 0x83, 0xB5, b"\x40\x04G502")]
                if n == 0x41:
                    return [long_frame(0xFF, 0x83, 0xB5, b"\x41\x05G PRO")]
                if n == 0x30:
                    return [long_frame(0xFF, 0x83, 0xB5, b"\x30\x12\x34\x56\x78")]
                if n == 0x31:
                    return [long_frame(0xFF, 0x83, 0xB5, b"\x31\x00\x00\x00\x00")]
            return []
        if pkt[0] == 0x11 and pkt[1] == 1:
            return mouse1(pkt)
        if pkt[0] == 0x11 and pkt[1] == 2:  # slot 2 not connected: HID++ 1.0 error 0x8F / 0x09
            return [bytes([0x10, 2, 0x8F, pkt[2], pkt[3], 0x09, 0x00])]
        return []

    return script, state


# ---- 1. wired 0x8071 mouse ------------------------------------------------------------------------------------------
hid = FakeHid(make_mouse_8071(0xFF))
link = lm.HidppLink(hid)
dev = lm.open_wired(link, 0xC08D)
d = dev.device
check(
    hid.writes[0] == bytes([0x11, 0xFF, 0x00, 0x07, 0x00, 0x01]) + bytes(14),
    "root GetFeature(0x0001): " + hexs(hid.writes[0]),
)
check(bytes([0x11, 0xFF, 0x00, 0x07, 0x80, 0x71]) + bytes(14) in hid.writes, "root GetFeature(0x8071) sent")
check(
    bytes([0x11, 0xFF, 0x00, 0x07, 0x80, 0x80]) + bytes(14) in hid.writes,
    "root GetFeature(0x8080) sent (keyboard guard)",
)
check(d.name == "G502 LIGHTSPEED Wireless Gaming Mouse", f"name from 0x0005: {d.name!r}")
check(d.type_name == "Mouse", f"type from 0x0005 GetDeviceType: {d.type_name}")
check(
    [z.name for z in d.zones] == ["Primary", "Logo"] and d.led_names == ["Primary", "Logo"],
    f"zones {[z.name for z in d.zones]}",
)
check(
    [m.name for m in d.modes] == ["Direct", "Off", "Static", "Spectrum Cycle", "Breathing"],
    f"modes {[m.name for m in d.modes]}",
)
check(all(m.flags & MODE_FLAG_AUTOMATIC_SAVE for m in d.modes[1:]), "0x8071 effect modes persist automatically")
check(d.modes[2].has_per_led and d.modes[4].has_per_led, "multi-cluster: Static/Breathing take per-zone colors")
check(not hid.writes or all(w[3] & 0x0F == 0x07 for w in hid.writes), "every request carries software id 7")

hid.writes.clear()
RED, BLUE = 0x0000FF, 0xFF0000
dev.set_colors([RED, BLUE])
w = hid.writes
claim = long_frame(0xFF, 0x0E, 0x57, bytes([0x01, 0x03, 0x06]))
check(len(w) >= 8, f"claim + 2 colors + upgrade = {len(w)} writes")
check(w[0] == claim and w[1] == claim, "SetSWControl(3,6) claim, resent once after BUSY: " + hexs(w[0]))
check(w[2] == long_frame(0xFF, 0x0E, 0x87, bytes([0x01, 0x01])), "SetRGBPowerMode(1): " + hexs(w[2]))
check(
    w[3] == long_frame(0xFF, 0x0E, 0x77, bytes([0x01, 0, 0, 0x00, 0x3C, 0x01, 0x2C])),
    "WritePowerConfig(60s, 300s): " + hexs(w[3]),
)
check(w[4] == long_frame(0xFF, 0x0F, 0x17, bytes([0x02])), "OnboardProfiles host mode: " + hexs(w[4]))
static_red = long_frame(0xFF, 0x0E, 0x17, bytes([0x00, 0x01, 0xFF, 0x00, 0x00, 0x02]))
static_blue = long_frame(0xFF, 0x0E, 0x17, bytes([0x01, 0x01, 0x00, 0x00, 0xFF, 0x02]))
check(w[5] == static_red, "0x8071 Direct cluster 0 = Static red, marker 0x02, persist 0: " + hexs(w[5]))
check(w[6] == static_blue, "0x8071 Direct cluster 1 = Static blue: " + hexs(w[6]))
check(
    w[7] == long_frame(0xFF, 0x0E, 0x57, bytes([0x01, 0x03, 0x05])),
    "SetSWControl(3,5) after first paint: " + hexs(w[7]),
)
hid.writes.clear()
dev.set_colors([RED, BLUE])
check(hid.writes == [], "unchanged colors are not re-sent")
dev.set_colors([RED, 0x00FF00])
check(
    len(hid.writes) == 1 and hid.writes[0][4] == 0x01 and hid.writes[0][6:9] == b"\x00\xff\x00",
    "only the changed cluster is re-sent",
)

hid.writes.clear()
check(dev.battery() == (55, True), f"battery via 0x1004 GetStatus: {dev.battery()}")
check(hid.writes[0] == long_frame(0xFF, 0x08, 0x17), "0x1004 GetStatus request: " + hexs(hid.writes[0]))

hid.writes.clear()
breathing = d.mode_named("Breathing")
breathing.speed, breathing.brightness = 70, 100
period = _slider_to_period(70, 1000, 20000)
dev.set_mode(breathing, save=False)
exp0 = long_frame(
    0xFF, 0x0E, 0x17, bytes([0x00, 0x03, 0xFF, 0x00, 0x00, period >> 8, period & 0xFF, 0x00, 100, 0, 0, 0, 0x01])
)
check(
    hid.writes[0] == exp0,
    "0x8071 Breathing cluster 0 (per-zone red, period, brightness, persist=1): " + hexs(hid.writes[0]),
)
check(
    hid.writes[1][4:6] == b"\x01\x03" and hid.writes[1][6:9] == b"\x00\xff\x00",
    "Breathing cluster 1 carries its own color",
)

hid.writes.clear()
dev.set_mode(d.mode_named("Off"))
check(
    hid.writes[0] == long_frame(0xFF, 0x0E, 0x17, bytes([0x00, 0x01])),
    "Off = Static black on cluster 0, persist 0: " + hexs(hid.writes[0]),
)
check(hid.writes[1] == long_frame(0xFF, 0x0E, 0x17, bytes([0x01, 0x01])), "Off = Static black on cluster 1")

hid.writes.clear()
dev.close()
check(
    hid.writes[0] == long_frame(0xFF, 0x0E, 0x57, bytes([0x01, 0x00, 0x00])),
    "release SetSWControl(0,0): " + hexs(hid.writes[0]),
)
check(hid.writes[1] == long_frame(0xFF, 0x0F, 0x17, bytes([0x01])), "back to onboard profiles on close")
check(hid.closed, "hidraw closed on close()")

# ---- 2. wired 0x8070 mouse ------------------------------------------------------------------------------------------
hid = FakeHid(make_mouse_8070())
dev = lm.open_wired(lm.HidppLink(hid), 0xC085)
d = dev.device
check(
    d.name == "G Pro Gaming Mouse" and [z.name for z in d.zones] == ["Logo"],
    f"0x8070 device: {d.name} zones {[z.name for z in d.zones]}",
)
check(
    all(m.flags & MODE_FLAG_MANUAL_SAVE for m in d.modes[1:]) and not d.modes[0].can_save,
    "0x8070 modes expose Save, Direct does not",
)
check(d.modes[2].has_mode_colors and not d.modes[2].has_per_led, "single cluster: Static takes one mode color")
hid.writes.clear()
static = d.mode_named("Static")
static.colors = [0x00FF00]
dev.set_mode(static, save=True)
check(
    hid.writes[0] == long_frame(0xFF, 0x0E, 0x87, bytes([0x01, 0x01])),
    "0x8070 claim manageSwControl(1,1): " + hexs(hid.writes[0]),
)
check(
    hid.writes[1] == long_frame(0xFF, 0x0E, 0x37, bytes([0x00, 0x01, 0x00, 0xFF, 0x00, 0x02, 0, 0, 0, 0, 0, 0, 0x01])),
    "0x8070 setClusterEffect Static green, save bit set: " + hexs(hid.writes[1]),
)
check(len(hid.writes) == 2, "0x8070 path has no power/host-mode tail")
hid.writes.clear()
dev.set_mode(static, save=False)
check(hid.writes[0][16] == 0x00, "0x8070 live write is ephemeral (persist 0)")
hid.writes.clear()
cycle = d.mode_named("Spectrum Cycle")
cycle.speed, cycle.brightness = 80, 50
p = _slider_to_period(80, 1000, 20000)
dev.set_mode(cycle)
check(
    hid.writes[0] == long_frame(0xFF, 0x0E, 0x37, bytes([0x00, 0x02, 0, 0, 0, 0, 0, p >> 8, p & 0xFF, 50])),
    "0x8070 Spectrum Cycle period@7-8 brightness@9: " + hexs(hid.writes[0]),
)
hid.writes.clear()
dev.set_colors([0x0000FF])
check(hid.writes == [], "colors while a non per-LED effect runs are only cached")
hid.writes.clear()
dev.close()
check(hid.writes[0] == long_frame(0xFF, 0x0E, 0x87, bytes([0x00, 0x00])), "0x8070 release manageSwControl(0,0)")

# ---- 3. receiver -------------------------------------------------------------------------------------------------
script, state = make_receiver()
hid = FakeHid(script, "/dev/hidraw-rcv")
link = lm.HidppLink(hid, wireless=True)
devs = lm.open_receiver(link, 0xC539)
w = hid.writes
check(w[0] == short_frame(0xFF, 0x81, 0x00), "RAP read notifications register: " + hexs(w[0]))
check(
    w[1] == short_frame(0xFF, 0x80, 0x00, b"\x00\x01\x00") and state["notif"] == 1,
    "RAP enable wireless notifications: " + hexs(w[1]),
)
check(w[2] == short_frame(0xFF, 0x81, 0x02), "RAP read connection state (device count)")
check(w[3] == short_frame(0xFF, 0x80, 0x02, b"\x02\x00\x00"), "RAP fake reconnect to replay pairings: " + hexs(w[3]))
check(
    short_frame(0xFF, 0x83, 0xB5, b"\x40") in w and short_frame(0xFF, 0x83, 0xB5, b"\x41") in w,
    "pairing names read for slots 1 and 2",
)
check(len(devs) == 1, f"one awake device built, asleep slot skipped ({len(devs)})")
if devs:
    m = devs[0]
    check(m.dev_idx == 1 and "slot 1" in m.device.location, f"location {m.device.location!r}")
    check(m.device.serial == "12345678", f"serial from receiver pairing info: {m.device.serial!r}")
    check(m.device.name == "G502 LIGHTSPEED Wireless Gaming Mouse", "name read through the receiver")
    fap_writes = [x for x in w if x[0] == 0x11 and x[1] == 1]
    check(
        fap_writes and all(x[1] in (1, 2) for x in w if x[0] == 0x11),
        "feature calls go to slot indexes only, never 0xFF",
    )
    slot2 = [x for x in w if x[0] == 0x11 and x[1] == 2]
    check(
        slot2 == [long_frame(2, 0x00, 0x07, bytes([0x00, 0x01]))],
        f"asleep slot 2 got exactly one first-contact call before the receiver's 0x8F answer stopped it ({len(slot2)})",
    )
    check(
        fap_writes[0] == long_frame(1, 0x00, 0x07, bytes([0x00, 0x01])),
        "slot 1 first contact = root GetFeature(0x0001)",
    )
    hid.writes.clear()
    m.set_colors([RED, BLUE])
    check(
        hid.writes[0] == long_frame(1, 0x0E, 0x57, bytes([0x01, 0x03, 0x06])),
        "claim through receiver uses index 1: " + hexs(hid.writes[0]),
    )
    check(
        long_frame(1, 0x0E, 0x17, bytes([0x00, 0x01, 0xFF, 0x00, 0x00, 0x02])) in hid.writes,
        "direct color through receiver uses index 1",
    )
    check(link.users == 1 and not hid.closed, "receiver link stays open while a slot uses it")
    m.close()
    check(hid.closed, "receiver link closed when the last slot closes")

# asleep slot answered by the receiver must raise DeviceOffline quickly, never hang
hid = FakeHid(script, "/dev/hidraw-rcv")
t0 = time.monotonic()
try:
    lm.LogitechHidppDevice(lm.HidppLink(hid, wireless=True), 2, 0x4079, "G Pro", "Mouse", "HID: x")
    check(False, "asleep slot should raise")
except lm.DeviceOffline:
    check(True, f"asleep slot raises DeviceOffline in {time.monotonic() - t0:.2f}s")
except DeviceError as e:
    check(False, f"asleep slot raised plain DeviceError: {e}")

# a dead node (no answers at all) gives up within the first-contact budget
hid = FakeHid(lambda pkt: [], "/dev/hidraw-dead")
t0 = time.monotonic()
try:
    lm.open_wired(lm.HidppLink(hid), 0xC08D)
    check(False, "silent device should raise")
except DeviceError:
    dt = time.monotonic() - t0
    check(dt < 1.5, f"silent device gives up in {dt:.2f}s")

# ---- 4. tables / payload builder ------------------------------------------------------------------------------------
check(
    (0x046D, 0xC539) in lm.USB_IDS and (0x046D, 0xC53A) in lm.USB_IDS and (0x046D, 0xC08D) in lm.USB_IDS,
    "USB_IDS lists receivers and wired mice",
)
check(callable(lm.probe), "probe() exists")
check(
    lm.effect_payload(0, 2, 0x0004, 0, 0x1388, 100, 6, False)
    == bytes([0, 2, 0, 0, 0, 0, 0, 0, 0x88, 6, 100, 0x13, 0, 0, 0, 0]),
    "Color Wave layout: period lo@8, dir@9, brightness@10, period hi@11",
)
check(
    lm.effect_payload(1, 4, 0x000B, 0x0000FF, 0x0064, 100, 0, False)[:8] == bytes([1, 4, 0xFF, 0, 0, 0, 0x00, 0x64]),
    "Ripple layout: color@2-4, period@6-7",
)
check(
    lm.effect_payload(0, 1, 0x0001, 0, 0, 100, 0, True) == bytes([0, 1] + [0] * 10 + [1, 0, 0, 0]),
    "Static black keeps marker 0, persist@12",
)

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
