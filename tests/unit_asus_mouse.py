# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""ASUS ROG mouse behind the SpeedNova 8K receiver (asus_mouse.py) against a simulated receiver and mouse.

The byte strings marked "captured" are from the notes of the rogctl project: ASUS GearLink talking to a real
Harpe II Ace, and that mouse's answers. Everything the driver sends is checked against them byte for byte."""

import os
import sys
import tempfile
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = tempfile.mkdtemp(prefix="rgbdeck-unit-")
os.environ["XDG_CONFIG_HOME"] = os.environ["APPDATA"] = os.environ["HOME"] = TMP  # the window's settings go here
os.environ["RGBDECK_NO_BLUETOOTH"] = "1"
os.environ["RGBDECK_NO_UPDATE_CHECK"] = "1"
os.environ["RGBDECK_NO_DESKTOP_HOOKS"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rgbdeck.backend as backend  # noqa: E402
from rgbdeck import device_shapes  # noqa: E402
from rgbdeck.hw import asus_mouse as am  # noqa: E402
from rgbdeck.hw.hidinfo import HidInfo  # noqa: E402
from rgbdeck.models import DeviceError, MODE_COLORS_PER_LED, rgb_to_u32  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asus_mouse_sim import FakeMouse  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def hx(data) -> str:
    return bytes(data).hex(" ")


RED, GREEN, BLUE, BLACK = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255), 0


def mouse(**kw):
    fake = FakeMouse(**kw)
    wireless = fake.rid != 0
    drv = am.AsusMouse("fake", am.NAME, fake.rid, wireless, hid=fake)
    fake.take()
    return fake, drv


def later(drv, seconds):
    """Let `seconds` pass for the driver's own clocks without waiting for them."""
    drv._sent_at -= seconds
    drv._asked_at -= seconds


# ------------------------------------------------------------------------------------- the bytes themselves
check(
    hx(am.lighting_packet(3, am.FX_STATIC, 50, GREEN)[:10]) == "03 51 28 00 00 00 32 00 ff 00",
    "static green at 50 % is what GearLink sends (captured)",
)
check(
    hx(am.lighting_packet(3, am.FX_BREATHING, 50, RED)[:10]) == "03 51 28 00 00 01 32 ff 00 00",
    "breathing red at 50 % (captured)",
)
check(
    hx(am.lighting_packet(3, am.FX_CYCLE, 40, BLACK)[:10]) == "03 51 28 00 00 02 28 00 00 00",
    "color cycle at 40 % (captured)",
)
check(
    hx(am.lighting_packet(3, am.FX_REACTIVE, 50, RED)[:10]) == "03 51 28 00 00 04 32 ff 00 00",
    "reactive red at 50 % (captured)",
)
check(
    hx(am.lighting_packet(3, am.FX_BATTERY, 0, RED)[:10]) == "03 51 28 00 00 06 00 ff 00 00",
    "battery level (captured)",
)
check(
    hx(am.lighting_packet(3, am.FX_STATIC, 100, GREEN)[:10]) == "03 51 28 00 00 00 64 00 ff 00"
    and hx(am.lighting_packet(3, am.FX_STATIC, 0, GREEN)[:10]) == "03 51 28 00 00 00 00 00 ff 00",
    "full brightness is 64 hex, off is brightness 0 (captured)",
)
check(am.lighting_packet(3, am.FX_STATIC, 250, RED)[6] == 100, "brightness never goes past 100 %")
p = am.lighting_packet(3, am.FX_STATIC, 100, rgb_to_u32(1, 2, 3))
check(len(p) == 64 and not any(p[10:]), "a request is 64 bytes, zeros after the command")
check(hx(am.packet(3, am.CMD_PROFILE, am.SAVE)[:4]) == "03 50 03 00", "save is 50 03 (captured)")

captured = bytes.fromhex("0312070000500214d80f000001") + bytes(51)
check(am.parse_battery(captured) == (80, False), "a real SpeedNova battery answer reads 80 %, not charging")
captured = b"\x00" + bytes.fromhex("120700002f0214eb0f01") + bytes(53)
check(am.parse_battery(captured) == (47, True), "a real answer over the cable reads 47 %, charging")
captured = bytes.fromhex("03120300000432ff0000") + bytes(54)
check(am.parse_lighting(captured) == (am.FX_REACTIVE, 50, RED), "a real lighting answer reads reactive, 50 %, red")

# ------------------------------------------------------------------------------------- opening the mouse
fake = FakeMouse()
drv = am.AsusMouse("fake", am.NAME, 3, True, hid=fake)
sent = fake.take()
check(
    [hx(s) for s in sent] == ["03 12 07", "03 12 03"],
    f"opening only reads: the battery, then the lighting ({[hx(s) for s in sent]})",
)
d = drv.device
check(drv.awake and drv.battery() == (80, False), "awake, battery 80 %")
check(
    d.type_name == "Mouse" and d.name == "ROG Harpe II Ace" and d.led_names == ["Scroll Wheel"] and len(d.colors) == 1,
    "a mouse with one light, the scroll wheel",
)
check(
    [m.name for m in d.modes]
    == ["Direct", "Static", "Breathing", "Spectrum Cycle", "Reactive", "Battery Level", "Off"],
    "the mouse's own effects are offered next to painting",
)
check(
    d.current_mode.name == "Reactive" and d.current_mode.brightness == 50 and d.current_mode.colors == [RED],
    "it starts on the effect the mouse is showing, with its color and brightness",
)
check(d.modes[0].color_mode == MODE_COLORS_PER_LED and d.mode_named("Static").has_mode_colors, "paintable; Static")
check(all(m.can_save for m in d.modes[1:]) and not d.modes[0].can_save, "effects can be stored in the mouse")
check(not fake.take(), "nothing was written while opening")

_f, drv2 = mouse(effect=am.FX_STATIC, pct=50, rgb=(0, 200, 100))
check(
    drv2.device.active_mode == 0 and drv2.device.colors == [rgb_to_u32(0, 100, 50)],
    "a mouse showing a plain color starts as its wheel painted in that color",
)
_f, drv2 = mouse(effect=am.FX_OFF, pct=50)
check(drv2.device.current_mode.name == "Off", "lighting switched off by other software is shown as Off")
_f, drv2 = mouse(effect=0x05, pct=50)
check(drv2.device.active_mode == 0 and drv2.device.colors == [0], "an effect RGB Deck does not know is left alone")

layout = device_shapes.build(d)
check(
    layout is not None and layout.kind == "mouse" and [g.led for g in layout] == [0],
    "it is drawn as a mouse with the wheel as its light",
)

# ------------------------------------------------------------------------------------- colors from the app
fake, drv = mouse()
drv.set_colors([GREEN])
sent = fake.take()
check(
    [hx(s) for s in sent] == ["03 51 28 00 00 00 64 00 ff"],
    f"a color is one 'static' command and nothing is stored ({[hx(s) for s in sent]})",
)
check(fake.light == (0, 100, 0, 255, 0) and fake.stored != fake.light, "the mouse shows it, its memory is untouched")
later(drv, 1)
drv.set_colors([GREEN])
check(not fake.take(), "the same color again is not sent again")
drv.set_colors([BLACK])
check(fake.light[:2] == (0, 0) and len(fake.take()) == 1, "black is sent as brightness 0")
later(drv, 1)
drv.set_colors([rgb_to_u32(10, 20, 30)])
check(fake.light == (0, 100, 10, 20, 30), "dim colors go out as they are, at full brightness")
fake.take()

# at most 20 changes a second over the radio, and the newest one always arrives
later(drv, 1)
drv.set_colors([RED])
drv.set_colors([GREEN])
drv.set_colors([BLUE])
check(len(fake.take()) == 1 and fake.light[2:] == (255, 0, 0), "changes in quick succession: the first goes out")
check(drv.keepalive_interval == am.MIN_GAP, "... and the driver asks to be called again in a moment")
drv.keepalive()
check(
    [hx(s) for s in fake.take()] == ["03 51 28 00 00 00 64 00 00 ff"] and drv.keepalive_interval == am.AWAKE_POLL,
    "... which sends the newest color, skipping the one in between",
)

# ------------------------------------------------------------------------------------- effects and storing
fake, drv = mouse()
m = drv.device.mode_named("Breathing")
m.colors, m.brightness = [BLUE], 70
drv.set_mode(m)
check(
    [hx(s) for s in fake.take()] == ["03 51 28 00 00 01 46 00 00 ff"] and drv.device.current_mode is m,
    "an effect carries its code, brightness and color",
)
later(drv, 1)
m = drv.device.mode_named("Spectrum Cycle")
m.brightness = 40
drv.set_mode(m)
check([hx(s) for s in fake.take()] == ["03 51 28 00 00 02 28"], "an effect without a color sends none")
later(drv, 1)
drv.set_mode(drv.device.mode_named("Off"))
check([hx(s) for s in fake.take()] == ["03 51 28"] and fake.light[:2] == (0, 0), "Off is static at brightness 0")
later(drv, 1)
m = drv.device.mode_named("Static")
m.colors, m.brightness = [RED], 100
drv.set_mode(m, save=True)
check(
    [hx(s) for s in fake.take()] == ["03 51 28 00 00 00 64 ff", "03 50 03"] and fake.stored == (0, 100, 255, 0, 0),
    "storing: the look, then the save, and the mouse keeps it",
)
drv.set_mode(m, save=True)
check(len(fake.take()) == 2, "storing sends the look again even if it is already shown")

# "keep the look when closing": a painted color is stored as it is shown
fake, drv = mouse(effect=am.FX_STATIC, pct=30, rgb=(0, 0, 255))
m = drv.device.mode_named("Static")
check(m.brightness == 30 and m.colors == [BLUE], "the Static effect starts with the mouse's own brightness")
drv.set_colors([GREEN])
m.colors = [GREEN]
later(drv, 1)
drv.set_mode(m, save=True)
check(
    fake.stored == (0, 100, 0, 255, 0),
    f"stored right out of painting: the painted color, not dimmed to the effect's 30 % ({fake.stored})",
)
later(drv, 1)
drv.set_mode(m, save=True)
check(fake.stored == (0, 30, 0, 255, 0), "stored as the Static effect: with the effect's brightness")
later(drv, 1)
drv.set_colors([BLACK])
m.colors = [BLACK]
drv.set_mode(m, save=True)
check(fake.stored[:2] == (0, 0), "a wheel painted black is stored as off")

fake, drv = mouse()
fake.refuse_saves = 1
m = drv.device.mode_named("Static")
m.colors = [GREEN]
drv.set_mode(m, save=True)
sent = fake.take()
check(
    [hx(s) for s in sent[1:]] == ["03 50 03", "03 50 03"] and fake.stored[2:] == (0, 255, 0),
    "a save the mouse refuses after sitting unused is asked for once more",
)
fake, drv = mouse()
fake.refuse_then_echo = True
t0 = time.monotonic()
drv.set_colors([BLUE])
check(
    len(fake.take()) == 1 and drv.awake and fake.light[2:] == (0, 0, 255) and time.monotonic() - t0 < 0.2,
    "a refusal that comes just ahead of the confirmation is not taken for sleep",
)

# reports that are not the answer
fake, drv = mouse()
fake.noise = [
    bytes([0x01, 0x12, 0x07]) + bytes(61),  # another report id on the same interface
    bytes([0x03, 0x12, 0x07, 0, 0, 55]) + bytes(58),  # the answer to somebody else's battery question
    bytes(3),  # too short to be anything
]
drv.set_colors([RED])
check(fake.light[2:] == (255, 0, 0) and drv.awake, "other reports on the interface are passed over")

# ------------------------------------------------------------------------------------- sleep
fake, drv = mouse()
drv.set_colors([GREEN])
fake.take()
fake.on = False
later(drv, 1)
drv.set_colors([RED])  # must not raise
sent = fake.take()
check(
    not drv.awake and drv.battery() is None and [hx(s) for s in sent] == ["03 51 28 00 00 00 64 ff", "03 12 07"],
    "a mouse that fell asleep: the color is refused, one question confirms it, no error",
)
for c in (BLUE, GREEN, RED, BLUE):
    drv.set_colors([c])
check(not fake.take(), "while it sleeps nothing more is sent to it")
check(drv.keepalive_interval == am.ASLEEP_POLL, "it is looked for every ten seconds")
try:
    drv.set_mode(drv.device.mode_named("Static"), save=True)
    check(False, "storing into a sleeping mouse")
except DeviceError as e:
    check("asleep" in str(e), f"storing into a sleeping mouse says so ({e})")
fake.take()
drv.set_colors([BLUE])
drv.keepalive()
check([hx(s) for s in fake.take()] == ["03 12 07"] and not drv.awake, "still asleep: one question, nothing else")
fake.on = True
fake.light = fake.stored  # what it shows after waking is its stored look
drv.keepalive()
sent = fake.take()
check(
    drv.awake and [hx(s) for s in sent] == ["03 12 07", "03 51 28 00 00 00 64 00 00 ff"],
    f"awake again: the look it should have is sent ({[hx(s) for s in sent]})",
)
check(drv.battery() == (80, False), "and its battery is known again")

# the app asking for the battery is what finds the mouse awake: the look still goes out
fake, drv = mouse()
drv.set_colors([GREEN])
fake.on = False
later(drv, 1)
drv.set_colors([RED])
fake.take()
fake.on = True
fake.light = fake.stored
later(drv, 6)
check(drv.battery() == (80, False) and drv.keepalive_interval == am.MIN_GAP, "found awake by a battery question")
drv.keepalive()
check(fake.light[2:] == (255, 0, 0), "... its look is sent all the same")

# asleep while colors keep coming (an animation): the question is asked from there, every ten seconds
fake, drv = mouse()
fake.on = False
drv.set_colors([RED])
fake.take()
fake.on = True
drv.set_colors([GREEN])
check(not fake.take(), "an animation does not ask a sleeping mouse on every frame")
later(drv, am.ASLEEP_POLL + 1)
drv.set_colors([BLUE])
check(
    drv.awake and [hx(s) for s in fake.take()] == ["03 12 07", "03 51 28 00 00 00 64 00 00 ff"],
    "... but after ten seconds it does, and the mouse gets the current color",
)

# a receiver that says nothing at all for a sleeping mouse
am.REPLY_TIMEOUT = 0.05
fake, drv = mouse()
fake.silent = True
t0 = time.monotonic()
drv.set_colors([RED])
check(not drv.awake and time.monotonic() - t0 < 0.5, "no answer at all also means asleep, and is not waited for long")
fake.silent = False
drv.keepalive()
check(drv.awake and fake.light[2:] == (255, 0, 0), "and it is picked up again the same way")

# a receiver answering 0 % for a mouse that is not there
fake, drv = mouse()
fake.battery = 0
drv.keepalive()
check(not drv.awake and drv.battery() is None, "0 % and not charging is a mouse that is not there")
fake.charging = True
drv.keepalive()
check(drv.awake and drv.battery() == (0, True), "0 % while charging is an empty mouse on its cable")

# the mouse going to sleep while nothing is being sent
fake, drv = mouse()
drv.set_colors([RED])
fake.take()
fake.on = False
drv.keepalive()
check(not drv.awake and [hx(s) for s in fake.take()] == ["03 12 07"], "the regular question notices sleep")

# a receiver whose mouse is asleep when RGB Deck starts
fake = FakeMouse()
fake.on = False
drv = am.AsusMouse("fake", am.NAME, 3, True, hid=fake)
check(
    not drv.awake and drv.device.active_mode == 0 and drv.device.colors == [0] and not fake.closed,
    "a sleeping mouse is still listed (its receiver is there)",
)
drv.set_colors([GREEN])
fake.take()
fake.on = True
drv.keepalive()
check(fake.light == (0, 100, 0, 255, 0), "and gets its color when it wakes up")

# ------------------------------------------------------------------------------------- a mouse without it
fake = FakeMouse()
fake.lighting = False
try:
    am.AsusMouse("fake", am.NAME, 3, True, hid=fake)
    check(False, "a mouse that refuses the lighting question")
except DeviceError as e:
    check(fake.closed, f"a mouse that answers but has no such lighting is not listed ({e})")
fake, drv = mouse()
fake.lighting = False
drv.set_colors([RED])
n = len(fake.take())
later(drv, 1)
drv.set_colors([RED])
check(drv.awake and n == 3 and not fake.take(), "a mouse that stops taking the command is not asked over and over")
drv.set_colors([GREEN])
drv.set_colors([BLUE])
check(
    not fake.take() and drv.keepalive_interval == am.AWAKE_POLL,
    "... not for new colors either: the other devices must not wait for it on every frame",
)
fake.lighting = True
drv._hold_until = 0.0  # ten seconds later
drv.keepalive()
check(fake.light[2:] == (0, 0, 255), "... and when it takes commands again it gets the newest color")

# ------------------------------------------------------------------------------------- on the cable
fake = FakeMouse(rid=0, effect=am.FX_STATIC, pct=100, rgb=(0, 0, 255), battery=47, charging=True)
drv = am.AsusMouse("fake", am.NAME, 0, False, hid=fake)
check(drv.awake and drv.battery() == (47, True) and drv.device.colors == [BLUE], "over the cable: read the same way")
check("cable" in drv.device.description, "and it says it is on its cable")
fake.take()
drv.set_colors([RED])
check(
    [hx(s) for s in fake.take()] == ["00 51 28 00 00 00 64 ff"] and fake.light[2:] == (255, 0, 0),
    "written with a leading 00, answers read without it",
)
fake = FakeMouse(rid=0)
fake.on = False
try:
    am.AsusMouse("fake", am.NAME, 0, False, hid=fake)
    check(False, "a cable that does not answer")
except DeviceError:
    check(fake.closed, "a cable that only charges (the mouse is on its receiver) is not a device")

# ------------------------------------------------------------------------------------- finding it
VENDOR3 = bytes.fromhex("0600ff0901a1018503150026ff00750895400902810209039102c0")


def info(pid, interface, name="ASUSTeK ROG SPEEDNOVA 8K RECEIVER", desc=b""):
    return HidInfo(f"node-{pid:04x}-{interface}", 0x0B05, pid, name, interface, desc)


def run_probe(infos, fakes):
    opened = []

    def open_node(path):
        opened.append(path)
        return fakes[path]

    am.enumerate_hid = lambda vid=None, pid=None, **k: [i for i in infos if vid in (None, i.vid)]
    am.HidDevice = open_node
    return am.probe(), opened


receiver = FakeMouse()
found, opened = run_probe(
    [info(0x1AD0, 0), info(0x1AD0, 1), info(0x1AD0, 2), info(0x1AD0, 3), info(0x19AF, 2, "AURA LED Controller")],
    {"node-1ad0-2": receiver},
)
check(
    opened == ["node-1ad0-2"] and len(found) == 1 and found[0].wireless and found[0].report_id == 3,
    f"only interface 2 of the receiver is opened ({opened})",
)
check(found[0].device.name == "ROG Harpe II Ace" and "SpeedNova" in found[0].device.description, "named as the mouse")
found, _ = run_probe([info(0x1AD0, 2, "ROG HARPE II EXTREME EDITION 20")], {"node-1ad0-2": FakeMouse()})
check(found[0].device.name == "ROG Harpe II Extreme Edition 20", "the Extreme Edition is told by the receiver's name")
found, opened = run_probe(
    [info(0x1AD0, -1, desc=bytes.fromhex("05010906a101c0")), info(0x1AD0, -1, desc=VENDOR3)],
    {"node-1ad0--1": FakeMouse()},
)
check(len(opened) == 1 and len(found) == 1, "with no interface number, the vendor collection with report 3 is it")
check(
    am._right_interface(info(0x1C69, -1, desc=bytes.fromhex("0601ff0901a101c0")), 0, 0)
    and not am._right_interface(info(0x1C69, -1, desc=VENDOR3), 0, 0),
    "... and for the cable the vendor collection without report ids",
)

asleep, cable = FakeMouse(), FakeMouse(rid=0)
asleep.on = False
found, opened = run_probe(
    [info(0x1AD0, 2), info(0x1C69, 0, "ASUSTeK ROG HARPE II ACE")], {"node-1ad0-2": asleep, "node-1c69-0": cable}
)
check(
    len(found) == 1 and not found[0].wireless and asleep.closed and not cable.closed,
    "set to its cable: the cable is the device, the receiver (reporting it asleep) is let go",
)
both_r, both_c = FakeMouse(), FakeMouse(rid=0)
found, _ = run_probe([info(0x1C69, 0), info(0x1AD0, 2)], {"node-1ad0-2": both_r, "node-1c69-0": both_c})
check(len(found) == 1 and found[0].wireless and both_c.closed, "answering on both: one mouse, through the receiver")
alone = FakeMouse()
alone.on = False
found, _ = run_probe([info(0x1AD0, 2)], {"node-1ad0-2": alone})
check(len(found) == 1 and not found[0].awake, "a receiver alone with its mouse asleep stays listed")
charging, away = FakeMouse(rid=0), FakeMouse()
charging.on = away.on = False
found, _ = run_probe([info(0x1AD0, 2), info(0x1C69, 0)], {"node-1ad0-2": away, "node-1c69-0": charging})
check(len(found) == 1 and found[0].wireless and charging.closed, "asleep and on its charging cable: listed once")
nolight = FakeMouse()
nolight.lighting = False
found, _ = run_probe([info(0x1AD0, 2)], {"node-1ad0-2": nolight})
check(found == [] and nolight.closed, "another mouse on the same receiver, without this lighting, is skipped")
found, _ = run_probe([info(0x19AF, 2, "AURA LED Controller")], {})
check(found == [], "other ASUS devices are not touched")

# ------------------------------------------------------------------------------------- inside the app's backend
check(am.EXPERIMENTAL and "asus_mouse" in backend.FROZEN_DRIVER_MODULES, "listed for the packaged build; experimental")
check(not backend.is_late(am), "found in the first, quick part of the search (it is a USB device)")
real = [m.__name__.rsplit(".", 1)[-1] for m in backend.discover_drivers()]
check("asus_mouse" in real, "the driver is discovered with the others")

am.REPLY_TIMEOUT = 0.3
fake = FakeMouse()
mod = types.ModuleType("rgbdeck.hw.asus_mouse")
mod.probe = lambda: [am.AsusMouse("fake", am.NAME, 3, True, hid=fake)]
mod.USB_IDS, mod.EXPERIMENTAL = am.USB_IDS, True
backend.discover_drivers = lambda: [mod]
backend.hidraw.enumerate_hid = lambda *a, **k: []
b = backend.LocalBackend()
b.connect()
dev = b.devices[0]
check(dev.experimental and dev.name == "ROG Harpe II Ace", "adopted by the backend, marked experimental")
check(b.battery(dev) == (80, False), "the battery is read through the backend")
fake.take()
b.update_mode(dev, dev.modes[0])
t0 = time.monotonic()
for i in range(40):  # more than a second's worth of animation frames in a burst
    b.update_leds(dev, [rgb_to_u32(i, 0, 255 - i)])
    time.sleep(0.004)
took = time.monotonic() - t0
b.wait_idle(2.0)
time.sleep(0.25)
sent = [s for s in fake.take() if s[1:3] == b"\x51\x28"]
check(fake.light == (0, 100, 39, 0, 216), f"after a burst of frames the mouse shows the last one ({fake.light})")
allowed = int(took / am.MIN_GAP) + 3
check(
    len(sent) <= allowed and len(sent) < 40,
    f"and the burst went out at the radio's pace: {len(sent)} commands for 40 frames in {took:.2f} s",
)
fake.on = False
b.update_leds(dev, [RED])
b.wait_idle(2.0)
check(b.take_error() == "", "a mouse that sleeps is not an error in the app")
b.shutdown()
check(fake.closed, "closed with the backend")

# ------------------------------------------------------------------------------------- in the real window
from PySide6.QtWidgets import QApplication  # noqa: E402
import rgbdeck.app as app_mod  # noqa: E402

app_mod.IS_WINDOWS = False  # login entries as files in TMP on every platform
app = QApplication.instance() or QApplication([])


def window(fake_mouse):
    mod.probe = lambda: [am.AsusMouse("fake", am.NAME, 3, True, hid=fake_mouse)]
    client = backend.LocalBackend()
    win = app_mod.MainWindow(client)
    client.refresh()
    win._scan_done()
    fake_mouse.take()
    return client, win


def settle(client):
    client.wait_idle(2.0)
    time.sleep(3 * am.MIN_GAP)  # a color that had to wait for its turn
    client.wait_idle(2.0)


def color(win, rgb):
    win.director.host.apply_color(rgb)
    win._flush()  # painted colors leave with the window's frame timer; here that is done by hand


def lighting(fake_mouse):
    return [s for s in fake_mouse.take() if s[1:3] == b"\x51\x28"]


fake = FakeMouse()  # as it comes out of the box: reactive, red, 50 %
client, w = window(fake)
dev = client.devices[0]
check(w.dev is dev and dev.current_mode.name == "Reactive", "the window opens on the mouse and its effect")
color(w, (0, 255, 0))
settle(client)
check(fake.light == (am.FX_REACTIVE, 50, 0, 255, 0), f"a color goes to the effect the mouse is on ({fake.light})")
client.update_mode(dev, dev.modes[0])
color(w, (0, 0, 255))
settle(client)
check(fake.light == (0, 100, 0, 0, 255) and dev.colors == [BLUE], "painted: the wheel takes the color")
fake.take()
w._set_animation("cycle", 80)
t0 = time.monotonic()
for _ in range(12):
    w._anim_tick()
    client.wait_idle(1.0)
    time.sleep(0.01)
took = time.monotonic() - t0
settle(client)
frames = lighting(fake)
check(
    2 <= len(frames) <= int(took / am.MIN_GAP) + 3 and fake.light[:2] == (0, 100) and fake.light[2:] != (0, 0, 255),
    f"an animation plays on the wheel, at the radio's pace ({len(frames)} commands for 12 frames in {took:.2f} s)",
)
w._set_animation(None)
settle(client)
check(fake.light == (0, 100, 0, 0, 255), "stopping it brings the painted color back")
w._everything_off()
settle(client)
check(fake.light[:2] == (0, 0) and dev.current_mode.name == "Off", "everything off: the wheel goes dark")
client.update_mode(dev, dev.modes[0])
color(w, (255, 106, 0))
settle(client)
check(fake.stored == (am.FX_REACTIVE, 50, 255, 0, 0), "until here nothing was written into the mouse's memory")
fake.take()
w.close()
client.wait_idle(3.0)
sent = fake.take()
check(
    fake.stored == (0, 100, 255, 106, 0) and sent[-1][1:3] == b"\x50\x03",
    f"closing stores the painted color in the mouse, as shown ({fake.stored})",
)
client.shutdown()

fake = FakeMouse()
client, w = window(fake)
w.close()
client.wait_idle(3.0)
check(not fake.take() and fake.light == fake.stored, "a mouse nothing was sent to is left exactly as it was")
client.shutdown()

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
