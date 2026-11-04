"""Hardware-free checks of MainWindow behaviour with fake drivers: keep-look-on-close (static stored in the device,
per-key layouts re-applied at login via the 'Last look' preset), single instance, settings round trip."""

import os
import sys
import tempfile
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = tempfile.mkdtemp(prefix="rgbdeck-unit-")
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["HOME"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rgbdeck.backend as backend  # noqa: E402
from rgbdeck.models import (  # noqa: E402
    Device,
    Mode,
    Zone,
    MODE_COLORS_PER_LED,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    rgb_to_u32,
)

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeDriver:
    def __init__(self, name, led_names, modes):
        n = len(led_names)
        self.device = Device(
            0,
            "Keyboard",
            name,
            "Fake",
            "",
            "",
            "",
            f"fake:{name}",
            0,
            modes,
            [Zone(0, "Z", 2, n, n, n)],
            led_names,
            [0] * n,
        )
        self.writes, self.modes = [], []

    def set_colors(self, colors):
        self.writes.append(list(colors))
        self.device.colors = list(colors)

    def set_mode(self, mode, save=False):
        self.modes.append((mode.name, save, list(mode.colors)))
        self.device.active_mode = mode.index

    def close(self):
        pass


def kb_modes():
    return [
        Mode(0, "Direct", -1, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
        Mode(
            1,
            "Static",
            1,
            MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
            colors_min=1,
            colors_max=1,
            color_mode=MODE_COLORS_MODE_SPECIFIC,
        ),
        Mode(
            2,
            "Breathing",
            2,
            MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
            colors_min=1,
            colors_max=1,
            color_mode=MODE_COLORS_MODE_SPECIFIC,
        ),
    ]


uniform = FakeDriver("Uniform board", ["Key: A", "Key: B", "Key: C", "Indicator: Caps Lock"], kb_modes())
painted = FakeDriver("Painted board", ["Key: A", "Key: B", "Key: C"], kb_modes())
fx = FakeDriver("Effect board", ["Key: A", "Key: B"], kb_modes())
mod = types.ModuleType("rgbdeck.hw.fake")
mod.probe = lambda: [uniform, painted, fx]
mod.USB_IDS = []
backend.discover_drivers = lambda: [mod]
backend.hidraw.enumerate_hid = lambda *a, **k: []

from PySide6.QtWidgets import QApplication  # noqa: E402
import rgbdeck.app as app_mod  # noqa: E402

app = QApplication.instance() or QApplication([])
client = backend.LocalBackend()
w = app_mod.MainWindow(client)
client.refresh()
w._scan_done()
check([d.name for d in client.devices] == ["Uniform board", "Painted board", "Effect board"], "fake devices adopted")

# set up looks: one color everywhere (indicator dark) / per-key / an onboard effect
orange = rgb_to_u32(255, 106, 0)
client.update_leds(client.devices[0], [orange, orange, orange, 0])
client.update_leds(client.devices[1], [rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255)])
m = client.devices[2].modes[2]
m.colors = [rgb_to_u32(0, 0, 255)]
client.update_mode(client.devices[2], m)
client.wait_idle()
w.base_colors = {d.index: list(d.colors) for d in client.devices}

w._really_quit = True
w.close()
client.wait_idle()
check(
    uniform.modes and uniform.modes[-1] == ("Static", True, [orange]),
    f"uniform board stored as saved Static ({uniform.modes[-1:]})",
)
check(not any(m[0] == "Static" for m in painted.modes), "painted board left in per-key mode")
check(fx.modes[-1][:2] == ("Breathing", True), f"running effect re-sent with save=True ({fx.modes[-1:]})")
presets = app_mod.load_json(app_mod.PRESETS_FILE, {})
check(app_mod.LAST_LOOK in presets, "'Last look' preset written")
snap = presets.get(app_mod.LAST_LOOK, {})
pb = next((e for e in snap.get("devices", []) if e["name"] == "Painted board"), {})
check(
    pb.get("colors") == [rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255)],
    "preset keeps the per-key colors",
)
check(
    app_mod.autostart_enabled("apply")
    and app_mod.load_json(app_mod.SETTINGS_FILE, {}).get("autostart_preset") == app_mod.LAST_LOOK,
    "login entry points at 'Last look'",
)
exec_line = app_mod.AUTOSTART_FILE.read_text()
check('--apply "Last look"' in exec_line, f"desktop entry quotes the preset name ({exec_line.splitlines()[-4]})")

# keep_on_close off: nothing is written
w2 = app_mod.MainWindow(client)
w2.settings["keep_on_close"] = False
w2.settings.pop("autostart_preset", None)
app_mod.set_autostart("apply", None)
client.refresh()
w2._scan_done()
before = len(uniform.modes)
w2._really_quit = True
w2.close()
client.wait_idle()
check(
    len(uniform.modes) == before and not app_mod.autostart_enabled("apply"),
    "setting off -> devices and login entry untouched",
)

client.shutdown()
print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
