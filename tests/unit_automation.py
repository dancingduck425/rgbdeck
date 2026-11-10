"""The automation rules without a desktop: schedule, notification parsing and colors, per-app profiles."""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.automation import NotifyParser, Profiles, Schedule, notify_color, parse_rule  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


check(parse_rule({"at": "22:00", "do": "dim 30"}) == (dt.time(22, 0), "brightness 30"), "dim rule")
check(parse_rule({"at": "7:05", "do": "preset Morning sun"}) == (dt.time(7, 5), "preset Morning sun"), "preset rule")
check(parse_rule({"at": "23:30", "do": "off"}) == (dt.time(23, 30), "off"), "off rule")
check(
    parse_rule({"at": "25:00", "do": "off"}) is None and parse_rule({"at": "x", "do": "off"}) is None,
    "bad times are refused",
)
check(
    parse_rule({"at": "10:00", "do": "explode"}) is None and parse_rule({"at": "10:00", "do": "dim"}) is None,
    "bad actions are refused",
)
check(parse_rule({"at": "10:00", "do": "dim 500"}) == (dt.time(10, 0), "brightness 100"), "percent is clamped")

s = Schedule([{"at": "22:00", "do": "dim 30"}, {"at": "07:00", "do": "dim 100"}, {"at": "nope", "do": "off"}])
day = dt.datetime(2026, 11, 10)
check(
    s.due(day.replace(hour=21, minute=59, second=50), day.replace(hour=22, minute=0, second=10)) == ["brightness 30"],
    "fires when its minute is crossed",
)
check(
    s.due(day.replace(hour=22, minute=0, second=10), day.replace(hour=22, minute=0, second=30)) == [], "and only once"
)
check(
    s.due(day.replace(hour=23, minute=59), day.replace(hour=23, minute=59) + dt.timedelta(hours=8))
    == ["brightness 100"],
    "across midnight",
)
check(s.due(day, day + dt.timedelta(days=3)) == [], "a clock jump fires nothing late")
check(s.due(day.replace(hour=12), day.replace(hour=11)) == [], "time going backwards fires nothing")

p = NotifyParser()
lines = """signal time=1 sender=org.freedesktop.DBus -> destination=:1.203 member=NameAcquired
   string ":1.203"
method call time=2 sender=:1.205 -> destination=:1.14 serial=9 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "Discord"
   uint32 0
   string ""
   string "New message"
method call time=3 sender=:1.9 -> destination=:1.14 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "say \\"hi\\""
""".splitlines()
got = [a for a in (p.feed(line) for line in lines) if a is not None]
check(got == ["Discord", 'say \\"hi\\"'], f"program names read from the monitor's output ({got})")
cfg = {"on": True, "color": 0xFFFFFF, "apps": {"discord": 0x5865F2}, "mute": ["spotify"]}
check(
    notify_color(cfg, "Discord") == 0x5865F2 and notify_color(cfg, "Thunderbird") == 0xFFFFFF,
    "own color, else the default",
)
check(
    notify_color(cfg, "Spotify") is None
    and notify_color({"on": False}, "Discord") is None
    and notify_color({}, "x") is None,
    "muted / switched off",
)

pr = Profiles(
    [{"app": "steam_app", "preset": "Game"}, {"app": "blender", "preset": "Work"}, {"app": "", "preset": "x"}]
)
check(pr.on_focus("org.kde.konsole") is None, "an ordinary program: nothing happens")
check(pr.on_focus("steam_app_1234") == ("apply", "Game"), "a matching program applies its preset")
check(
    pr.on_focus("steam_app_1234") is None and pr.on_focus("") is None,
    "staying there (or focusing the desktop) changes nothing",
)
check(pr.on_focus("Blender") == ("apply", "Work"), "straight to another rule")
check(pr.on_focus("firefox") == ("restore", None), "leaving restores")
check(pr.recent[:2] == ["firefox", "Blender"] and len(pr.recent) == 4, f"recent programs are remembered ({pr.recent})")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
