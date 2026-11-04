"""Drive MainWindow offscreen against the real devices (standalone backend) and check hardware state."""

import os
import sys
import json
import subprocess
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import rgbdeck.app as rgbdeck  # noqa: E402
from rgbdeck.backend import LocalBackend  # noqa: E402
from rgbdeck.models import MODE_COLORS_PER_LED, u32_to_rgb  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def pump(ms=250):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class _View:
    def __init__(self, dev, colors):
        self.dev, self.colors = dev, colors

    def __getattr__(self, k):
        return getattr(self.dev, k)


def fresh():
    client.wait_idle()
    out = {}
    for dv in client.devices:
        cols = client.read_colors(dv) if dv.type_name == "Keyboard" else list(dv.colors)
        if dv.type_name == "Keyboard":
            # indicator LEDs are not readable; take them from state
            cols = [c if "Indicator" not in n else dv.colors[i] for i, (c, n) in enumerate(zip(cols, dv.led_names))]
        out[dv.type_name] = _View(dv, cols)
    return out


app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(rgbdeck.QSS)
client = LocalBackend()
client.connect()
w = rgbdeck.MainWindow(client)
w.resize(1360, 820)
w.show()
pump(300)

kb = next(d for d in client.devices if d.type_name == "Keyboard")
mouse = next(d for d in client.devices if d.type_name == "Mouse")
w._select_device(kb.index)
w.sync_chk.setChecked(True)
w._set_interaction("device")
pump()

# 1. palette-style click: whole device + sync
w.color_panel.setColor(QColor("#ff6a00"), emit=True)
pump(200)
s = fresh()
check(all(c == 0x006AFF for c in s["Keyboard"].colors), "sync color: every keyboard key is #FF6A00")
check(s["Keyboard"].current_mode.color_mode == MODE_COLORS_PER_LED, "keyboard switched to Direct")
mm = s["Mouse"].current_mode
check(
    mm.name == "Static" and mm.colors and mm.colors[0] == 0x006AFF,
    f"mouse switched to Static #FF6A00 (got {mm.name} {[hex(c) for c in mm.colors]})",
)
check(
    s["Motherboard"].colors == [0x006AFF] or s["Motherboard"].current_mode.colors == [0x006AFF],
    "motherboard took #FF6A00",
)

# 2. paint keys
w._set_interaction("paint")
w.color_panel.setColor(QColor("#00ff00"), emit=True)  # only sets brush in paint mode
pump(200)
s = fresh()
check(all(c == 0x006AFF for c in s["Keyboard"].colors), "paint mode: changing color does not repaint the board")
wasd = [i for i, n in enumerate(kb.led_names) if n in ("Key: W", "Key: A", "Key: S", "Key: D")]
w._paint_leds(wasd)
pump(200)
s = fresh()
check(all(s["Keyboard"].colors[i] == 0x00FF00 for i in wasd), "WASD painted green")
check(sum(1 for c in s["Keyboard"].colors if c == 0x006AFF) == len(kb.colors) - 4, "other keys untouched")
w.grab().save(str(OUT / "render_painted.png"))

# 3. select + live color
w._set_interaction("select")
w.canvas.selected = set(wasd[:2])
w._active_color_changed(QColor("#0000ff"), True)
pump(200)
s = fresh()
check(all(s["Keyboard"].colors[i] == 0xFF0000 for i in wasd[:2]), "selected keys follow the wheel live")
check(all(s["Keyboard"].colors[i] == 0x00FF00 for i in wasd[2:]), "unselected keys keep their color")
check(s["Mouse"].current_mode.colors[0] == 0x006AFF, "select mode does not touch the mouse")
w.grab().save(str(OUT / "render_selected.png"))

# 4. rainbow / gradient / dim on whole board
w.canvas.clear_selection()
w._rainbow("h")
pump(200)
s = fresh()
check(len(set(s["Keyboard"].colors)) > 20, "rainbow produced many colors")
w.grab().save(str(OUT / "render_rainbow.png"))
w.grad_a.setColor(QColor("#ff0000"))
w.grad_b.setColor(QColor("#0000ff"))
w._gradient("v")
pump(200)
s = fresh()
esc = kb.led_names.index("Key: Escape")
spc = kb.led_names.index("Key: Space")
check(
    u32_to_rgb(s["Keyboard"].colors[esc])[0] > 200 and u32_to_rgb(s["Keyboard"].colors[spc])[2] > 200,
    "vertical gradient red top -> blue bottom",
)
w.dim_slider.setValue(50)
pump(200)
s = fresh()
check(max(u32_to_rgb(s["Keyboard"].colors[esc])) in range(120, 136), "dim 50% halves brightness")
w.dim_slider.setValue(100)
pump(200)

# 5. effects
breathing = kb.mode_named("Breathing")
w._mode_picked(breathing)
pump(200)
s = fresh()
check(s["Keyboard"].current_mode.name == "Breathing", "keyboard effect -> Breathing")
check(s["Keyboard"].current_mode.colors[0] != 0, "breathing got a non-black color automatically")
w.effects.speed.setValue(25)
w.effects._param_changed()
pump(200)
s = fresh()
check(s["Keyboard"].current_mode.speed == 25, "speed slider applied")
w._effect_everywhere()
pump(200)
s = fresh()
check(s["Mouse"].current_mode.name == "Breathing", "same effect on all devices -> mouse Breathing")

# 6. mouse view render
w._select_device(mouse.index)
pump(200)
w.grab().save(str(OUT / "render_mouse.png"))
w.color_panel.setColor(QColor("#ff00ff"), emit=True)
pump(200)
s = fresh()
check(s["Mouse"].current_mode.colors[0] == 0xFF00FF, "mouse: color click recolors its current effect")

# 7. presets
w._select_device(kb.index)
w._mode_picked(kb.mode_named("Direct"))
w._rainbow("h")
pump(200)
snap = w._snapshot()
w.presets["__test__"] = snap
rgbdeck.save_json(rgbdeck.PRESETS_FILE, w.presets)
w._everything_off()
pump(200)
s = fresh()
check(s["Keyboard"].current_mode.name == "Off" and s["Mouse"].current_mode.name == "Off", "All off")
w._apply_preset("__test__")
pump(300)
s = fresh()
check(s["Keyboard"].colors == snap["devices"][kb.index]["colors"], "preset restores keyboard per-key colors")
check(s["Mouse"].active_mode == snap["devices"][mouse.index]["active_mode"], "preset restores mouse mode")

# 8. CLI --apply
w._everything_off()
pump(200)
client.wait_idle()
r = subprocess.run(
    [sys.executable, "-m", "rgbdeck", "--apply", "__test__"],
    capture_output=True,
    text=True,
    cwd=str(Path(__file__).resolve().parents[1]),
)
pump(400)
check(r.returncode == 0, f"--apply exits 0 ({r.stderr.strip()} {r.stdout.strip()})")
s = fresh()
check(s["Keyboard"].colors == snap["devices"][kb.index]["colors"], "--apply restored keyboard colors")

w.presets.pop("__test__", None)
rgbdeck.save_json(rgbdeck.PRESETS_FILE, w.presets)
w.close()
print("\nFAILURES:" if fails else "\nALL PASSED", *fails, sep="\n  ")
sys.exit(1 if fails else 0)
