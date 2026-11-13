# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""Hardware-free checks for the backend worker: coalescing, ordering, errors, keepalive, hotplug state carry-over."""

import os
import sys
import time
import types

os.environ["RGBDECK_NO_BLUETOOTH"] = "1"
os.environ["RGBDECK_NO_DESKTOP_HOOKS"] = "1"  # tests never touch the radio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rgbdeck.backend as backend  # noqa: E402
from rgbdeck.models import Device, DeviceError, Mode, Zone, MODE_COLORS_PER_LED, MODE_FLAG_HAS_PER_LED_COLOR  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeDriver:
    def __init__(self, name, leds=4, slow=0.0, fail=False, keepalive=False):
        modes = [
            Mode(0, "Direct", -1, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            Mode(1, "Static", 1, 0),
        ]
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
            [Zone(0, "Z", 2, leds, leds, leds)],
            [f"LED {i}" for i in range(leds)],
            [0] * leds,
        )
        self.writes, self.modes, self.pings = [], [], 0
        self.slow, self.fail = slow, fail
        if keepalive:
            self.keepalive = self._keepalive
            self.keepalive_interval = 0.05

    def set_colors(self, colors):
        if self.fail:
            raise DeviceError("boom")
        time.sleep(self.slow)
        self.writes.append(list(colors))
        self.device.colors = list(colors)

    def set_mode(self, mode, save=False):
        self.modes.append((mode.name, save))
        self.device.active_mode = mode.index

    def _keepalive(self):
        self.pings += 1

    def close(self):
        pass


def fake_module(drivers):
    m = types.ModuleType("rgbdeck.hw.fake")
    m.probe = lambda: drivers
    m.USB_IDS = [(0x1234, 0x0001)]
    return m


drivers = [FakeDriver("A", slow=0.02), FakeDriver("B", keepalive=True), FakeDriver("C", fail=True)]
backend.discover_drivers = lambda: [fake_module(drivers)]
backend.hidraw.enumerate_hid = lambda *a, **k: []
backend.KEEPALIVE_INTERVAL = 0.05

b = backend.LocalBackend()
b.connect()
check([d.name for d in b.devices] == ["A", "B", "C"], "all fake devices adopted")

# coalescing: 50 rapid frames -> far fewer hardware writes, last one wins
A = b.devices[0]
for i in range(50):
    b.update_leds(A, [i] * 4)
b.wait_idle()
check(drivers[0].writes and drivers[0].writes[-1] == [49] * 4, "last frame wins")
check(len(drivers[0].writes) < 50, f"frames coalesced ({len(drivers[0].writes)} writes for 50 updates)")

# mode + leds ordering per device: mode enqueued first is executed first
Bdev = b.devices[1]
b.update_mode(Bdev, Bdev.modes[1])
b.update_leds(Bdev, [7] * 4)
b.wait_idle()
check(drivers[1].modes == [("Static", False)] and drivers[1].writes == [[7] * 4], "mode then leds, both delivered")

# errors are captured, not raised, and do not kill the worker
b.update_leds(b.devices[2], [1] * 4)
b.wait_idle()
err = b.take_error()
check("boom" in err, f"driver error surfaced ({err})")
b.update_leds(A, [5] * 4)
b.wait_idle()
check(drivers[0].writes[-1] == [5] * 4, "worker alive after a driver error")

# keepalive fires while idle for drivers that offer it
time.sleep(0.3)
check(
    drivers[1].pings >= 2 and not hasattr(drivers[0], "pings") or drivers[0].pings == 0,
    f"keepalive called {drivers[1].pings}x on the driver that has it",
)

# hotplug refresh keeps per-device state for devices still present
A.active_mode = 1
drivers2 = [FakeDriver("A"), FakeDriver("B", keepalive=True)]
backend.discover_drivers = lambda: [fake_module(drivers2)]
b.refresh()
check([d.name for d in b.devices] == ["A", "B"], "refresh drops the vanished device")
check(b.devices[0].colors == [5] * 4 and b.devices[0].active_mode == 1, "colors and mode carried over on refresh")
check(b.poll_notifications() is False, "no hotplug change reported when nothing changed")
b.shutdown()

# ---- a scan that one driver holds up ---------------------------------------------------------------------
# (seen on a real Windows PC: the scan never came to an end, so not one device was shown and closing the
# window left the program running)
import logging  # noqa: E402
import threading  # noqa: E402


class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


grab = Grab()
backend.log.addHandler(grab)
backend.PROBE_LIMIT = 0.4
backend.LATE_PROBE_LIMIT = 0.4
release = threading.Event()
usb = [FakeDriver("Keys"), FakeDriver("Mouse")]
light = FakeDriver("Lamp")
closed = []
light.close = lambda: closed.append("Lamp")


def named(name, probe, usb_ids):
    m = types.ModuleType(f"rgbdeck.hw.{name}")
    m.probe = probe
    m.USB_IDS = usb_ids
    return m


def hanging_probe():
    release.wait(10)
    return [light]


hang = named("hanging", hanging_probe, [])
after = FakeDriver("Strip")
backend.discover_drivers = lambda: [named("usb", lambda: usb, [(1, 2)]), hang, named("net", lambda: [after], [])]
b = backend.LocalBackend()
seen_at_progress = []
t0 = time.monotonic()
b.refresh(progress=lambda: seen_at_progress.append([d.name for d in b.devices]))
took = time.monotonic() - t0
check(
    seen_at_progress == [["Keys", "Mouse"]], f"USB devices are handed over before the slow drivers ({seen_at_progress})"
)
check(
    [d.name for d in b.devices] == ["Keys", "Mouse", "Strip"] and took < 2.0,
    f"a driver that hangs costs its time limit, not the scan ({took:.1f} s, {[d.name for d in b.devices]})",
)
check("hanging did not answer in time" in b.last_error, f"the scan names the driver ({b.last_error})")
check(
    any("hanging_probe" in ln and "release.wait" in ln for ln in grab.lines),
    "the log shows where the driver is stuck",
)
b.update_leds(b.devices[0], [9] * 4)
check(b.wait_idle(2.0) and usb[0].writes[-1] == [9] * 4, "devices work while that driver is still stuck")
check(b.was_written(b.devices[0]) and not b.was_written(b.devices[1]), "knows which devices this session wrote to")
t0 = time.monotonic()
b.refresh()
check(
    time.monotonic() - t0 < 0.3 and "has not come back" in b.last_error,
    f"the next scan does not wait for it again ({b.last_error})",
)
check(b.poll_notifications() is False, "nothing new while it is stuck")
release.set()
deadline = time.monotonic() + 3
while not closed and time.monotonic() < deadline:
    time.sleep(0.02)
check(closed == ["Lamp"], "what it finds too late is let go of again")
check(b.poll_notifications() is True, "... and asks for one more scan")
b.refresh()
check([d.name for d in b.devices] == ["Keys", "Mouse", "Lamp", "Strip"], "which then has everything")
check(b.poll_notifications() is False, "and the asking stops")

# closing while a scan is running: the scan ends, shutdown returns, nothing waits for the stuck driver
release.clear()
backend.LATE_PROBE_LIMIT = 30.0
scan = threading.Thread(target=b.refresh, daemon=True)
scan.start()
time.sleep(0.3)
t0 = time.monotonic()
b.shutdown()
scan.join(2.0)
check(
    time.monotonic() - t0 < 2.0 and not scan.is_alive(),
    f"shutdown during a scan returns at once ({time.monotonic() - t0:.2f} s)",
)
release.set()

# a table of drivers that changes under the worker's feet must not end the worker
b = backend.LocalBackend()
backend.discover_drivers = lambda: [fake_module([FakeDriver("A", keepalive=True)])]
b.refresh()
real = b._next_keepalive_wait
calls = []


def broken_once():
    calls.append(1)
    if len(calls) == 1:
        raise RuntimeError("dictionary changed size during iteration")
    return real()


b._next_keepalive_wait = broken_once
b.update_leds(b.devices[0], [3] * 4)
b.wait_idle(1.0)
time.sleep(0.5)
b.update_leds(b.devices[0], [4] * 4)
check(
    b.wait_idle(2.0) and b.devices[0].colors == [4] * 4 and len(calls) > 1,
    "the I/O thread survives an error of its own",
)
b.shutdown()

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
