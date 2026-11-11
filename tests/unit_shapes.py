"""Device pictures (device_shapes.py): every light gets a place inside the picture, lights do not sit on top
of each other, a folded strip still counts as one line for effects, and odd devices fall back to the grid."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck import device_shapes  # noqa: E402
from rgbdeck.keyboard_layout import build_geometry, effect_positions  # noqa: E402
from rgbdeck.models import Device, Zone  # noqa: E402

fails = []
SINGLE, LINEAR, MATRIX = 0, 1, 2


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def dev(kind, zones, matrix=None):
    zs, names, start = [], [], 0
    for i, (name, ztype, n) in enumerate(zones):
        zs.append(Zone(i, name, ztype, n, n, n, matrix if ztype == MATRIX else None, start))
        names += [name if n == 1 else f"{name} {k + 1}" for k in range(n)]
        start += n
    return Device(0, kind, kind, "", "", "", "", "", 0, [], zs, names, [0] * start)


def sound(layout, d, what, shared=False):
    """Every light placed, inside the picture, and no two different lights overlapping."""
    w, h = layout.extent()
    placed = {g.led for g in layout}
    check(placed == set(range(len(d.colors))), f"{what}: every light has a place ({len(placed)}/{len(d.colors)})")
    inside = all(g.x >= -1e-6 and g.y >= -1e-6 and g.x + g.w <= w + 1e-6 and g.y + g.h <= h + 1e-6 for g in layout)
    check(inside, f"{what}: all inside the picture")
    clash = [
        (a.led, b.led)
        for i, a in enumerate(layout)
        for b in layout[i + 1 :]
        if a.led != b.led
        and a.x < b.x + b.w - 0.02
        and b.x < a.x + a.w - 0.02
        and a.y < b.y + b.h - 0.02
        and b.y < a.y + a.h - 0.02
    ]
    check(not clash, f"{what}: no two lights on top of each other {clash[:3]}")
    if not shared:
        check(len(layout) == len(placed), f"{what}: one place per light")
    check(all(g.w > 0.2 and g.h > 0.2 for g in layout), f"{what}: big enough to click")


mice = {
    "Aerox 5": [("Front", SINGLE, 1), ("Middle", SINGLE, 1), ("Rear", SINGLE, 1), ("Reactive", SINGLE, 1)],
    "Rival 600": [("Logo", SINGLE, 1), ("Scroll Wheel", SINGLE, 1)]
    + [(f"{side} {part}", SINGLE, 1) for side in ("Left", "Right") for part in ("top", "mid", "bottom")],
    "underglow": [("Logo", SINGLE, 1), ("Scroll Wheel", SINGLE, 1), ("Underglow", LINEAR, 14)],
    "Logitech": [("Primary", SINGLE, 1), ("Logo", SINGLE, 1)],
    "six in a row": [(f"Primary {i}", SINGLE, 1) for i in range(1, 7)],
    "long underglow": [("Mouse", LINEAR, 40)],
}
for name, zones in mice.items():
    d = dev("Mouse", zones)
    layout = build_geometry(d)
    check(layout.kind == "mouse" and layout.backdrop, f"mouse {name}: drawn as a mouse")
    sound(layout, d, f"mouse {name}")

d = dev("Mouse", mice["Rival 600"])
by_led = {g.led: g for g in build_geometry(d)}
wheel, logo = by_led[1], by_led[0]
check(wheel.y < logo.y, "the wheel sits in front of the logo")
check(all(by_led[i].x < wheel.x for i in (2, 3, 4)) and all(by_led[i].x > wheel.x for i in (5, 6, 7)), "left and right")
check(by_led[2].y < by_led[3].y < by_led[4].y, "top, mid, bottom in that order")
check(by_led[2].label == "top" and by_led[5].label == "top", "side lights are labelled without the side")

# light strips
d = dev("LED Strip", [("Segments", LINEAR, 15)])
layout = build_geometry(d)
check(layout.kind == "strip" and len({g.y for g in layout}) == 1, "a short strip is one row")
sound(layout, d, "strip 15")
check([g.x for g in layout] == sorted(g.x for g in layout), "in order from left to right")

d = dev("LED Strip", [("LEDs", LINEAR, 60)])
layout = build_geometry(d)
sound(layout, d, "strip 60")
rows = sorted({g.y for g in layout})
check(len(rows) == 3, "a long strip is folded into rows")
second = [g for g in layout if g.y == rows[1]]
check(second[0].x > second[-1].x, "every second row runs back, so neighbours stay neighbours")
last_of_first = max((g for g in layout if g.y == rows[0]), key=lambda g: g.led)
check(abs(last_of_first.x - second[0].x) < 1e-6, "the fold is where the row ends")
pos = effect_positions(d)
check([pos[i][0] for i in range(60)] == [float(i) for i in range(60)], "for effects it is still one straight line")
check(any(step[-1] == "band" for step in layout.backdrop), "the bends are drawn")

d = dev("Accessory", [("Port 1", LINEAR, 34), ("Port 2", LINEAR, 16)])
layout = build_geometry(d)
check(layout.kind == "strip", "a hub with two light channels: two strips")
sound(layout, d, "two channels")
check([s[3] for s in layout.backdrop if s[0] == "text"] == ["Port 1", "Port 2"], "each channel is named")
pos = effect_positions(d)
check(pos[0][1] != pos[34][1], "the channels are separate lines for effects")

d = dev("LED Strip", [("LEDs", LINEAR, 204)])
sound(build_geometry(d), d, "strip 204")

d = dev("Light", [("Light", LINEAR, 1)])
layout = build_geometry(d)
check(layout.kind == "lamp" and len(layout) == 1 and layout[0].shape == "dot", "one light is drawn as a lamp")

# mainboard, headset, mouse mat
for n in (1, 8, 11, 12, 24, 60):
    d = dev("Motherboard", [("Aura Mainboard", LINEAR, n)])
    layout = build_geometry(d)
    check(layout.kind == "mainboard", f"mainboard with {n} lights: drawn as a board")
    sound(layout, d, f"mainboard {n}")

d = dev("Headset", [("Left", SINGLE, 1), ("Right", SINGLE, 1)])
layout = build_geometry(d)
sound(layout, d, "headset")
check(layout[0].x < layout[1].x, "left ear cup on the left")
d = dev("Headset", [("Logo", SINGLE, 1)])
layout = build_geometry(d)
check(len(layout) == 2 and {g.led for g in layout} == {0}, "a headset with one light shows it on both ear cups")
sound(layout, d, "headset, one light", shared=True)
d = dev("Headset", [("Zone", LINEAR, 5)])
sound(build_geometry(d), d, "headset, five lights")

for zones in (
    [("LED Strip", LINEAR, 15)],
    [("Zone 1", SINGLE, 1), ("Zone 2", SINGLE, 1)],
    [("Edge", LINEAR, 19), ("Logo", SINGLE, 1)],
):
    d = dev("Mousemat", zones)
    layout = build_geometry(d)
    check(layout.kind == "mousemat", f"mouse mat {zones[0][0]}: drawn as a mat")
    sound(layout, d, f"mouse mat {zones[0][0]}")

# what stays a grid
d = dev("Microphone", [("Top", SINGLE, 1), ("Bottom", SINGLE, 1)])
layout = build_geometry(d)
check(layout.kind == "grid" and not layout.backdrop, "a device without a picture keeps the grid")
check([g.label for g in layout] == ["Top", "Bottom"], "and its lights are labelled by name")
d = dev("Mouse", [("Matrix", MATRIX, 4)], matrix=[[0, 1], [2, 3]])
check(build_geometry(d).kind == "grid", "a mouse that reports a matrix keeps it")
d = dev("Mouse", [])
check(list(build_geometry(d)) == [], "a mouse without lights of its own has nothing to paint on")

# outlines for devices without separate lights
for kind in ("Mouse", "Headset", "Mousemat", "Motherboard", "LED Strip", "Light"):
    g = device_shapes.glyph(kind)
    check(g is not None and any(s[-1] == "body" for s in g.backdrop), f"outline for a {kind} without separate lights")
check(device_shapes.glyph("Speaker") is None, "no outline for types without a picture (plain block)")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
