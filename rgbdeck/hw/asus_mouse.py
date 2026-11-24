# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
# Packet formats learned from G-Helper (https://github.com/seerge/g-helper, GPL-3.0: app/Peripherals/Mouse/
# AsusMouse.cs and Models/HarpeIIAce.cs), rogctl (https://github.com/Humblemonk/rogctl, whose notes hold
# captures of ASUS GearLink talking to a Harpe II Ace) and OpenRGB's AsusAuraMouseController (GPL-2.0-or-later).
# Read as references only: nothing of them is linked or run.
"""
ASUS ROG mice behind the ROG SpeedNova 8K receiver (0b05:1ad0), and the same mice on their cable.

EXPERIMENTAL: the packets are the ones captured from ASUS's own software on a Harpe II Ace, but this driver has
not been run on a real mouse yet.

  ROG Harpe II Ace                  SpeedNova 8K receiver 0b05:1ad0, cable 0b05:1c69
  ROG Harpe II Extreme Edition 20   the same receiver id; told apart by "EXTREME" in the receiver's USB name

What the mouse has is one light, in the scroll wheel. RGB Deck shows it as a mouse with that one light and
offers the mouse's own effects next to it (Static, Breathing, Spectrum Cycle, Reactive, Battery Level, Off).

The protocol (every request is zero-padded to 64 bytes, report id first; the answer echoes its first bytes):

  receiver   USB interface 2, report id 03 (on Windows: collection 3 of that interface)
  cable      USB interface 0, no report ids (written with a leading 00, read without it)

  12 07                              battery: byte 5 = percent, byte 10 = non-zero while charging
  12 03 00                           lighting now: byte 5 effect, byte 6 brightness (percent), bytes 7-9 R G B
  51 28 00 00 <effect> <pct> R G B   set the lighting; shown at once, lost when the mouse is switched off
  50 03                              write what was set into the mouse's memory

  effects    00 static, 01 breathing, 02 color cycle, 04 reactive (lights on a click), 06 battery level
  answers    <id> FF AA = refused. From the receiver that means the mouse is asleep, switched off or out of
             range, as do an all-zero answer, none at all, and a battery of 0 that is not charging.

A color from the app (painting, Sync all, an animation) is sent as "static" WITHOUT the write to memory, so an
animation does not wear the mouse's memory out; only Save and "keep the look when closing" store anything.
A sleeping mouse is not an error: the look is remembered and delivered when the mouse answers again, which is
looked for every ten seconds.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from rgbdeck.hw.hidinfo import parse_collections
from rgbdeck.hw.hidraw import HidDevice, enumerate_hid
from rgbdeck.models import (
    Device,
    DeviceError,
    Mode,
    Zone,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_PER_LED,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_MANUAL_SAVE,
    rgb_to_u32,
    u32_to_rgb,
)

log = logging.getLogger("rgbdeck.asus_mouse")

EXPERIMENTAL = True
VID = 0x0B05
PID_SPEEDNOVA, PID_HARPE_II_CABLE = 0x1AD0, 0x1C69
# product id -> (USB interface, report id, through a receiver)
LINKS = {
    PID_SPEEDNOVA: (2, 0x03, True),
    PID_HARPE_II_CABLE: (0, 0x00, False),
}
USB_IDS = [(VID, pid) for pid in sorted(LINKS)]
NAME, NAME_EXTREME = "ROG Harpe II Ace", "ROG Harpe II Extreme Edition 20"

PACKET = 64  # with the report id
CMD_GET, CMD_SET, CMD_PROFILE = 0x12, 0x51, 0x50
GET_LIGHTING, GET_BATTERY = 0x03, 0x07
SET_LIGHTING, SAVE = 0x28, 0x03
ZONE = 0x00  # the wheel, as ASUS's own software addresses it
FX_STATIC, FX_BREATHING, FX_CYCLE, FX_REACTIVE, FX_BATTERY = 0x00, 0x01, 0x02, 0x04, 0x06
FX_OFF = 0xF0  # only ever read (other software can leave it behind); "off" is sent as brightness 0
MODE_DIRECT, MODE_OFF = -1, -2  # Mode.value of the two modes that are not an effect code of their own

REPLY_TIMEOUT = 0.3  # an awake mouse answers in about 5 ms, in 70 ms after it sat unused
ECHO_AFTER_REFUSAL = 0.15  # a refusal can come just ahead of the real answer: wait this long for that one
MIN_GAP = 0.05  # at most 20 color changes a second go over the radio; the newest one is never dropped
AWAKE_POLL, ASLEEP_POLL = 20.0, 10.0  # seconds between "are you there": to notice sleep, and waking up

OK, REFUSED, EMPTY, SILENT = "ok", "refused", "empty", "silent"


def packet(report_id: int, *body: int) -> bytes:
    """A request: report id, command bytes, zeros up to 64."""
    data = bytes([report_id, *body])
    return data + bytes(PACKET - len(data))


def lighting_packet(report_id: int, effect: int, brightness: int, color: int) -> bytes:
    r, g, b = u32_to_rgb(color)
    pct = max(0, min(100, int(brightness)))
    return packet(report_id, CMD_SET, SET_LIGHTING, ZONE, 0x00, effect, pct, r, g, b)


def parse_battery(reply: bytes) -> Tuple[int, bool]:
    """(percent, charging) from the answer to 12 07."""
    return min(100, reply[5]), reply[10] > 0


def parse_lighting(reply: bytes) -> Tuple[int, int, int]:
    """(effect, brightness in percent, color) from the answer to 12 03."""
    return reply[5], min(100, reply[6]), rgb_to_u32(reply[7], reply[8], reply[9])


def _scaled(color: int, pct: int) -> int:
    return rgb_to_u32(*(int(c * pct / 100 + 0.5) for c in u32_to_rgb(color)))


class AsusMouse:
    def __init__(self, path: str, name: str, report_id: int, wireless: bool, hid=None):
        self.hid = hid if hid is not None else HidDevice(path)
        self.report_id, self.wireless = report_id, wireless
        self.name = name
        self.awake = False
        self._battery: Optional[Tuple[int, bool]] = None
        self._want: Optional[bytes] = None  # the lighting packet the mouse should be showing
        self._shown: Optional[bytes] = None  # the one it took last
        self._sent_at = 0.0
        self._asked_at = 0.0
        self._refusals = 0
        self._hold_until = 0.0  # awake but refusing the lighting command: not tried again before this
        self._painting = False  # the wheel shows a color painted by the app, not one of the mouse's effects
        self._met = False  # only changes after the first look are worth a line in the log
        try:
            self.hid.drain()
            self._ask()
            now = self._read_lighting() if self.awake else None
            if self.awake and now is None:
                raise DeviceError("it answers, but not with lighting RGB Deck knows")
            if not self.awake and not wireless:
                raise DeviceError("no answer over the cable")
        except DeviceError:
            self.hid.close()
            raise
        self._met = True
        self.device = self._build(path, now)
        if self.awake:
            log.info("%s: battery %s %%%s", name, self._battery[0], ", charging" if self._battery[1] else "")
        else:
            log.info("%s: the receiver is there, the mouse is asleep or switched off", name)

    # ---------------------------------------------------------------- talking to the mouse
    @property
    def keepalive_interval(self) -> float:
        """When the backend should call keepalive() next (it asks every time)."""
        if self._waiting():
            return MIN_GAP  # a color is waiting for its turn
        return AWAKE_POLL if self.awake else ASLEEP_POLL

    def _waiting(self) -> bool:
        """Is there a look the mouse should be showing, does not show yet, and could be given now?"""
        return (
            self.awake and self._want is not None and self._want != self._shown and time.monotonic() >= self._hold_until
        )

    def _answer(self, sent: bytes, echo: int, deadline: float):
        """The next report that answers `sent`: (OK | REFUSED | EMPTY | SILENT, the report with its id first)."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return SILENT, b""
            buf = self.hid.read(PACKET + 1, left)
            if buf is None:
                return SILENT, b""
            buf = bytes(buf)
            if self.report_id == 0:
                buf = b"\x00" + buf  # reports without an id arrive without that byte
            elif not buf or buf[0] != self.report_id:
                continue  # another report on the same interface
            if len(buf) < 11:
                continue
            buf += bytes(PACKET - len(buf))
            if buf[:echo] == sent[:echo]:
                return OK, buf
            if buf[1] == 0xFF and buf[2] == 0xAA:
                return REFUSED, buf
            if not any(buf[1:4]):
                return EMPTY, buf

    def _query(self, *body: int, echo: int = 3):
        """Ask for something. The first answer counts, a refusal too."""
        sent = packet(self.report_id, *body)
        self.hid.drain()
        self.hid.write(sent)
        return self._answer(sent, echo, time.monotonic() + REPLY_TIMEOUT)

    def _command(self, sent: bytes) -> str:
        """Send a change or the save and wait for its echo. A refusal does not end the wait at once: this mouse
        can send one just ahead of the echo."""
        self.hid.drain()
        self.hid.write(sent)
        deadline = time.monotonic() + REPLY_TIMEOUT
        worst = SILENT
        while True:
            status, _reply = self._answer(sent, 3, deadline)
            if status == OK:
                return OK
            if status == SILENT:
                return worst
            worst = status
            deadline = min(deadline, time.monotonic() + ECHO_AFTER_REFUSAL)

    def _ask(self) -> bool:
        """Is the mouse there? Reads the battery, which is also how ASUS's own software finds out.

        Whoever asks (a color on its way, the regular look, the app wanting the battery), a mouse found awake
        again counts as showing nothing that was sent before: whether it kept that through its sleep is not
        known, and keepalive_interval then has the look sent again at once."""
        was = self.awake
        self._asked_at = time.monotonic()
        status, reply = self._query(CMD_GET, GET_BATTERY)
        if status == OK:
            pct, charging = parse_battery(reply)
            self.awake = pct > 0 or charging  # a receiver answers 0 % for a mouse that is not there
            self._battery = (pct, charging) if self.awake else None
        else:
            self.awake, self._battery = False, None
        if self.awake and not was:
            self._shown = None
            if self._met:
                log.info("%s is awake", self.name)
        elif was and not self.awake:
            log.info("%s went to sleep or out of range; its look is sent again when it is back", self.name)
        return self.awake

    def _read_lighting(self) -> Optional[Tuple[int, int, int]]:
        """(effect, brightness, color) the mouse shows; None if it refuses the question or stays silent."""
        for _ in range(2):  # an answer can get lost; a refusal is an answer
            status, reply = self._query(CMD_GET, GET_LIGHTING, ZONE)
            if status == OK:
                return parse_lighting(reply)
            if status == REFUSED:
                return None
        return None

    def _deliver(self, force: bool = False) -> bool:
        """Bring the mouse to the look that is wanted. False: it is not there to take it (not an error; the
        look goes out when it is back)."""
        if self._want is None:
            return True
        if not self.awake:
            if time.monotonic() - self._asked_at < ASLEEP_POLL or not self._ask():
                return False
        if self._want == self._shown and not force:
            return True
        now = time.monotonic()
        if not force and (now - self._sent_at < MIN_GAP or now < self._hold_until):
            return True  # too soon after the last one: keepalive() sends the newest in a moment
        want = self._want
        status = self._command(want)
        if status != OK and self._ask():  # there, but it did not take it: once more
            status = self._command(want)
        self._sent_at = time.monotonic()
        if status == OK:
            self._shown, self._refusals = want, 0
            return True
        if self.awake:
            # Awake and still refusing: that is not sleep. Say so once, and do not hold up the other devices
            # (they share one line to the hardware) by trying again on every color that comes along.
            self._refusals += 1
            if self._refusals == 1:
                log.warning("%s does not take the lighting command (%s)", self.name, status)
            self._hold_until = self._sent_at + ASLEEP_POLL
            return True
        return False

    def _save(self) -> bool:
        """Have the mouse write the lighting into its memory. After sitting unused it refuses the first try
        and takes the second."""
        sent = packet(self.report_id, CMD_PROFILE, SAVE)
        return self._command(sent) == OK or self._command(sent) == OK

    # ---------------------------------------------------------------- what the app sees
    def _build(self, path: str, now: Optional[Tuple[int, int, int]]) -> Device:
        color_fx = MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_MANUAL_SAVE
        plain_fx = MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_MANUAL_SAVE
        effect, pct, color = now if now is not None else (FX_STATIC, 0, 0)
        level = pct or 100
        swatch = color or 0x00B4FF

        def colored(index: int, name: str, value: int) -> Mode:
            return Mode(
                index,
                name,
                value,
                color_fx,
                brightness_min=0,
                brightness_max=100,
                brightness=level,
                colors_min=1,
                colors_max=1,
                color_mode=MODE_COLORS_MODE_SPECIFIC,
                colors=[swatch],
            )

        def plain(index: int, name: str, value: int) -> Mode:
            return Mode(index, name, value, plain_fx, brightness_min=0, brightness_max=100, brightness=level)

        modes = [
            Mode(0, "Direct", MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
            colored(1, "Static", FX_STATIC),
            colored(2, "Breathing", FX_BREATHING),
            plain(3, "Spectrum Cycle", FX_CYCLE),
            colored(4, "Reactive", FX_REACTIVE),
            plain(5, "Battery Level", FX_BATTERY),
            Mode(6, "Off", MODE_OFF, MODE_FLAG_MANUAL_SAVE),
        ]
        # Start on what the mouse shows: a static color is the wheel painted in that color, its own effects
        # are the matching effect, anything else (set by other software) is left as it is until asked.
        active, shown = 0, 0
        if effect == FX_STATIC:
            shown = _scaled(color, pct)
        elif effect == FX_OFF:
            active = 6
        else:
            active = next((m.index for m in modes if m.value == effect), 0)
        where = "through the SpeedNova 8K receiver" if self.wireless else "on its cable"
        return Device(
            0,
            "Mouse",
            self.name,
            "ASUS",
            f"ASUS ROG mouse {where}",
            "",
            "",
            "HID: " + path,
            active,
            modes,
            [Zone(0, "Scroll Wheel", 0, 1, 1, 1, None, 0)],
            ["Scroll Wheel"],
            [shown],
        )

    def _packet_for(self, mode: Mode) -> bytes:
        if mode.value == MODE_DIRECT:
            color = self.device.colors[0] & 0xFFFFFF if self.device.colors else 0
            # black is sent as "no brightness": both are what the mouse's own software sends
            return lighting_packet(self.report_id, FX_STATIC, 100 if color else 0, color)
        if mode.value == MODE_OFF:
            return lighting_packet(self.report_id, FX_STATIC, 0, 0)
        color = (mode.colors[0] & 0xFFFFFF) if mode.has_mode_colors and mode.colors else 0
        return lighting_packet(self.report_id, mode.value, mode.brightness, color)

    def set_colors(self, colors: List[int]):
        self.device.colors = [int(colors[0]) & 0xFFFFFF if colors else 0]
        self._painting = True
        self._want = self._packet_for(self.device.modes[0])
        self._deliver()

    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        painted = self.device.colors[0] if self._painting and self.device.colors else None
        self._painting = mode.value == MODE_DIRECT
        if save and painted is not None and mode.value == FX_STATIC and mode.colors[:1] == [painted]:
            # "Keep the look when closing" stores a painted color as Static. It is stored the way it is shown,
            # not at whatever brightness the Static effect was last used with.
            self._want = self._packet_for(self.device.modes[0])
        else:
            self._want = self._packet_for(mode)
        there = self._deliver(force=save)
        if not save:
            return
        if not there:
            raise DeviceError(f"{self.name} is asleep or switched off: its look could not be stored in it")
        if not self._save():
            # The mouse is known to keep a change it refuses to confirm, so this is a note, not an error.
            log.info("%s did not confirm storing its look", self.name)

    def read_colors(self) -> Optional[List[int]]:
        """What the wheel shows, if that is a plain color."""
        now = self._read_lighting() if self.awake or self._ask() else None
        if now is None or now[0] != FX_STATIC:
            return None
        return [_scaled(now[2], now[1])]

    def battery(self) -> Optional[Tuple[int, bool]]:
        """(percent, charging), or None while the mouse sleeps."""
        if time.monotonic() - self._asked_at > 5.0:
            self._ask()
        return self._battery

    def keepalive(self):
        """Called by the backend when nothing else was sent for keepalive_interval seconds: sends a color that
        had to wait, and otherwise looks whether the mouse is (still) there."""
        if not self._waiting():
            self._ask()
        if self._waiting():
            self._deliver(force=True)

    def close(self):
        self.hid.close()


def _right_interface(info, interface: int, report_id: int) -> bool:
    """The interface the lighting protocol lives on. Where the system does not say which interface a node
    belongs to, go by its descriptor: a vendor-defined collection that carries the report id."""
    if info.interface >= 0:
        return info.interface == interface
    try:
        cols = parse_collections(info.descriptor or b"")
    except Exception:  # noqa: BLE001 - a descriptor that cannot be read decides nothing
        cols = []
    return any(page >= 0xFF00 and (report_id in ids if report_id else not ids) for page, _usage, ids in cols)


def probe() -> List[AsusMouse]:
    found: List[AsusMouse] = []
    for info in enumerate_hid(VID):
        if info.pid not in LINKS:
            continue
        interface, report_id, wireless = LINKS[info.pid]
        if not _right_interface(info, interface, report_id):
            continue
        name = NAME_EXTREME if "EXTREME" in (info.name or "").upper() else NAME
        try:
            found.append(AsusMouse(info.path, name, report_id, wireless))
        except DeviceError as e:
            log.info("ASUS mouse %04x at %s (interface %s) skipped: %s", info.pid, info.path, info.interface, e)
    # One mouse can be there twice: on its receiver and on its cable. It answers on one of them (the receiver
    # reports it asleep while it is set to its cable), and that one is the device.
    if any(m.awake for m in found):
        for m in [m for m in found if not m.awake]:
            m.close()
            found.remove(m)
    cabled = [m for m in found if not m.wireless]
    if cabled and len(found) > len(cabled):
        for m in cabled:  # awake on both at once: the receiver is how it is being used
            m.close()
            found.remove(m)
    return found


__all__ = ["USB_IDS", "AsusMouse", "probe"]
