# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
The last stage every color passes on its way to a device, and the one list of actions that the window, the tray
menu, the command line, automations and the phone page all use.

Whoever decides colors (painting, a preset, an animation, a typing effect) hands the frame to send(). The
director remembers it and applies what sits on top of every look:

    flash      a short pulse toward one color on all devices (a notification, a low battery)
    master     overall brightness, 0..100
    blackout   lights off for a reason ("user", "idle", "schedule"); the look underneath is kept and comes
               back when the last reason is gone

Devices that show an onboard effect instead of per-LED colors can't be dimmed frame by frame: under a blackout
they are switched to their "Off" mode and get their mode back afterwards.

The host (the main window, or a stand-in in tests) supplies the looks themselves through a few methods:
presets() -> [names], apply_preset(name), current_preset() -> name or None, set_animation(kind, speed),
animation() -> (kind, speed), apply_color((r, g, b)), all_off(), shown_base(device) -> [colors].
"""

from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from rgbdeck import animations
from rgbdeck.models import MODE_COLORS_PER_LED, DeviceError, rgb_to_u32, u32_to_rgb

log = logging.getLogger("rgbdeck.director")

FLASH_PULSE = 0.45  # seconds per pulse
FLASH_TICK_MS = 33


def _scale(colors: List[int], k: float) -> List[int]:
    if k >= 0.999:
        return list(colors)
    return [rgb_to_u32(*(int(x * k + 0.5) for x in u32_to_rgb(c))) for c in colors]


def _toward(colors: List[int], rgb: Tuple[int, int, int], w: float) -> List[int]:
    return [rgb_to_u32(*(int(a + (b - a) * w + 0.5) for a, b in zip(u32_to_rgb(c), rgb))) for c in colors]


class Director(QObject):
    changed = Signal()  # master / blackout / preset / animation changed: menus and pages refresh

    def __init__(self, client, host, parent=None):
        super().__init__(parent)
        self.client, self.host = client, host
        self.master = 100
        self._blackout: set = set()
        self._raw: Dict[int, List[int]] = {}  # device index -> the look as last handed to send()
        self._parked: Dict[int, int] = {}  # device index -> mode it was in before a blackout switched it off
        self._flash: Optional[tuple] = None  # (t0, duration, rgb, pulses)
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._flash_tick)

    # ---- output stage -----------------------------------------------------------
    def send(self, dev, colors: List[int]):
        """Show this look on the device (subject to flash, master brightness and blackout)."""
        self._raw[dev.index] = list(colors)
        self.client.update_leds(dev, self._final(dev, colors))

    def _final(self, dev, colors: List[int]) -> List[int]:
        if self._blackout:
            return [0] * len(colors)
        out = list(colors)
        fl = self._flash
        if fl is not None:
            w = self._flash_weight(time.monotonic())
            if w > 0.0 and (any(out) or not getattr(self.client.driver_for(dev), "keeps_state", False)):
                out = _toward(out, fl[2], w)  # a room light that is off does not flash
        return _scale(out, self.master / 100.0)

    def forget(self):
        """After a device scan: indexes mean different devices now."""
        self._raw.clear()
        self._parked.clear()

    def refresh(self):
        """Looks were changed behind send() (a preset was applied): take them from the host again."""
        self._raw.clear()
        if self.master < 100 or self._blackout:
            self.resend()

    def resend(self):
        """Master, blackout or flash changed: put every per-LED device's look out again."""
        for dev in list(self.client.devices):
            if not dev.led_names or dev.current_mode.color_mode != MODE_COLORS_PER_LED:
                continue
            raw = self._raw.get(dev.index)
            if raw is None or len(raw) != len(dev.colors):
                raw = self.host.shown_base(dev)
                self._raw[dev.index] = list(raw)
            try:
                self.client.update_leds(dev, self._final(dev, raw))
            except DeviceError:
                continue

    # ---- master brightness / blackout ---------------------------------------------
    def set_master(self, value: int):
        value = max(5, min(100, int(value)))
        if value != self.master:
            self.master = value
            self.resend()
            self.changed.emit()

    @property
    def dark(self) -> bool:
        return bool(self._blackout)

    def blackout(self, reason: str, on: bool):
        """Lights off for this reason / the reason is gone. The look returns when no reason is left."""
        was = bool(self._blackout)
        (self._blackout.add if on else self._blackout.discard)(reason)
        now = bool(self._blackout)
        if was == now:
            return
        for dev in list(self.client.devices):
            try:
                if dev.current_mode.color_mode == MODE_COLORS_PER_LED:
                    continue
                if now:
                    off = dev.mode_named("Off")
                    if off is not None and dev.current_mode is not off:
                        self._parked[dev.index] = dev.active_mode
                        self.client.update_mode(dev, off)
                elif dev.index in self._parked:
                    self.client.update_mode(dev, dev.modes[self._parked.pop(dev.index)])
            except (DeviceError, IndexError):
                continue
        self.resend()
        self.changed.emit()

    def toggle(self):
        self.blackout("user", "user" not in self._blackout)

    # ---- flash ------------------------------------------------------------------
    def flash(self, rgb: Tuple[int, int, int], pulses: int = 2):
        """Pulse every device toward this color a couple of times, then leave the look as it was."""
        if self._blackout:
            return
        pulses = max(1, min(6, int(pulses)))
        self._flash = (time.monotonic(), pulses * FLASH_PULSE, tuple(rgb), pulses)
        self._flash_timer.start(FLASH_TICK_MS)
        self._flash_tick()

    def _flash_weight(self, now: float) -> float:
        fl = self._flash
        if fl is None:
            return 0.0
        phase = (now - fl[0]) / FLASH_PULSE
        if phase >= fl[3] or phase < 0:
            return 0.0
        return 0.85 * math.sin(math.pi * (phase - int(phase))) ** 2

    def _flash_tick(self):
        fl = self._flash
        if fl is None or time.monotonic() - fl[0] >= fl[1]:
            self._flash = None
            self._flash_timer.stop()
        self.resend()

    # ---- actions ------------------------------------------------------------------
    def next_preset(self) -> Optional[str]:
        names = self.host.presets()
        if not names:
            return None
        cur = self.host.current_preset()
        name = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self.host.apply_preset(name)
        self.changed.emit()
        return name

    def state(self) -> dict:
        kind, speed = self.host.animation()
        return {
            "master": self.master,
            "dark": self.dark,
            "presets": list(self.host.presets()),
            "preset": self.host.current_preset(),
            "animation": kind,
            "speed": speed,
            "animations": [{"kind": k, "name": label} for k, label, _tip in animations.KINDS],
            "devices": [d.name for d in self.client.devices],
        }

    def command(self, line: str) -> str:
        """One text command (control channel, command line, phone page). Replies start with "ok" or "no"."""
        cmd, _, arg = line.strip().partition(" ")
        arg = arg.strip()
        try:
            if cmd == "preset":
                if arg not in self.host.presets():
                    return f"no preset named {arg!r}"
                self.host.apply_preset(arg)
                self.changed.emit()
                return f"ok preset {arg}"
            if cmd == "next-preset":
                name = self.next_preset()
                return f"ok preset {name}" if name else "no presets saved yet"
            if cmd == "animate":
                kind, _, speed = arg.partition(" ")
                if kind in ("off", "none", ""):
                    kind = None
                elif kind not in animations.NAMES:
                    return f"no animation named {kind!r} (have: {', '.join(animations.NAMES)})"
                self.host.set_animation(kind, int(speed) if speed.strip().isdigit() else None)
                self.changed.emit()
                return f"ok animate {kind or 'off'}"
            if cmd == "brightness":
                if arg[:1] in "+-" and arg[1:].isdigit():
                    self.set_master(self.master + int(arg))
                elif arg.isdigit():
                    self.set_master(int(arg))
                else:
                    return "no brightness given (0-100, +10, -10)"
                return f"ok brightness {self.master}"
            if cmd in ("toggle", "on", "off"):
                self.blackout("user", "user" not in self._blackout if cmd == "toggle" else cmd == "off")
                return "ok lights " + ("off" if self.dark else "on")
            if cmd == "color":
                text = arg.lstrip("#")
                if len(text) != 6:
                    return "no color given (RRGGBB)"
                self.host.apply_color((int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)))
                self.changed.emit()
                return f"ok color {text.upper()}"
            if cmd == "flash":
                text = arg.lstrip("#") or "FFFFFF"
                self.flash((int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)))
                return "ok flash"
            if cmd == "state":
                import json

                return "ok " + json.dumps(self.state())
        except (ValueError, DeviceError) as e:
            return f"no {e}"
        return "unknown command"
