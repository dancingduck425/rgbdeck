import os, sys, time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
import rgbdeck.app as rgbdeck
from rgbdeck.backend import LocalBackend
from rgbdeck.models import u32_to_rgb

fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    (not c) and fails.append(m)


def pump(ms):
    l = QEventLoop()
    QTimer.singleShot(ms, l.quit)
    l.exec()


class _View:
    def __init__(self, dev, colors):
        self.dev, self.colors = dev, colors

    def __getattr__(self, k):
        return getattr(self.dev, k)


def fresh():
    c.wait_idle()
    out = {}
    for dv in c.devices:
        cols = c.read_colors(dv) if dv.type_name == "Keyboard" else list(dv.colors)
        if dv.type_name == "Keyboard":
            cols = [x if "Indicator" not in n else dv.colors[i] for i, (x, n) in enumerate(zip(cols, dv.led_names))]
        out[dv.type_name] = _View(dv, cols)
    return out


app = QApplication([])
app.setStyleSheet(rgbdeck.QSS)
c = LocalBackend()
c.connect()
w = rgbdeck.MainWindow(c)
w.resize(1360, 820)
w.show()
pump(300)
kb = next(d for d in c.devices if d.type_name == "Keyboard")
w._select_device(kb.index)
w.sync_chk.setChecked(False)
w.color_panel.setColor(QColor("#102040"), emit=True)
pump(600)
w.color_panel.setColor(QColor("#ff00ff"))  # brush only (no emit)
w._app_effect_picked("ripple")
pump(600)
check(w.engine is not None and w._fx_timer.isActive(), "ripple engine running")
check(w.listener is not None and w.listener.ok, f"evdev listener ok ({w.listener.error if w.listener else 'none'})")
check(all(c0 == 0x402010 for c0 in w.engine.background), "engine background = my colors")
gi = kb.led_names.index("Key: G")
w.engine.key_pressed("G")
pump(45)
s = fresh()
col = u32_to_rgb(s["Keyboard"].colors[gi])
check(col[0] > 90 and col[2] > 90, f"G key got magenta ripple on hardware (got {col})")
check(u32_to_rgb(w.canvas.colors[gi])[0] > 90, "canvas preview shows the ripple")
pump(2500)
s = fresh()
check(all(c0 == 0x402010 for c0 in s["Keyboard"].colors), "board returns to background after ripple")
w.grab().save(sys.argv[1] + "/render_effect.png")
# painting while effect runs updates the background
w._set_interaction("paint")
w._paint_leds([gi])
pump(300)
check(w.engine.background[gi] == 0xFF00FF, "painting a key updates the effect background")
# preset round trip
w.presets["__fx__"] = w._snapshot()
check(w.presets["__fx__"]["app_effect"]["kind"] == "ripple", "snapshot stores the effect")
w._mode_picked(kb.mode_named("Static"))
pump(600)
check(w.engine is None, "picking a hardware effect stops the ripple")
w._apply_preset("__fx__")
pump(600)
check(w.engine is not None and w.engine_kind == "ripple", "applying preset restarts the ripple")
w._stop_app_effect()
w.presets.pop("__fx__")
rgbdeck.save_json(rgbdeck.PRESETS_FILE, w.presets)
w._really_quit = True
w.close()
print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(bool(fails))
