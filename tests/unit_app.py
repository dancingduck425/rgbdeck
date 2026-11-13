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
os.environ["RGBDECK_NO_BLUETOOTH"] = "1"
os.environ["RGBDECK_NO_UPDATE_CHECK"] = "1"  # never ask GitHub from a test
os.environ["RGBDECK_NO_DESKTOP_HOOKS"] = "1"  # tests never touch the radio
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
check(
    fx.device.current_mode.name == "Breathing",
    f"a device that showed an onboard effect gets it back ({fx.device.current_mode.name})",
)
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
# updates: the dialog appears for a newer release; Later and Skip are remembered; Update restarts
from rgbdeck import updater  # noqa: E402

rel = updater.Release(
    "9.9.9", "v9.9.9", "## 9.9.9\n\n- **Shiny** new thing\n- A fix", "https://example.invalid/r", "", {}
)
updater.install_kind = lambda *a, **k: "git"
said = []
app_mod.QMessageBox.information = staticmethod(lambda *a, **k: said.append(a[2]))
app_mod.QMessageBox.warning = staticmethod(lambda *a, **k: said.append(a[2]))
w._update_checked(None, "", True)
check(said and "newest version" in said[-1], "asked by hand and up to date: says so")
w._update_checked(None, "no network", True)
check("no network" in said[-1], "asked by hand and offline: says why")
n_said = len(said)
w._update_checked(None, "no network", False)
check(len(said) == n_said and w._update_dialog is None, "the daily check fails quietly")
w._update_checked(rel, "", False)
dlg = w._update_dialog
check(
    dlg is not None and dlg.isVisible() and "9.9.9" in dlg.windowTitle() + dlg.findChild(app_mod.QLabel).text(),
    "a newer release opens the update dialog",
)
check(
    "Shiny" in dlg.notes.toPlainText() and dlg.update_btn.text() == "Update now",
    "with what is new and an Update now button",
)
dlg.later_btn.click()
check(
    w._update_dialog is None and w.settings["updates"]["later_until"] > time.time() + 3600, "Later: asks again tomorrow"
)
w._update_checked(rel, "", False)
check(w._update_dialog is None, "and not before")
w._update_checked(rel, "", True)
check(w._update_dialog is not None, "unless asked by hand")
w._update_dialog.skip_btn.click()
w.settings["updates"]["later_until"] = 0
w._update_checked(rel, "", False)
check(w.settings["updates"]["skip"] == "9.9.9" and w._update_dialog is None, "Skip this version: not offered again")
newer = updater.Release("10.0.0", "v10.0.0", "- more", "https://example.invalid/r", "", {})
w._update_checked(newer, "", False)
check(w._update_dialog is not None, "but the version after it is")
dlg = w._update_dialog
updater.apply = lambda release, kind, progress=None, relaunch=None: (_ for _ in ()).throw(
    updater.UpdateError("disk full.")
)
dlg._update_now()
for _ in range(100):
    app.processEvents()
    time.sleep(0.01)
    if not dlg._busy:
        break
check(
    "disk full" in dlg.status.text() and dlg.update_btn.isEnabled() and dlg.update_btn.text() == "Try again",
    "a failed update is explained and can be retried",
)
quits = []
w._quit = lambda: quits.append(1)
updater.apply = lambda release, kind, progress=None, relaunch=None: (progress("Downloading", 50), ["relaunch-me"])[1]
dlg._update_now()
for _ in range(300):
    app.processEvents()
    time.sleep(0.01)
    if quits:
        break
check(
    quits == [1] and app_mod.RESTART_COMMAND == ["relaunch-me"],
    "a finished update closes the app and hands over the restart command",
)
app_mod.RESTART_COMMAND = []
del w._quit
asked = []
w._check_updates = lambda manual=True: asked.append(manual)
del os.environ["RGBDECK_NO_UPDATE_CHECK"]
w.settings["updates"] = {"auto": True, "last_check": time.time()}  # checked a moment ago
w._startup_update_check()
check(asked == [False], "every start asks, however recent the last check")
w._maybe_check_updates()
check(asked == [False], "while running, only once a day")
w.settings["updates"]["last_check"] = time.time() - 30 * 3600
w._maybe_check_updates()
check(asked == [False, False], "a day later the running app asks again")
w.settings["updates"]["auto"] = False
w._startup_update_check()
w._maybe_check_updates()
check(asked == [False, False], "switched off in Settings: never")
os.environ["RGBDECK_NO_UPDATE_CHECK"] = "1"
del w._check_updates
managed = app_mod.UpdateDialog(w, rel, "managed")
check(
    managed.update_btn.text() == "Open the download page" and "package manager" in managed.info.text(),
    "a package-managed install is pointed to the download instead",
)
managed.close()
w.settings.pop("updates", None)

# automations: a per-app look takes over and hands back; away switches the lights off; a notification flashes
auto = w.automation
auto.configure(
    {
        "away": True,
        "notify": {"on": True, "color": 0xFFFFFF, "apps": {"discord": 0x0000FF}, "mute": ["spotify"]},
        "profiles": [{"app": "game", "preset": "Two"}],
        "schedule": [{"at": "22:00", "do": "dim 30"}],
    }
)
w._flush()
client.wait_idle()
green = [rgb_to_u32(0, 255, 0)] * 3
w.presets["Two"]["devices"][1]["colors"] = [rgb_to_u32(9, 9, 9)] * 3
auto.focus_changed("steam_app_game_123")
client.wait_idle()
check(painted.writes[-1] == [rgb_to_u32(9, 9, 9)] * 3, f"program in front: its preset shows ({painted.writes[-1]})")
auto.focus_changed("org.kde.dolphin")
client.wait_idle()
check(painted.writes[-1] == green, f"leaving it brings the previous look back ({painted.writes[-1]})")
auto.away_changed(True)
client.wait_idle()
check(w.director.dark and painted.writes[-1] == [0, 0, 0], "screen locked: lights off")
auto.away_changed(False)
client.wait_idle()
check(not w.director.dark and painted.writes[-1] == green, "unlocked: the look is back")
auto._on_notification("Spotify")
check(w.director._flash is None, "a muted program does not flash")
auto._on_notification("Discord")
check(
    w.director._flash is not None and w.director._flash[2] == (0, 0, 255),
    "a notification flashes in its program's color",
)
w.director._flash = None
w.director._flash_timer.stop()
w.director.resend()
import datetime as dt  # noqa: E402

auto._last_tick = dt.datetime.now().replace(hour=21, minute=59, second=0)
auto.schedule.due  # noqa: B018
for cmd in auto.schedule.due(auto._last_tick, auto._last_tick.replace(hour=22, minute=0, second=30)):
    w.director.command(cmd)
check(w.director.master == 30, "22:00 came: dimmed to 30 %")
w.director.set_master(100)
w.settings["automation"] = dict(auto.cfg)
dlg = app_mod.AutomationsDialog(w)
cfg_out = dlg.result_config()
check(
    cfg_out["schedule"] == [{"at": "22:00", "do": "dim 30"}]
    and cfg_out["notify"]["apps"] == {"discord": 0x0000FF}
    and cfg_out["profiles"] == [{"app": "game", "preset": "Two"}]
    and cfg_out["notify"]["mute"] == ["spotify"],
    f"Automations dialog round-trips the settings ({str(cfg_out)[:70]})",
)
dlg.close()
w.settings.pop("automation")
auto.configure({})
client.wait_idle()
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

# ---- what a real Windows PC showed: closing before the scan had finished, and devices never written to ------
# (its log: the scan did not come to an end, the window was closed, and the mainboard was stored as black)
app_mod.set_autostart("apply", None)


def fresh_backend(first, late):
    usb = types.ModuleType("rgbdeck.hw.fakeusb")
    usb.probe = lambda: first
    usb.USB_IDS = [(1, 2)]
    net = types.ModuleType("rgbdeck.hw.fakenet")
    net.probe = late
    net.USB_IDS = []
    backend.discover_drivers = lambda: [usb, net]
    return backend.LocalBackend()


board = FakeDriver("Mainboard", ["Light"], kb_modes())  # cannot be read back: starts out as "black"
keys = FakeDriver("Keys", ["Key: A", "Key: B"], kb_modes())
lamp = FakeDriver("Lamp", ["Light"], kb_modes())
import threading  # noqa: E402

gate = threading.Event()


def slow_lights():
    gate.wait(10)
    return [lamp]


c3 = fresh_backend([keys, board], slow_lights)
w3 = app_mod.MainWindow(c3)
w3.settings.pop("autostart_preset", None)
w3.settings["keep_on_close"] = True
w3.presets[app_mod.LAST_LOOK] = {
    "devices": [{"name": "Mainboard", "colors": [orange], "active_mode": 0, "modes": {}, "dim": 100}]
}
w3.start_scan()
deadline = time.monotonic() + 5
while w3._scan_shown is None and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
check(
    [t.dev.name for t in w3.tabs] == ["Keys", "Mainboard"] and w3._scanning,
    f"USB devices are on screen while the lights are still being looked for ({[t.dev.name for t in w3.tabs]})",
)
w3.dev = c3.devices[0]
c3.update_leds(c3.devices[0], [blue, blue])  # the user recolors the keyboard meanwhile; the board is not touched
check(c3.wait_idle(2.0) and keys.writes[-1] == [blue, blue], "... and can be used at once")
w3.base_colors[0] = [blue, blue]
w3.start_scan()  # asked for again while it runs (a light was added): remembered, not dropped
gate.set()
deadline = time.monotonic() + 5
while w3._scanning and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
check([t.dev.name for t in w3.tabs] == ["Keys", "Mainboard", "Lamp"], "the lights join when they have answered")
check(w3.base_colors[0] == [blue, blue], "what was set in between is still the look")
deadline = time.monotonic() + 5
again = False
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
    again = again or w3._scanning
    if again and not w3._scanning:
        break
check(again and not w3._scanning, "a scan asked for during a scan runs afterwards")
check(w3.base_colors[0] == [blue, blue], "a rescan keeps the look instead of taking the last frame for it")
board.modes.clear()
keys.modes.clear()
w3._really_quit = True
w3.close()
c3.wait_idle()
check(not board.modes, f"a device nothing was sent to is not stored on close ({board.modes})")
check(keys.modes and keys.modes[-1] == ("Static", True, [blue]), f"the one that was set is ({keys.modes[-1:]})")
kept = {e["name"]: e for e in app_mod.load_json(app_mod.PRESETS_FILE, {}).get(app_mod.LAST_LOOK, {}).get("devices", [])}
check(
    kept.get("Mainboard", {}).get("colors") == [orange] and kept.get("Keys", {}).get("colors") == [blue, blue],
    f"'Last look' keeps the untouched device's earlier look ({kept.get('Mainboard', {}).get('colors')})",
)
c3.shutdown()

# closed while the very first scan is still running: nothing is on screen, so nothing is stored anywhere
gate.clear()
board2 = FakeDriver("Mainboard", ["Light"], kb_modes())
hold = threading.Event()


def slow_net():
    hold.wait(10)
    return []


c4 = fresh_backend([board2], slow_net)
w4 = app_mod.MainWindow(c4)
w4.settings["keep_on_close"] = True
look_before = app_mod.load_json(app_mod.PRESETS_FILE, {}).get(app_mod.LAST_LOOK)
w4.start_scan()
deadline = time.monotonic() + 3
while not c4.devices and time.monotonic() < deadline:
    time.sleep(0.01)  # found by the scan, but the window has not been told yet
check(len(c4.devices) == 1 and w4._scan_shown is None, "a device is found but not on screen yet")
w4._really_quit = True
t0 = time.monotonic()
w4.close()
c4.shutdown()
check(time.monotonic() - t0 < 8.0, f"closing during a scan does not wait for the scan ({time.monotonic() - t0:.1f} s)")
hold.set()
check(not board2.modes, "closing before any device was shown writes nothing to the devices")
check(
    app_mod.load_json(app_mod.PRESETS_FILE, {}).get(app_mod.LAST_LOOK) == look_before,
    "... and leaves 'Last look' alone",
)
backend.discover_drivers = lambda: [mod]

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
