"""Hardware-free checks: effect engine, keyboard geometry, color panel state, packet encoders."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.models import Device, Mode, Zone, rgb_to_u32, u32_to_rgb  # noqa: E402
from rgbdeck.keyboard_layout import build_geometry  # noqa: E402
from rgbdeck.effects import EffectEngine, KEYMAP, EVDEV_CODES  # noqa: E402
from rgbdeck.hw.logitech_hidpp import USAGE_NAMES, _display_order, _slider_to_period  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def fake_keyboard():
    names = ["Key: " + USAGE_NAMES[k] for k in sorted(USAGE_NAMES) if k < 0x66 or k >= 0xE0]
    names = [n for n in names if "(ISO)" not in n and "(JIS)" not in n and "Number Pad =" not in n]
    return Device(0, "Keyboard", "Fake", "Test", "", "", "", "", 0, [Mode(0, "Direct", -1, 0)],
                  [Zone(0, "Keyboard", 2, len(names), len(names), len(names))], names, [0] * len(names))


# --- colors
check(u32_to_rgb(rgb_to_u32(1, 2, 3)) == (1, 2, 3), "rgb <-> u32 round trip")
check(rgb_to_u32(255, 0, 0) == 0x0000FF, "u32 layout is r | g<<8 | b<<16")

# --- keyboard geometry from LED names
kb = fake_keyboard()
g = build_geometry(kb)
placed = {k.led for k in g}
check(len(placed) == len(kb.led_names), f"every LED gets geometry ({len(placed)}/{len(kb.led_names)})")
check(all(k.y < 7 for k in g), "no key falls into the leftover strip for a standard ANSI set")
order = _display_order()
check(len(order) == len(set(order)), "display order has no duplicates")

# --- effects
pos = {k.led: (k.x + k.w / 2, k.y + k.h / 2) for k in g}
name_to_led = {n[5:]: i for i, n in enumerate(kb.led_names)}
eng = EffectEngine(pos, name_to_led, lambda: (255, 0, 255))
eng.background = [rgb_to_u32(0, 40, 0)] * len(kb.led_names)
t0 = time.monotonic()
eng.key_pressed("G")
f = eng.tick(t0 + 0.01)
gi = name_to_led["G"]
check(f is not None and u32_to_rgb(f[gi])[0] > 150, "ripple starts magenta on the pressed key")
check(u32_to_rgb(f[name_to_led["Escape"]]) == (0, 40, 0), "far keys keep the background at t=0")
n = 0
t = t0
while eng.tick(t := t + 1 / 24) is not None:
    n += 1
    if n > 500:
        break
check(10 < n < 200, f"ripple ends by itself ({n} frames)")
check(eng.tick(t + 1) is None and not eng.busy, "engine idle after the ripple")
check(all(KEYMAP[v] for v in ("KEY_A", "KEY_SPACE", "KEY_KP7", "KEY_F12")), "evdev keymap covers common keys")
check(EVDEV_CODES[30] == "KEY_A" and EVDEV_CODES[57] == "KEY_SPACE", "embedded key-code table")

# --- Logitech period mapping
check(_slider_to_period(1, 1000, 20000) == 20000 and _slider_to_period(100, 1000, 20000) == 1000, "speed slider -> period")

# --- color panel keeps hue through brightness 0 (needs Qt)
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor
    import rgbdeck.app as app_mod
    app = QApplication.instance() or QApplication([])
    cp = app_mod.ColorPanel()
    cp.setColor(QColor("#ff6a00"))
    cp.v_slider.setValue(0)
    cp.v_slider.setValue(100)
    h = cp.color().hue()
    check(20 <= h <= 28, f"hue survives brightness 0 -> 100 (hue {h})")
except ImportError as e:
    print("SKIP Qt checks:", e)

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
