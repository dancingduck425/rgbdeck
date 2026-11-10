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

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
