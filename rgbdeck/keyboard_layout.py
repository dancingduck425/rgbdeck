# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Physical key geometry for drawing a keyboard from its LED names.

Units: 1.0 = one standard key. Each entry: (led name suffix, x, y, w, h, label).
Names are matched against the device's LED names after stripping "Key: ".
Anything unmatched is laid out in a strip below the board.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from rgbdeck.models import Device, Zone, NO_LED


@dataclass
class KeyGeom:
    led: int  # LED index in device.colors
    x: float
    y: float
    w: float
    h: float
    label: str


LABELS = {
    "Escape": "Esc",
    "Print Screen": "PrtSc",
    "Scroll Lock": "ScrLk",
    "Pause/Break": "Pause",
    "Backspace": "⌫",
    "Insert": "Ins",
    "Home": "Home",
    "Page Up": "PgUp",
    "Delete": "Del",
    "End": "End",
    "Page Down": "PgDn",
    "Tab": "Tab",
    "Caps Lock": "Caps",
    "Enter": "Enter",
    "Left Shift": "Shift",
    "Right Shift": "Shift",
    "Left Control": "Ctrl",
    "Right Control": "Ctrl",
    "Left Windows": "Win",
    "Right Windows": "Win",
    "Left Alt": "Alt",
    "Right Alt": "Alt",
    "Right Fn": "Fn",
    "Left Fn": "Fn",
    "Menu": "Menu",
    "Space": "",
    "Up Arrow": "↑",
    "Down Arrow": "↓",
    "Left Arrow": "←",
    "Right Arrow": "→",
    "Num Lock": "Num",
    "Number Pad /": "/",
    "Number Pad *": "*",
    "Number Pad -": "-",
    "Number Pad +": "+",
    "Number Pad Enter": "Ent",
    "Number Pad .": ".",
    "\\ (ANSI)": "\\",
    "\\": "\\",
    "Caps Lock Indicator": "Caps●",
    "Game Mode Indicator": "Game●",
    "Num Lock Indicator": "Num●",
    "# (ISO)": "#",
    "\\ (ISO)": "\\",
    "Ro (JIS)": "ろ",
    "Yen (JIS)": "¥",
    "Henkan (JIS)": "変換",
    "Muhenkan (JIS)": "無変換",
    "Katakana/Hiragana (JIS)": "かな",
    "Number Pad =": "=",
    "Number Pad ,": ",",
    "Scroll Lock Indicator": "Scrl●",
    "Logo": "Logo",
}
for _n in range(10):
    LABELS[f"Number Pad {_n}"] = str(_n)


def _row(y: float, start_x: float, keys: List) -> List:
    """keys: list of name or (name, width)."""
    out, x = [], start_x
    for k in keys:
        if isinstance(k, tuple):
            name, w = k
        else:
            name, w = k, 1.0
        if name is not None:
            out.append((name, x, y, w, 1.0))
        x += w
    return out


ANSI_104: List = []
# function row
ANSI_104 += _row(
    0.0,
    0.0,
    [
        "Escape",
        (None, 1.0),
        "F1",
        "F2",
        "F3",
        "F4",
        (None, 0.5),
        "F5",
        "F6",
        "F7",
        "F8",
        (None, 0.5),
        "F9",
        "F10",
        "F11",
        "F12",
        (None, 0.25),
        "Print Screen",
        "Scroll Lock",
        "Pause/Break",
    ],
)
# indicators above the numpad
ANSI_104 += [
    ("Caps Lock Indicator", 18.5, 0.25, 1.0, 0.5),
    ("Game Mode Indicator", 19.5, 0.25, 1.0, 0.5),
    ("Num Lock Indicator", 20.5, 0.25, 1.0, 0.5),
    ("Scroll Lock Indicator", 21.5, 0.25, 1.0, 0.5),
]
# number row
ANSI_104 += _row(
    1.5,
    0.0,
    [
        "`",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "0",
        "-",
        "=",
        ("Backspace", 2.0),
        (None, 0.25),
        "Insert",
        "Home",
        "Page Up",
        (None, 0.25),
        "Num Lock",
        "Number Pad /",
        "Number Pad *",
        "Number Pad -",
    ],
)
ANSI_104 += _row(
    2.5,
    0.0,
    [
        ("Tab", 1.5),
        "Q",
        "W",
        "E",
        "R",
        "T",
        "Y",
        "U",
        "I",
        "O",
        "P",
        "[",
        "]",
        ("\\ (ANSI)", 1.5),
        (None, 0.25),
        "Delete",
        "End",
        "Page Down",
        (None, 0.25),
        "Number Pad 7",
        "Number Pad 8",
        "Number Pad 9",
    ],
)
ANSI_104 += [("Number Pad +", 21.5, 2.5, 1.0, 2.0)]
ANSI_104 += _row(
    3.5,
    0.0,
    [
        ("Caps Lock", 1.75),
        "A",
        "S",
        "D",
        "F",
        "G",
        "H",
        "J",
        "K",
        "L",
        ";",
        "'",
        ("Enter", 2.25),
        (None, 3.5),
        "Number Pad 4",
        "Number Pad 5",
        "Number Pad 6",
    ],
)
ANSI_104 += _row(
    4.5,
    0.0,
    [
        ("Left Shift", 2.25),
        "Z",
        "X",
        "C",
        "V",
        "B",
        "N",
        "M",
        ",",
        ".",
        "/",
        ("Right Shift", 2.75),
        (None, 1.25),
        "Up Arrow",
        (None, 1.25),
        "Number Pad 1",
        "Number Pad 2",
        "Number Pad 3",
    ],
)
ANSI_104 += [("Number Pad Enter", 21.5, 4.5, 1.0, 2.0)]
ANSI_104 += [
    ("# (ISO)", 12.75, 3.5, 1.0, 1.0),
    ("\\ (ISO)", 1.25, 4.5, 1.0, 1.0),
    ("Yen (JIS)", 13.0, 1.5, 1.0, 1.0),
    ("Ro (JIS)", 11.25, 4.5, 1.0, 1.0),
]
ANSI_104 += _row(
    5.5,
    0.0,
    [
        ("Left Control", 1.25),
        ("Left Windows", 1.25),
        ("Left Alt", 1.25),
        ("Space", 6.25),
        ("Right Alt", 1.25),
        ("Right Fn", 1.25),
        ("Menu", 1.25),
        ("Right Control", 1.25),
        (None, 0.25),
        "Left Arrow",
        "Down Arrow",
        "Right Arrow",
        (None, 0.25),
        ("Number Pad 0", 2.0),
        "Number Pad .",
    ],
)

ALIASES = {
    "Right Windows": "Right Fn",  # boards with an Fn key where the right Win key would be (G513, G512, G810)
    "Left Fn": "Menu",
    "\\": "\\ (ANSI)",
    "Backslash": "\\ (ANSI)",
    "Print": "Print Screen",
    "Pause": "Pause/Break",
    "Return": "Enter",
    "ISO Enter": "Enter",
}


def strip_key(name: str) -> str:
    return name[5:] if name.startswith("Key: ") else name


def label_for(name: str) -> str:
    n = strip_key(name)
    if n in LABELS:
        return LABELS[n]
    return n if len(n) <= 6 else n[:6]


def build_keyboard_geometry(dev: Device) -> Optional[List[KeyGeom]]:
    """Physical ANSI layout from LED names. None if too few names match."""
    by_name: Dict[str, int] = {}
    for i, n in enumerate(dev.led_names):
        k = strip_key(n)
        by_name.setdefault(ALIASES.get(k, k), i)
    geoms: List[KeyGeom] = []
    used = set()
    for name, x, y, w, h in ANSI_104:
        if name in by_name and by_name[name] not in used:
            led = by_name[name]
            used.add(led)
            geoms.append(KeyGeom(led, x, y, w, h, LABELS.get(name, name)))
    if len(used) < 0.7 * max(1, len(dev.led_names)):
        return None
    # leftovers go in a strip under the board
    leftovers = [i for i in range(len(dev.led_names)) if i not in used]
    for n, led in enumerate(leftovers):
        geoms.append(KeyGeom(led, (n % 22) * 1.0, 7.0 + (n // 22) * 1.0, 1.0, 1.0, label_for(dev.led_names[led])))
    return geoms


def build_matrix_geometry(dev: Device, zone: Zone, y0: float) -> List[KeyGeom]:
    """Fallback: draw a device matrix map as a grid; merge equal neighbours into wide keys."""
    geoms: List[KeyGeom] = []
    for r, row in enumerate(zone.matrix):
        c = 0
        while c < len(row):
            v = row[c]
            if v == NO_LED:
                c += 1
                continue
            w = 1
            while c + w < len(row) and row[c + w] == v:
                w += 1
            led = zone.start_idx + v
            label = label_for(dev.led_names[led]) if led < len(dev.led_names) else str(v)
            geoms.append(KeyGeom(led, c, y0 + r, w, 1.0, label))
            c += w
    return geoms


def build_generic_geometry(dev: Device, per_row: int = 16) -> List[KeyGeom]:
    """Any device: one block of squares per zone, with the zone's name as a header row."""
    geoms: List[KeyGeom] = []
    y = 0.0
    for z in dev.zones:
        if z.leds_count == 0:
            continue
        if z.matrix:
            geoms += build_matrix_geometry(dev, z, y)
            y += len(z.matrix) + 0.6
            continue
        for n in range(z.leds_count):
            led = z.start_idx + n
            label = label_for(dev.led_names[led]) if led < len(dev.led_names) else str(n + 1)
            if label.startswith(z.name):
                label = label[len(z.name) :].strip(" ,") or str(n + 1)
            geoms.append(KeyGeom(led, (n % per_row) * 1.0, y + (n // per_row), 1.0, 1.0, label))
        y += (z.leds_count + per_row - 1) // per_row + 0.6
    return geoms


def build_geometry(dev: Device) -> List[KeyGeom]:
    if dev.type_name == "Keyboard":
        g = build_keyboard_geometry(dev)
        if g:
            return g
    return build_generic_geometry(dev)
