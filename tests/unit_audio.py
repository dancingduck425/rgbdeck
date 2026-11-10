"""Audio analysis on synthetic sound, and the sensor readers on a fake /sys tree. No sound card, no hardware."""

import math
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck import audio, sensors  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def tone(freq, amp=12000, n=audio.WINDOW, phase=0):
    return struct.pack(
        f"<{n}h", *(int(amp * math.sin(2 * math.pi * freq * (i + phase) / audio.RATE)) for i in range(n))
    )


for use_numpy in (False, True):
    if use_numpy and audio._np is None:
        print("SKIP numpy path (numpy not installed)")
        continue
    saved = audio._np
    if not use_numpy:
        audio._np = None
    how = "numpy" if use_numpy else "pure Python"
    a = audio.Analyzer()
    for k in range(20):
        a.feed(tone(1000, phase=k * audio.WINDOW), now=k * 0.023)
    lv = a.levels(now=20 * 0.023)
    check(
        lv.active and lv.bands.index(max(lv.bands)) == 4 and lv.bands[4] > 0.8,
        f"{how}: a 1 kHz tone peaks in the 1 kHz band ({[round(x, 2) for x in lv.bands]})",
    )
    check(lv.bands[0] < 0.3 and lv.bands[7] < 0.3, f"{how}: and leaves bass and treble low")
    a = audio.Analyzer()
    for k in range(20):
        a.feed(tone(60, phase=k * audio.WINDOW), now=k * 0.023)
    lv = a.levels(now=20 * 0.023)
    check(lv.bass > 0.8 and lv.bands[5] < 0.3, f"{how}: a 60 Hz tone is bass ({round(lv.bass, 2)})")
    a = audio.Analyzer()
    for k in range(30):
        a.feed(bytes(audio.WINDOW * 2), now=k * 0.023)
    lv = a.levels(now=30 * 0.023)
    check(not lv.active and max(lv.bands) == 0.0, f"{how}: silence is silence")
    a = audio.Analyzer()
    beats = 0
    for k in range(120):  # quiet hum with a kick every 20 windows (about every 0.46 s)
        kick = k % 20 == 0
        a.feed(tone(70, amp=20000 if kick else 600, phase=k * audio.WINDOW), now=k * 0.023)
        if a.levels(now=k * 0.023).beat == 1.0:
            beats += 1
    check(4 <= beats <= 7, f"{how}: kicks are counted as beats ({beats} of 6)")
    audio._np = saved

a = audio.Analyzer()
a.feed(tone(1000), now=0.0)
check(not a.levels(now=5.0).active, "a stalled capture reads as silence")
ok, why = audio.available()
check(ok or bool(why), "availability comes with a reason when it is missing")

# sensors: a fake hwmon tree
root = tempfile.mkdtemp(prefix="rgbdeck-hwmon-")
for i, (name, temps) in enumerate([("nvme", [30000]), ("coretemp", [41000, 55500, 48000]), ("amdgpu", [62000])]):
    d = os.path.join(root, f"hwmon{i}")
    os.makedirs(d)
    open(os.path.join(d, "name"), "w").write(name + "\n")
    for j, t in enumerate(temps):
        open(os.path.join(d, f"temp{j + 1}_input"), "w").write(f"{t}\n")
if sys.platform != "win32":
    check(sensors.cpu_temp(root) == 55.5, f"CPU temperature = hottest core ({sensors.cpu_temp(root)})")
    check(sensors._hwmon_temp(sensors.GPU_CHIPS, root) == 62.0, "GPU temperature from an amdgpu entry")
    check(sensors._hwmon_temp(("nothere",), root) is None, "no matching chip, no reading")
load = sensors._Load()
first = load.read()
second = load.read()
check(first is None and (second is None or 0.0 <= second <= 100.0), f"CPU load needs two readings ({second})")
sensors._READERS["cpu"] = lambda: 62.5
sensors.watch(["cpu"])
import time  # noqa: E402

time.sleep(0.3)
check(
    sensors.value("cpu") == 62.5 and abs(sensors.level("cpu") - 0.5) < 0.01,
    "watched sources are read; level is 0..1 between cool and hot",
)
sensors.watch([])
check("load" in sensors.offered(), "CPU load is offered everywhere")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
