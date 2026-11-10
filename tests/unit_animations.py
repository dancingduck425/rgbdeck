"""Animations are pure functions of time: check shape, range, determinism and what each one promises."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck import animations as an  # noqa: E402
from rgbdeck.models import rgb_to_u32, u32_to_rgb  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


xs, ys = an.normalise([(i, i % 3) for i in range(12)])
check(min(xs) == 0.0 and max(xs) == 1.0 and min(ys) == 0.0 and max(ys) == 1.0, "positions normalised to 0..1")
check(an.normalise([(5, 5)]) == ([0.5], [0.5]), "a single LED sits at the centre")
check(an.normalise([]) == ([], []), "no LEDs, no positions")
base = [rgb_to_u32(200, 100, 50)] * 12
for kind, label, _tip in an.KINDS:
    a = an.render(kind, 1.234, xs, ys, base, 50)
    b = an.render(kind, 1.234, xs, ys, base, 50)
    later = an.render(kind, 1.9, xs, ys, base, 50)
    ok = len(a) == 12 and all(0 <= c <= 0xFFFFFF for c in a)
    moves = a != later or kind in an.NEEDS_AUDIO  # music animations move with the sound, not the clock
    check(ok and a == b and moves, f"{label}: one color per LED, same time same frame, moves with time")
    check(len(an.render(kind, 0.5, [0.5], [0.5], [0], 50)) == 1, f"{label}: works on a one-LED device")
wave = an.render("wave", 0.0, xs, ys, base)
check(len(set(wave)) > 6, "wave: colors differ along the device")
cyc = an.render("cycle", 2.0, xs, ys, base)
check(len(set(cyc)) == 1, "cycle: every LED shows the same color")
levels = [max(u32_to_rgb(an.render("breathe", t / 20, xs, ys, base, 50)[0])) for t in range(200)]
check(
    max(levels) >= 195 and min(levels) <= 20,
    f"breathing: the device's own color from nearly off to full ({min(levels)}..{max(levels)})",
)
r, g, b = u32_to_rgb(an.render("breathe", 0.37, xs, ys, base, 50)[0])
check(abs(r - 2 * g) <= 2 and abs(g - 2 * b) <= 2, "breathing: keeps the hue")
check(
    any(an.render("breathe", 5.0 / an.rate(50) + 0.5 / an.rate(50), xs, ys, [0] * 12, 50)),
    "breathing: a dark device breathes white",
)
fire = [u32_to_rgb(c) for t in range(30) for c in an.render("fire", t / 7, xs, ys, base, 50)]
check(all(r >= g >= b for r, g, b in fire), "fire: only reds, oranges and yellows")
tw = [an.render("twinkle", t / 10, xs, ys, base, 50) for t in range(100)]
dim = rgb_to_u32(44, 22, 11)
check(
    any(c == dim for f in tw for c in f) and any(max(u32_to_rgb(c)) > 200 for f in tw for c in f),
    "starlight: dimmed colors with bright glints",
)
check(an.rate(1) < an.rate(50) < an.rate(100), "speed slider is monotonic")
pal = [rgb_to_u32(255, 0, 0), rgb_to_u32(0, 0, 255)]
check(
    all(c & 0x00FF00 == 0 for c in an.render("wave", 0.7, xs, ys, base, 50, pal)),
    "own colors: the wave stays inside them",
)
check(
    an.gradient(pal, 0.0) == pal[0] and an.gradient(pal, 0.5) == pal[1] and an.gradient(pal, 1.0) == pal[0],
    "gradient is a ring",
)


class Sig:
    def __init__(self, bands, bass=0.0, beat=0.0, active=True):
        self.bands, self.bass, self.beat, self.active = bands, bass, beat, active


quiet = an.render("spectrum", 1.0, xs, ys, base, 50, None, Sig([0.0] * 8, active=False))
check(
    quiet == [rgb_to_u32(70, 35, 18)] * 12 or max(u32_to_rgb(quiet[0])) < 100,
    "music: silence shows the own look, dimmed",
)
bassy = an.render("spectrum", 1.0, xs, ys, base, 50, None, Sig([1.0, 1.0, 0, 0, 0, 0, 0, 0]))
left = max(max(u32_to_rgb(c)) for c, x in zip(bassy, xs) if x < 0.15)
right = max(max(u32_to_rgb(c)) for c, x in zip(bassy, xs) if x > 0.7)
check(left > 200 and right < 40, f"spectrum: bass lights the left side only ({left} vs {right})")
strip = an.render("spectrum", 1.0, [0.0, 0.5, 1.0], [0.5] * 3, [0] * 3, 50, None, Sig([1.0] * 4 + [0.0] * 4))
check(max(u32_to_rgb(strip[0])) > 200 and max(u32_to_rgb(strip[2])) < 40, "spectrum on a strip: brightness per band")
soft = max(u32_to_rgb(an.render("pulse", 1.0, xs, ys, base, 50, None, Sig([0] * 8, bass=0.0))[0]))
hard = max(u32_to_rgb(an.render("pulse", 1.0, xs, ys, base, 50, None, Sig([0] * 8, bass=1.0, beat=1.0))[0]))
check(hard > 190 and soft < 40, f"beat pulse: brightness follows the bass ({soft} -> {hard})")
cold, hot = (
    an.render("status", 0.0, [0.5], [0.5], [0], 50, None, 0.0)[0],
    an.render("status", 0.0, [0.5], [0.5], [0], 50, None, 1.0)[0],
)
check(
    u32_to_rgb(cold)[2] > 200 and u32_to_rgb(cold)[0] < 60 and u32_to_rgb(hot)[0] > 150 and u32_to_rgb(hot)[2] < 30,
    "status: blue when cool, red when hot",
)
check(an.render("nonsense", 1.0, xs, ys, base) == base, "an unknown animation leaves the colors alone")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
