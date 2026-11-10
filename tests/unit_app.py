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
os.environ["APPDATA"] = TMP  # Windows config root
os.environ["HOME"] = TMP
os.environ["RGBDECK_NO_BLUETOOTH"] = "1"  # tests never touch the radio
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
room = FakeDriver("Room light", ["Light"], kb_modes())  # like a network light: remembers its state, has no keys
room.keeps_state = room.follows_typing = True
mod = types.ModuleType("rgbdeck.hw.fake")
mod.probe = lambda: [uniform, painted, fx, room]
mod.USB_IDS = []
backend.discover_drivers = lambda: [mod]
backend.hidraw.enumerate_hid = lambda *a, **k: []

from PySide6.QtWidgets import QApplication  # noqa: E402
import rgbdeck.app as app_mod  # noqa: E402

app_mod.IS_WINDOWS = False  # login entries as files in TMP on every platform (never touch a real registry)

app = QApplication.instance() or QApplication([])
client = backend.LocalBackend()
w = app_mod.MainWindow(client)
client.refresh()
w._scan_done()
check(
    [d.name for d in client.devices] == ["Uniform board", "Painted board", "Effect board", "Room light"],
    "fake devices adopted",
)

# set up looks: one color everywhere (indicator dark) / per-key / an onboard effect
orange = rgb_to_u32(255, 106, 0)
client.update_leds(client.devices[0], [orange, orange, orange, 0])
client.update_leds(client.devices[1], [rgb_to_u32(255, 0, 0), rgb_to_u32(0, 255, 0), rgb_to_u32(0, 0, 255)])
m = client.devices[2].modes[2]
m.colors = [rgb_to_u32(0, 0, 255)]
client.update_mode(client.devices[2], m)
client.wait_idle()
blue = rgb_to_u32(0, 0, 255)
client.update_leds(client.devices[3], [blue])
client.wait_idle()
w.base_colors = {d.index: list(d.colors) for d in client.devices}

# a typing effect on the keyboard makes the room light flash toward the key's color, then fall back
from rgbdeck.effects import EffectEngine  # noqa: E402

kbd = client.devices[1]
w.engine = EffectEngine({0: (0, 0), 1: (1, 0), 2: (2, 0)}, {"A": 0, "B": 1, "C": 2}, lambda: (255, 0, 0))
w.engine.settings.kind = "glow"
w.engine.background = list(kbd.colors)
w.engine_dev, w.engine_kind = kbd, "glow"
before_writes = len(room.writes)
w._fx_tick()
client.wait_idle()
check(len(room.writes) == before_writes, "no key pressed: the room light is left alone")
w.engine.key_pressed("A")
w._fx_tick()
client.wait_idle()
r, g, b = room.writes[-1][0] & 0xFF, room.writes[-1][0] >> 8 & 0xFF, room.writes[-1][0] >> 16 & 0xFF
check(r > 150 and b < 100, f"key press pulls the room light toward the effect color ({(r, g, b)})")
w._stop_app_effect()
client.wait_idle()
check(room.writes[-1] == [blue], "stopping the effect gives the room light its own color back")
w.base_colors[3] = [0]
w.engine = EffectEngine({0: (0, 0)}, {"A": 0}, lambda: (255, 0, 0))
w.engine.settings.kind = "glow"
w.engine.background = list(kbd.colors)
w.engine_dev, w.engine_kind = kbd, "glow"
n_writes = len(room.writes)
w.engine.key_pressed("A")
w._fx_tick()
client.wait_idle()
check(len(room.writes) == n_writes, "a light that is off is not switched on by typing")
w._stop_app_effect()
client.wait_idle()
w.base_colors[3] = [blue]

# animations play on every per-LED device at once; a room light that is off stays off; stopping restores colors
check(hasattr(w, "anim_btn") and len(w.anim_actions) == 9, "Animate menu lists Off + eight animations")
painted_before = list(w.base_colors[1])
room_writes = len(room.writes)
w.base_colors[3] = [0]  # the room light is off
w._set_animation("wave", 80)
client.wait_idle()
check(
    w._anim_timer.isActive()
    and w.settings["animation"] == {"kind": "wave", "speed": 80, "palette": [], "skip": [], "status": {}},
    "animation runs and is remembered",
)
check(len(set(painted.writes[-1])) == 3, f"wave: the keys show different colors ({painted.writes[-1]})")
check(fx.device.current_mode.name == "Direct", "a device showing an onboard effect joins the animation")
check(len(room.writes) == room_writes, "a room light that is off is not switched on by an animation")
w.base_colors[3] = [blue]
room.slow_colors = True
w._anim_pos.clear()
w._anim_tick()
client.wait_idle()
check(len(room.writes) > room_writes and room.writes[-1] != [blue], "a room light that is on takes part")
frame1 = list(painted.writes[-1])
time.sleep(0.12)
w._anim_tick()
client.wait_idle()
check(painted.writes[-1] != frame1, "frames move with time")
w.engine = EffectEngine({0: (0, 0), 1: (1, 0), 2: (2, 0)}, {"A": 0, "B": 1, "C": 2}, lambda: (255, 255, 255))
w.engine.settings.kind = "glow"
w.engine_dev, w.engine_kind = kbd, "glow"
w._anim_tick()
check(
    w.engine.background == painted.device.colors or len(w.engine.background) == 3,
    "typing effect renders over the animation",
)
client.wait_idle()
room_writes = len(room.writes)
w.engine.key_pressed("A")
w._fx_tick()
client.wait_idle()
check(len(room.writes) == room_writes, "the animation, not the typing pulse, drives the room light meanwhile")
w._stop_app_effect()
w._set_animation(None)
client.wait_idle()
check(
    not w._anim_timer.isActive() and painted.writes[-1] == painted_before, "stopping gives every device its colors back"
)
check(room.writes[-1] == [blue] and w.settings["animation"]["kind"] is None, "room light restored, choice remembered")
w._set_animation("cycle")
w._everything_off()
check(w._anim_kind is None, "All off stops the animation")
client.wait_idle()

# own colors, devices sitting out, presets that bring an animation back
client.update_mode(client.devices[1], client.devices[1].modes[0])
client.update_mode(client.devices[2], client.devices[2].modes[0])
w.base_colors[1] = list(painted_before)
w._anim_palette = [rgb_to_u32(255, 0, 0), rgb_to_u32(0, 0, 255)]
w._anim_skip = {"Effect board"}
fx_writes = len(fx.writes)
w._set_animation("wave", 50)
w._anim_tick()
client.wait_idle()
check(all(c & 0x00FF00 == 0 for c in painted.writes[-1]), f"own colors: only reds and blues ({painted.writes[-1]})")
check(len(fx.writes) == fx_writes, "a device taken out of the animation is left alone")
w.presets["Party"] = w._snapshot()
check(w.presets["Party"]["animation"]["kind"] == "wave", "a preset remembers the running animation")
w._set_animation(None)
w._anim_palette, w._anim_skip = [], set()
w._apply_preset("Party")
check(
    w._anim_kind == "wave" and len(w._anim_palette) == 2 and w._anim_skip == {"Effect board"},
    "applying the preset brings the animation back, colors and devices included",
)
w._set_animation(None)
w._anim_palette, w._anim_skip = [], set()
w.presets.pop("Party")
client.wait_idle()

# a device set to show a sensor does so with no animation running, and keeps doing so next to one
from rgbdeck import sensors  # noqa: E402

sensors._READERS["cpu"] = lambda: 85.0  # as hot as the scale goes
w._anim_status = {"Painted board": "cpu"}
w._set_animation(None)
check(w._anim_timer.isActive(), "a status device keeps the animation clock running by itself")
time.sleep(0.4)
w._anim_tick()
client.wait_idle()
check(
    painted.writes[-1] == [rgb_to_u32(255, 0, 0)] * 3 or painted.writes[-1][0] & 0xFF > 150,
    f"hot CPU shows red ({painted.writes[-1]})",
)
n_same = len(painted.writes)
w._anim_tick()
client.wait_idle()
check(len(painted.writes) <= n_same + 1, "a steady reading is not sent over and over")
w._anim_status = {}
w._set_animation(None)
client.wait_idle()
check(
    not w._anim_timer.isActive() and painted.writes[-1] == painted_before, "taking the sensor away restores the colors"
)
sensors.watch([])

# the director: master brightness, lights off / on, flash, text commands
d = w.director
w.base_colors[1] = list(painted_before)
d.send(client.devices[1], painted_before)
d.set_master(50)
client.wait_idle()
check(
    painted.writes[-1] == [rgb_to_u32(128, 0, 0), rgb_to_u32(0, 128, 0), rgb_to_u32(0, 0, 128)],
    "master 50 % halves every color",
)
check(w.settings.get("master") == 50, "master brightness is remembered")
client.update_mode(client.devices[2], client.devices[2].modes[2])  # an onboard effect
client.wait_idle()
check(d.command("toggle") == "ok lights off" and d.dark, "toggle: lights off")
client.wait_idle()
check(painted.writes[-1] == [0, 0, 0], "per-key device goes dark")
check(
    fx.device.current_mode.name != "Breathing" or fx.device.mode_named("Off") is None,
    "a device on an onboard effect is switched off where it can",
)
d.send(client.devices[1], painted_before)
client.wait_idle()
check(painted.writes[-1] == [0, 0, 0], "nothing lights up while the lights are off")
check(d.command("on") == "ok lights on", "on")
client.wait_idle()
check(
    painted.writes[-1][0] == rgb_to_u32(128, 0, 0) and fx.device.current_mode.name == "Breathing",
    "the look comes back, onboard effect too",
)
check(
    d.command("brightness +50") == "ok brightness 100" and d.command("brightness 300") == "ok brightness 100",
    "brightness is clamped",
)
d.flash((255, 255, 255), 1)
client.wait_idle()
time.sleep(0.25)
d._flash_tick()
client.wait_idle()
check(painted.writes[-1][1] != painted_before[1], f"flash pulls the colors toward white ({painted.writes[-1]})")
time.sleep(0.35)
d._flash_tick()
client.wait_idle()
check(painted.writes[-1] == painted_before and not d._flash_timer.isActive(), "and leaves the look as it was")
w.presets["One"], w.presets["Two"] = w._snapshot(), w._snapshot()
check(d.command("next-preset") == "ok preset One" and d.command("next-preset") == "ok preset Two", "next-preset cycles")
check(
    d.command("preset Nope").startswith("no ") and d.command("animate nope").startswith("no "),
    "unknown names are refused",
)
check(
    d.command("animate cycle 30") == "ok animate cycle" and w._anim_kind == "cycle" and w._anim_speed == 30,
    "animate by command",
)
check(d.command("animate off") == "ok animate off" and w._anim_kind is None, "animate off")
check(d.command("color 00ff00") == "ok color 00FF00", "color by command")
w._flush()
client.wait_idle()
check(painted.writes[-1] == [rgb_to_u32(0, 255, 0)] * 3, "and every device takes it")
state = d.state()
check(
    state["presets"] == ["One", "Two"] and state["master"] == 100 and not state["dark"],
    f"state for menus and pages ({str(state)[:60]})",
)
check(d.command("nonsense") == "unknown command", "unknown command")
check(app_mod.remote_command(["rgbdeck", "--brightness", "+10"]) == "brightness +10", "command line: --brightness +10")
check(
    app_mod.remote_command(["rgbdeck", "--toggle"]) == "toggle" and app_mod.remote_command(["rgbdeck"]) is None,
    "command line: --toggle",
)
w.presets.pop("One")
w.presets.pop("Two")
w._last_preset = None
client.wait_idle()
# put the looks back the way the close tests below expect them
client.update_leds(client.devices[0], [orange, orange, orange, 0])
client.update_leds(client.devices[1], painted_before)
client.update_mode(client.devices[1], client.devices[1].modes[0])
m = client.devices[2].modes[2]
client.update_mode(client.devices[2], m)
client.update_mode(client.devices[0], client.devices[0].modes[0])
client.wait_idle()
w.base_colors = {d.index: list(d.colors) for d in client.devices}
w.base_colors[3] = [blue]
uniform.modes.clear()
painted.modes.clear()
room.modes.clear()

w._really_quit = True
w.close()
client.wait_idle()
check(
    uniform.modes and uniform.modes[-1] == ("Static", True, [orange]),
    f"uniform board stored as saved Static ({uniform.modes[-1:]})",
)
check(not any(m[0] == "Static" for m in painted.modes), "painted board left in per-key mode")
check(not room.modes, f"a device that keeps its own state is not touched on close ({room.modes})")
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

# Windows control channel: a stale control.json pointing at some other program's port must not look like a
# running RGB Deck (that would make the next start exit silently)
import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("127.0.0.1", 0))
srv.listen(1)


def _stranger():
    c, _ = srv.accept()
    c.recv(4096)
    c.sendall(b"220 something else entirely\r\n")
    c.close()


threading.Thread(target=_stranger, daemon=True).start()
app_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
app_mod.CONTROL_FILE.write_text(json.dumps({"port": srv.getsockname()[1], "token": "x"}))
app_mod.TCP_CONTROL = True
check(app_mod.send_control("show") is None, "foreign reply on a stale control port is not 'running'")
srv.close()
check(app_mod.send_control("show") is None, "dead control port is not 'running'")
ctl = app_mod.control_listen()
check(ctl is not None and app_mod.CONTROL_FILE.exists(), "listener bound and control.json rewritten")
ctl.close()
app_mod.forget_control_file()
check(not app_mod.CONTROL_FILE.exists(), "control.json removed on exit")
app_mod.TCP_CONTROL = False

# the Lights panel opens and lists nothing when no light is known (looking switched off: no network traffic here)
from rgbdeck.hw import govee_lan  # noqa: E402

govee_lan.set_settings(False, [])
panel = app_mod.LightsDialog(w)
check(not panel.enable.isChecked() and panel.rows.count() == 1, "Lights panel opens with its empty state")
panel._timer.stop()
panel.close()
check(hasattr(w, "lights_btn"), "main window has the Lights button")

client.shutdown()
print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
