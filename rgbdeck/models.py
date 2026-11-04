"""Device model shared by the UI and the hardware drivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# mode flags
MODE_FLAG_HAS_SPEED = 1 << 0
MODE_FLAG_HAS_DIRECTION_LR = 1 << 1
MODE_FLAG_HAS_DIRECTION_UD = 1 << 2
MODE_FLAG_HAS_DIRECTION_HV = 1 << 3
MODE_FLAG_HAS_BRIGHTNESS = 1 << 4
MODE_FLAG_HAS_PER_LED_COLOR = 1 << 5
MODE_FLAG_HAS_MODE_SPECIFIC_COLOR = 1 << 6
MODE_FLAG_HAS_RANDOM_COLOR = 1 << 7
MODE_FLAG_MANUAL_SAVE = 1 << 8
MODE_FLAG_AUTOMATIC_SAVE = 1 << 9

MODE_COLORS_NONE = 0
MODE_COLORS_PER_LED = 1
MODE_COLORS_MODE_SPECIFIC = 2
MODE_COLORS_RANDOM = 3

DIRECTION_NAMES = {0: "Left", 1: "Right", 2: "Up", 3: "Down", 4: "Horizontal", 5: "Vertical"}

NO_LED = 0xFFFFFFFF


def rgb_to_u32(r: int, g: int, b: int) -> int:
    return (r & 0xFF) | ((g & 0xFF) << 8) | ((b & 0xFF) << 16)


def u32_to_rgb(c: int) -> Tuple[int, int, int]:
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


class DeviceError(Exception):
    """A device stopped answering or went away."""


@dataclass
class Mode:
    index: int
    name: str
    value: int
    flags: int
    speed_min: int = 0
    speed_max: int = 0
    brightness_min: int = 0
    brightness_max: int = 0
    colors_min: int = 0
    colors_max: int = 0
    speed: int = 0
    brightness: int = 0
    direction: int = 0
    color_mode: int = MODE_COLORS_NONE
    colors: List[int] = field(default_factory=list)

    @property
    def has_speed(self):
        return bool(self.flags & MODE_FLAG_HAS_SPEED)

    @property
    def has_brightness(self):
        return bool(self.flags & MODE_FLAG_HAS_BRIGHTNESS)

    @property
    def has_per_led(self):
        return bool(self.flags & MODE_FLAG_HAS_PER_LED_COLOR)

    @property
    def has_mode_colors(self):
        return bool(self.flags & MODE_FLAG_HAS_MODE_SPECIFIC_COLOR)

    @property
    def has_random(self):
        return bool(self.flags & MODE_FLAG_HAS_RANDOM_COLOR)

    @property
    def can_save(self):
        return bool(self.flags & MODE_FLAG_MANUAL_SAVE)

    def directions(self) -> List[int]:
        out = []
        if self.flags & MODE_FLAG_HAS_DIRECTION_LR:
            out += [0, 1]
        if self.flags & MODE_FLAG_HAS_DIRECTION_UD:
            out += [2, 3]
        if self.flags & MODE_FLAG_HAS_DIRECTION_HV:
            out += [4, 5]
        return out


@dataclass
class Zone:
    index: int
    name: str
    type: int
    leds_min: int
    leds_max: int
    leds_count: int
    matrix: Optional[List[List[int]]] = None
    start_idx: int = 0


@dataclass
class Device:
    index: int
    type_name: str  # "Keyboard" | "Mouse" | "Motherboard" | ...
    name: str
    vendor: str
    description: str
    version: str
    serial: str
    location: str
    active_mode: int
    modes: List[Mode]
    zones: List[Zone]
    led_names: List[str]
    colors: List[int]
    experimental: bool = False  # driver ported from sources but not yet verified on real hardware

    @property
    def current_mode(self) -> Mode:
        return self.modes[self.active_mode]

    def mode_named(self, name: str) -> Optional[Mode]:
        for m in self.modes:
            if m.name.lower() == name.lower():
                return m
        return None
