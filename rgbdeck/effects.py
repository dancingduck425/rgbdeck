"""
Software (app-driven) keyboard effects: reactive key glow and ripples.

Key presses come from evdev (/dev/input/event*). Frames are rendered onto the
user's own per-key colors and pushed to the keyboard at a capped rate.
"""

from __future__ import annotations

import colorsys
import glob
import math
import os
import random
import select
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from rgbdeck.models import rgb_to_u32, u32_to_rgb

# Linux input_event: struct timeval (16 bytes on 64-bit) + u16 type + u16 code + s32 value
_EV_FMT = "llHHi"
_EV_SIZE = struct.calcsize(_EV_FMT)
EV_KEY = 0x01

# evdev KEY_* name -> LED name (after "Key: ")
KEYMAP: Dict[str, str] = {
    "KEY_ESC": "Escape",
    "KEY_GRAVE": "`",
    "KEY_MINUS": "-",
    "KEY_EQUAL": "=",
    "KEY_BACKSPACE": "Backspace",
    "KEY_TAB": "Tab",
    "KEY_LEFTBRACE": "[",
    "KEY_RIGHTBRACE": "]",
    "KEY_BACKSLASH": "\\ (ANSI)",
    "KEY_CAPSLOCK": "Caps Lock",
    "KEY_SEMICOLON": ";",
    "KEY_APOSTROPHE": "'",
    "KEY_ENTER": "Enter",
    "KEY_LEFTSHIFT": "Left Shift",
    "KEY_COMMA": ",",
    "KEY_DOT": ".",
    "KEY_SLASH": "/",
    "KEY_RIGHTSHIFT": "Right Shift",
    "KEY_LEFTCTRL": "Left Control",
    "KEY_LEFTMETA": "Left Windows",
    "KEY_LEFTALT": "Left Alt",
    "KEY_SPACE": "Space",
    "KEY_RIGHTALT": "Right Alt",
    "KEY_RIGHTMETA": "Right Windows",
    "KEY_COMPOSE": "Menu",
    "KEY_RIGHTCTRL": "Right Control",
    "KEY_SYSRQ": "Print Screen",
    "KEY_SCROLLLOCK": "Scroll Lock",
    "KEY_PAUSE": "Pause/Break",
    "KEY_INSERT": "Insert",
    "KEY_HOME": "Home",
    "KEY_PAGEUP": "Page Up",
    "KEY_DELETE": "Delete",
    "KEY_END": "End",
    "KEY_PAGEDOWN": "Page Down",
    "KEY_UP": "Up Arrow",
    "KEY_DOWN": "Down Arrow",
    "KEY_LEFT": "Left Arrow",
    "KEY_RIGHT": "Right Arrow",
    "KEY_NUMLOCK": "Num Lock",
    "KEY_KPSLASH": "Number Pad /",
    "KEY_KPASTERISK": "Number Pad *",
    "KEY_KPMINUS": "Number Pad -",
    "KEY_KPPLUS": "Number Pad +",
    "KEY_KPENTER": "Number Pad Enter",
    "KEY_KPDOT": "Number Pad .",
    "KEY_FN": "Right Fn",
    "KEY_102ND": "\\ (ISO)",
    "KEY_RO": "Ro (JIS)",
    "KEY_YEN": "Yen (JIS)",
    "KEY_HENKAN": "Henkan (JIS)",
    "KEY_MUHENKAN": "Muhenkan (JIS)",
    "KEY_KATAKANAHIRAGANA": "Katakana/Hiragana (JIS)",
    "KEY_KPEQUAL": "Number Pad =",
    "KEY_KPCOMMA": "Number Pad ,",
}
for _n in range(13, 25):
    KEYMAP[f"KEY_F{_n}"] = f"F{_n}"
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEYMAP[f"KEY_{_c}"] = _c
for _n in range(10):
    KEYMAP[f"KEY_{_n}"] = str(_n)
    KEYMAP[f"KEY_KP{_n}"] = f"Number Pad {_n}"
for _n in range(1, 13):
    KEYMAP[f"KEY_F{_n}"] = f"F{_n}"
ALIASES = {"Right Windows": "Right Fn"}  # boards without a right Win key put Fn there (G513)


EVDEV_CODES: Dict[int, str] = {
    1: "KEY_ESC",
    2: "KEY_1",
    3: "KEY_2",
    4: "KEY_3",
    5: "KEY_4",
    6: "KEY_5",
    7: "KEY_6",
    8: "KEY_7",
    9: "KEY_8",
    10: "KEY_9",
    11: "KEY_0",
    12: "KEY_MINUS",
    13: "KEY_EQUAL",
    14: "KEY_BACKSPACE",
    15: "KEY_TAB",
    16: "KEY_Q",
    17: "KEY_W",
    18: "KEY_E",
    19: "KEY_R",
    20: "KEY_T",
    21: "KEY_Y",
    22: "KEY_U",
    23: "KEY_I",
    24: "KEY_O",
    25: "KEY_P",
    26: "KEY_LEFTBRACE",
    27: "KEY_RIGHTBRACE",
    28: "KEY_ENTER",
    29: "KEY_LEFTCTRL",
    30: "KEY_A",
    31: "KEY_S",
    32: "KEY_D",
    33: "KEY_F",
    34: "KEY_G",
    35: "KEY_H",
    36: "KEY_J",
    37: "KEY_K",
    38: "KEY_L",
    39: "KEY_SEMICOLON",
    40: "KEY_APOSTROPHE",
    41: "KEY_GRAVE",
    42: "KEY_LEFTSHIFT",
    43: "KEY_BACKSLASH",
    44: "KEY_Z",
    45: "KEY_X",
    46: "KEY_C",
    47: "KEY_V",
    48: "KEY_B",
    49: "KEY_N",
    50: "KEY_M",
    51: "KEY_COMMA",
    52: "KEY_DOT",
    53: "KEY_SLASH",
    54: "KEY_RIGHTSHIFT",
    55: "KEY_KPASTERISK",
    56: "KEY_LEFTALT",
    57: "KEY_SPACE",
    58: "KEY_CAPSLOCK",
    59: "KEY_F1",
    60: "KEY_F2",
    61: "KEY_F3",
    62: "KEY_F4",
    63: "KEY_F5",
    64: "KEY_F6",
    65: "KEY_F7",
    66: "KEY_F8",
    67: "KEY_F9",
    68: "KEY_F10",
    69: "KEY_NUMLOCK",
    70: "KEY_SCROLLLOCK",
    71: "KEY_KP7",
    72: "KEY_KP8",
    73: "KEY_KP9",
    74: "KEY_KPMINUS",
    75: "KEY_KP4",
    76: "KEY_KP5",
    77: "KEY_KP6",
    78: "KEY_KPPLUS",
    79: "KEY_KP1",
    80: "KEY_KP2",
    81: "KEY_KP3",
    82: "KEY_KP0",
    83: "KEY_KPDOT",
    86: "KEY_102ND",
    87: "KEY_F11",
    88: "KEY_F12",
    89: "KEY_RO",
    92: "KEY_HENKAN",
    93: "KEY_KATAKANAHIRAGANA",
    94: "KEY_MUHENKAN",
    96: "KEY_KPENTER",
    97: "KEY_RIGHTCTRL",
    98: "KEY_KPSLASH",
    99: "KEY_SYSRQ",
    100: "KEY_RIGHTALT",
    102: "KEY_HOME",
    103: "KEY_UP",
    104: "KEY_PAGEUP",
    105: "KEY_LEFT",
    106: "KEY_RIGHT",
    107: "KEY_END",
    108: "KEY_DOWN",
    109: "KEY_PAGEDOWN",
    110: "KEY_INSERT",
    111: "KEY_DELETE",
    117: "KEY_KPEQUAL",
    119: "KEY_PAUSE",
    121: "KEY_KPCOMMA",
    124: "KEY_YEN",
    125: "KEY_LEFTMETA",
    126: "KEY_RIGHTMETA",
    127: "KEY_COMPOSE",
    183: "KEY_F13",
    184: "KEY_F14",
    185: "KEY_F15",
    186: "KEY_F16",
    187: "KEY_F17",
    188: "KEY_F18",
    189: "KEY_F19",
    190: "KEY_F20",
    191: "KEY_F21",
    192: "KEY_F22",
    193: "KEY_F23",
    194: "KEY_F24",
    464: "KEY_FN",
}


def _evdev_code_names() -> Dict[int, str]:
    """Linux input key codes -> KEY_* names (embedded; python-evdev is optional and only adds more)."""
    out = dict(EVDEV_CODES)
    try:
        from evdev import ecodes  # type: ignore

        for code, name in ecodes.KEY.items():
            out.setdefault(code, name if isinstance(name, str) else name[0])
    except ImportError:
        pass
    return out


def find_keyboard_events(name_hint: str = "") -> List[str]:
    """Event nodes that look like our keyboard (by-id *-event-kbd matching the hint)."""
    hint = "".join(ch for ch in name_hint.lower() if ch.isalnum())
    found = []
    for p in sorted(glob.glob("/dev/input/by-id/*-event-kbd")):
        base = "".join(ch for ch in os.path.basename(p).lower() if ch.isalnum())
        if not hint or any(tok in base for tok in (hint[:12], hint.split("rgb")[0][:10])):
            found.append(os.path.realpath(p))
    return found


class KeyListener(threading.Thread):
    """Reads key-down events from evdev nodes; calls on_key(led_name) from the thread."""

    def __init__(self, paths: List[str], on_key: Callable[[str], None]):
        super().__init__(daemon=True)
        self.paths = paths
        self.on_key = on_key
        self._stop = threading.Event()
        self.names = _evdev_code_names()
        self.ok = False
        self.error = ""

    def run(self):
        fds = []
        for p in self.paths:
            try:
                fds.append(os.open(p, os.O_RDONLY | os.O_NONBLOCK))
            except OSError as e:
                self.error = f"{p}: {e}"
        if not fds:
            return
        self.ok = True
        try:
            while not self._stop.is_set():
                r, _, _ = select.select(fds, [], [], 0.25)
                for fd in r:
                    try:
                        data = os.read(fd, _EV_SIZE * 64)
                    except BlockingIOError:
                        continue
                    except OSError:
                        self.ok = False
                        return
                    for off in range(0, len(data) - _EV_SIZE + 1, _EV_SIZE):
                        _s, _us, etype, code, value = struct.unpack_from(_EV_FMT, data, off)
                        if etype == EV_KEY and value == 1:
                            kname = self.names.get(code)
                            led = KEYMAP.get(kname or "")
                            if led:
                                self.on_key(ALIASES.get(led, led))
        finally:
            for fd in fds:
                os.close(fd)

    def stop(self):
        self._stop.set()


@dataclass
class EffectSettings:
    kind: str = "ripple"  # "ripple" | "glow" | "ripple+glow"
    color_source: str = "active"  # "active" | "rainbow" | "random" | "key"
    speed: int = 60  # percent
    size: int = 50  # ring thickness percent
    fade: int = 50  # glow duration percent
    background: str = "colors"  # "colors" | "black" | "dim"
    fps: int = 24

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


@dataclass
class _Ripple:
    x: float
    y: float
    t0: float
    color: Tuple[int, int, int]


@dataclass
class _Glow:
    led: int
    t0: float
    color: Tuple[int, int, int]


class EffectEngine:
    """
    Renders frames: background + active ripples/glows.
    tick() returns a frame (list of u32) when something changed, else None.
    """

    def __init__(
        self,
        positions: Dict[int, Tuple[float, float]],
        name_to_led: Dict[str, int],
        active_color: Callable[[], Tuple[int, int, int]],
    ):
        self.positions = positions  # led -> (x, y) in key units (centres)
        self.name_to_led = name_to_led
        self.active_color = active_color
        self.settings = EffectSettings()
        self.background: List[int] = []
        self._ripples: List[_Ripple] = []
        self._glows: List[_Glow] = []
        self._lock = threading.Lock()
        self._idle_sent = True
        self._hue = random.random()
        n = max(positions) + 1 if positions else 0
        self._n = n
        xs = [p[0] for p in positions.values()] or [0]
        ys = [p[1] for p in positions.values()] or [0]
        self._extent = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1.0

    # ---- input -----------------------------------------------------------------
    def key_pressed(self, led_name: str):
        led = self.name_to_led.get(led_name)
        if led is None or led not in self.positions:
            return
        col = self._pick_color(led)
        now = time.monotonic()
        with self._lock:
            if self.settings.kind in ("ripple", "ripple+glow"):
                x, y = self.positions[led]
                self._ripples.append(_Ripple(x, y, now, col))
                if len(self._ripples) > 24:
                    self._ripples.pop(0)
            if self.settings.kind in ("glow", "ripple+glow"):
                self._glows = [g for g in self._glows if g.led != led]
                self._glows.append(_Glow(led, now, col))

    def _pick_color(self, led: int) -> Tuple[int, int, int]:
        src = self.settings.color_source
        if src == "rainbow":
            self._hue = (self._hue + 0.09) % 1.0
            return tuple(int(v * 255) for v in colorsys.hsv_to_rgb(self._hue, 1, 1))
        if src == "random":
            return tuple(int(v * 255) for v in colorsys.hsv_to_rgb(random.random(), 1, 1))
        if src == "key" and led < len(self.background):
            r, g, b = u32_to_rgb(self.background[led])
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            # complementary, full brightness, so it shows on top of the key's own color
            return tuple(int(c * 255) for c in colorsys.hsv_to_rgb((h + 0.5) % 1.0, max(0.6, s), 1.0))
        return self.active_color()

    # ---- rendering -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool(self._ripples or self._glows) or not self._idle_sent

    def _bg(self, led: int) -> Tuple[float, float, float]:
        if self.settings.background == "black" or led >= len(self.background):
            return 0.0, 0.0, 0.0
        r, g, b = u32_to_rgb(self.background[led])
        if self.settings.background == "dim":
            return r * 0.18, g * 0.18, b * 0.18
        return float(r), float(g), float(b)

    def tick(self, now: Optional[float] = None) -> Optional[List[int]]:
        now = time.monotonic() if now is None else now
        s = self.settings
        speed = 4.0 + s.speed / 100.0 * 26.0  # key-units per second
        thickness = 0.8 + s.size / 100.0 * 3.2  # ring thickness in key units
        life = (self._extent + thickness) / speed
        glow_life = 0.25 + s.fade / 100.0 * 2.0
        with self._lock:
            self._ripples = [r for r in self._ripples if now - r.t0 <= life]
            self._glows = [g for g in self._glows if now - g.t0 <= glow_life]
            ripples = list(self._ripples)
            glows = list(self._glows)
        if not ripples and not glows:
            if self._idle_sent:
                return None
            self._idle_sent = True
            return (
                list(self.background)
                if s.background == "colors"
                else [rgb_to_u32(*(int(c) for c in self._bg(i))) for i in range(self._n)]
            )
        self._idle_sent = False
        frame = []
        for led in range(self._n):
            pos = self.positions.get(led)
            r, g, b = self._bg(led)
            if pos is not None:
                for rp in ripples:
                    age = now - rp.t0
                    radius = age * speed
                    d = math.hypot(pos[0] - rp.x, pos[1] - rp.y)
                    k = abs(d - radius) / thickness
                    if k < 1.0:
                        w = (1.0 - k) ** 1.5 * max(0.0, 1.0 - age / life) ** 0.6
                        r = r + (rp.color[0] - r) * w
                        g = g + (rp.color[1] - g) * w
                        b = b + (rp.color[2] - b) * w
                for gl in glows:
                    if gl.led == led:
                        w = max(0.0, 1.0 - (now - gl.t0) / glow_life) ** 1.3
                        r = r + (gl.color[0] - r) * w
                        g = g + (gl.color[1] - g) * w
                        b = b + (gl.color[2] - b) * w
            frame.append(rgb_to_u32(int(min(255, max(0, r))), int(min(255, max(0, g))), int(min(255, max(0, b)))))
        return frame
