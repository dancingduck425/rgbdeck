# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Pictures for devices that are not keyboards: a mouse drawn as a mouse with its lights where they sit, a light
strip as a strip, a headset as two ear cups, a mouse mat with its lit edge, a mainboard as a board.

Nothing here draws. A builder returns a Layout: the LED rectangles (KeyGeom, as for keyboards) plus a backdrop,
a list of drawing steps in the same units that the canvas in app.py paints behind them:

    ("path", [("M", x, y), ("C", x1, y1, x2, y2, x, y), ("L", x, y), ("Z",)], style)
    ("rrect", x, y, w, h, radius, style)      ("ellipse", cx, cy, rx, ry, style)
    ("line", x1, y1, x2, y2, style)           ("text", x, y, text, style)

    style   body      the device itself          inset    a recess in it (wheel slot, CPU socket)
            detail    a thin line on it          tape     the band of a light strip
            band      a thick line (headband, the bend of a strip)

Where a device's lights really sit is not something its protocol tells, so the pictures go by what the lights
are called (Logo, Scroll Wheel, Left 2, Rear ...) and fall back to a sensible spot. KeyGeom.pos, where set, is
where a light counts as being for effects and gradients: a long strip is drawn folded into rows, but a wave
still runs along it from one end to the other.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from rgbdeck.keyboard_layout import KeyGeom, Layout, label_for
from rgbdeck.models import Device, Zone

ZONE_LINEAR, ZONE_MATRIX = 1, 2
STRIP_ROW = 20  # lights per row of a folded strip
STRIP_TYPES = ("LED Strip", "Light")


def _name(dev: Device, zone: Zone, led: int) -> str:
    return dev.led_names[led] if led < len(dev.led_names) and dev.led_names[led] else zone.name


def _short(dev: Device, zone: Zone, led: int, n: int) -> str:
    """What to print on a light: its name without the zone's, else its number."""
    full = _name(dev, zone, led)
    for prefix in (zone.name, zone.name.rstrip("s")):
        if prefix and full.startswith(prefix) and len(full) > len(prefix):
            return label_for(full[len(prefix) :].strip(" ,")) or str(n + 1)
    return label_for(full) if zone.leds_count == 1 else str(n + 1)


def _leds(zone: Zone) -> range:
    return range(zone.start_idx, zone.start_idx + zone.leds_count)


def _has_matrix(dev: Device) -> bool:
    return any(z.matrix for z in dev.zones if z.leds_count)


# ----------------------------------------------------------------------------- mouse
MOUSE_W, MOUSE_H, MOUSE_PAD = 6.0, 10.0, 0.7


def _mouse_backdrop(ox: float, oy: float) -> list:
    def pt(x: float, y: float) -> Tuple[float, float]:
        return ox + x, oy + y

    outline = [
        ("M", *pt(3.0, 0.0)),
        ("C", *pt(4.6, 0.0), *pt(5.7, 0.9), *pt(5.7, 2.6)),
        ("C", *pt(5.7, 4.2), *pt(5.35, 4.8), *pt(5.45, 6.0)),
        ("C", *pt(5.6, 8.4), *pt(4.9, 10.0), *pt(3.0, 10.0)),
        ("C", *pt(1.1, 10.0), *pt(0.4, 8.4), *pt(0.55, 6.0)),
        ("C", *pt(0.65, 4.8), *pt(0.3, 4.2), *pt(0.3, 2.6)),
        ("C", *pt(0.3, 0.9), *pt(1.4, 0.0), *pt(3.0, 0.0)),
        ("Z",),
    ]
    seam = [("M", *pt(0.42, 3.9)), ("C", *pt(2.0, 4.35), *pt(4.0, 4.35), *pt(5.58, 3.9))]
    return [
        ("path", outline, "body"),
        ("path", seam, "detail"),
        ("line", *pt(3.0, 0.05), *pt(3.0, 4.2), "detail"),
        ("rrect", *pt(2.62, 1.05), 0.76, 1.8, 0.38, "inset"),
    ]


def _row(leds: Sequence[Tuple[int, str]], cx: float, y: float, h: float, room: float, widest: float) -> List[KeyGeom]:
    """Lights side by side, centred on cx."""
    if not leds:
        return []
    w = min(widest, room / len(leds))
    x0 = cx - w * len(leds) / 2
    return [KeyGeom(led, x0 + i * w, y, w, h, label, "pill") for i, (led, label) in enumerate(leds)]


def _column(leds: Sequence[Tuple[int, str]], x: float, w: float, y0: float, room: float) -> List[KeyGeom]:
    if not leds:
        return []
    h = min(1.3, room / len(leds))
    top = y0 + (room - h * len(leds)) / 2
    return [KeyGeom(led, x, top + i * h, w, h, label, "pill") for i, (led, label) in enumerate(leds)]


def build_mouse(dev: Device) -> Optional[Layout]:
    if _has_matrix(dev):
        return None
    top, mid, low, left, right, ring = [], [], [], [], [], []
    for zone in dev.zones:
        if zone.type == ZONE_LINEAR and zone.leds_count >= 4:
            ring += [(led, str(n + 1)) for n, led in enumerate(_leds(zone))]
            continue
        for n, led in enumerate(_leds(zone)):
            name = _name(dev, zone, led).lower()
            item = (led, _short(dev, zone, led, n))
            if "left" in name or "right" in name:  # on a side: the side itself need not be printed on it
                rest = name.replace("left", "").replace("right", "").replace("side", "").strip(" ,")
                (left if "left" in name else right).append((led, rest[:3]))
            elif any(k in name for k in ("wheel", "scroll", "front", "reactive", "top")):
                top.append(item)
            elif any(k in name for k in ("logo", "rear", "back", "palm", "bottom")):
                low.append(item)
            else:
                mid.append(item)
    if not (top or mid or low or left or right or ring):
        return None
    ox = oy = MOUSE_PAD
    cx = ox + MOUSE_W / 2
    geoms: List[KeyGeom] = []
    if len(top) == 1 and any(k in _name(dev, dev.zones[0], top[0][0]).lower() for k in ("wheel", "scroll")):
        geoms.append(KeyGeom(top[0][0], ox + 2.55, oy + 0.98, 0.9, 1.94, "", "pill"))  # the wheel itself
    else:
        geoms += _row(top, cx, oy + 1.15, 1.5, 3.4, 1.5)
    geoms += _row(mid, cx, oy + 4.55, 1.3, 3.0, 1.6)
    geoms += _row(low, cx, oy + 6.5, 1.7, 3.0, 1.8)
    geoms += _column(left, ox + 0.7, 0.95, oy + 2.4, 5.6)
    geoms += _column(right, ox + MOUSE_W - 1.65, 0.95, oy + 2.4, 5.6)
    if ring:  # underglow: down the left side, round the back, up the right side
        rx, ry, cy = 2.45, 4.25, oy + 5.25
        a0, a1 = math.radians(160), math.radians(380)
        steps = 400  # evenly spaced along the curve, not by angle (that would crowd them at the back)
        curve = [
            (cx + rx * math.cos(a0 + (a1 - a0) * i / steps), cy - ry * math.sin(a0 + (a1 - a0) * i / steps))
            for i in range(steps + 1)
        ]
        run = [0.0]
        for (x1, y1), (x2, y2) in zip(curve, curve[1:]):
            run.append(run[-1] + math.hypot(x2 - x1, y2 - y1))
        gap = run[-1] / len(ring)
        size = min(0.8, gap * 0.7)
        at = 0
        for i, (led, label) in enumerate(ring):
            want = gap * (i + 0.5)
            while run[at] < want:
                at += 1
            x, y = curve[at]
            geoms.append(KeyGeom(led, x - size / 2, y - size / 2, size, size, label if size > 0.6 else "", "dot"))
    size = (MOUSE_W + 2 * MOUSE_PAD, MOUSE_H + 2 * MOUSE_PAD)
    return Layout(geoms, _mouse_backdrop(ox, oy), size, "mouse")


# ----------------------------------------------------------------------------- mouse mat
def _around(n: int, x: float, y: float, w: float, h: float) -> List[Tuple[float, float]]:
    """n points spread evenly round a rectangle, clockwise from its top left corner."""
    total = 2 * (w + h)
    out = []
    for i in range(n):
        d = total * (i + 0.5) / n
        if d < w:
            out.append((x + d, y))
        elif d < w + h:
            out.append((x + w, y + d - w))
        elif d < 2 * w + h:
            out.append((x + w - (d - w - h), y + h))
        else:
            out.append((x, y + h - (d - 2 * w - h)))
    return out


def build_mousemat(dev: Device) -> Optional[Layout]:
    if _has_matrix(dev):
        return None
    w, h, pad = 14.0, 9.0, 0.8
    edge, inner = [], []
    for zone in dev.zones:
        target = edge if zone.type == ZONE_LINEAR or zone.leds_count > 1 else inner
        target += [(led, _short(dev, zone, led, n)) for n, led in enumerate(_leds(zone))]
    if not edge and len(inner) > 1:
        edge, inner = inner, []  # a few separately named edge zones
    if not (edge or inner):
        return None
    geoms: List[KeyGeom] = []
    if 0 < len(edge) <= 3:  # a lit strip along the top edge, in two or three parts
        part = (w - 1.0) / len(edge)
        geoms += [
            KeyGeom(led, pad + 0.5 + i * part, pad - 0.35, part, 0.7, lb, "pill") for i, (led, lb) in enumerate(edge)
        ]
    elif edge:
        size = min(0.9, 2 * (w + h) / len(edge) * 0.9)
        for (led, label), (x, y) in zip(edge, _around(len(edge), pad, pad, w, h)):
            geoms.append(KeyGeom(led, x - size / 2, y - size / 2, size, size, label if size > 0.6 else "", "dot"))
    geoms += _row(inner, pad + w / 2, pad + h / 2 - 0.8, 1.6, 6.0, 2.2)
    backdrop = [
        ("rrect", pad, pad, w, h, 0.7, "body"),
        ("rrect", pad + 0.6, pad + 0.6, w - 1.2, h - 1.2, 0.4, "detail"),
    ]
    return Layout(geoms, backdrop, (w + 2 * pad, h + 2 * pad), "mousemat")


# ----------------------------------------------------------------------------- headset
def _headset_backdrop() -> list:
    band = [("M", 1.7, 5.0), ("C", 1.7, -0.9, 8.3, -0.9, 8.3, 5.0)]
    return [
        ("path", band, "band"),
        ("rrect", 0.5, 4.3, 2.4, 4.0, 1.1, "body"),
        ("rrect", 7.1, 4.3, 2.4, 4.0, 1.1, "body"),
    ]


def build_headset(dev: Device) -> Optional[Layout]:
    if _has_matrix(dev):
        return None
    left, right, other = [], [], []
    for zone in dev.zones:
        for n, led in enumerate(_leds(zone)):
            name = _name(dev, zone, led).lower()
            item = (led, _short(dev, zone, led, n))
            (left if "left" in name else right if "right" in name else other).append(item)
    if not (left or right or other):
        return None
    if not left and not right:
        if len(other) == 1:
            left, right, other = [other[0]], [other[0]], []  # one light: both ear cups show it
        else:
            half = (len(other) + 1) // 2
            left, right, other = other[:half], other[half:], []
    geoms: List[KeyGeom] = []
    for side, x in ((left, 0.5), (right, 7.1)):
        if not side:
            continue
        d = min(1.5, 3.2 / len(side))
        y0 = 6.3 - d * len(side) / 2
        geoms += [
            KeyGeom(led, x + 1.2 - d / 2, y0 + i * d, d, d, lb if d > 1.0 else "", "dot")
            for i, (led, lb) in enumerate(side)
        ]
    geoms += _row(other, 5.0, 5.6, 1.2, 3.4, 1.4)
    return Layout(geoms, _headset_backdrop(), (10.0, 9.0), "headset")


# ----------------------------------------------------------------------------- light strip
def build_strip(dev: Device) -> Optional[Layout]:
    zones = [z for z in dev.zones if z.leds_count]
    if not zones or _has_matrix(dev):
        return None
    total = sum(z.leds_count for z in zones)
    if total == 1:  # a lamp
        led = zones[0].start_idx
        backdrop = [("ellipse", 3.0, 3.0, 2.6, 2.6, "body"), ("ellipse", 3.0, 3.0, 2.05, 2.05, "inset")]
        return Layout([KeyGeom(led, 1.2, 1.2, 3.6, 3.6, "", "dot")], backdrop, (6.0, 6.0), "lamp")
    geoms: List[KeyGeom] = []
    backdrop: list = []
    pitch, y, width = 1.5, 0.3, 0.0
    titled = len(zones) > 1
    for zi, zone in enumerate(zones):
        n = zone.leds_count
        rows = (n + STRIP_ROW - 1) // STRIP_ROW
        per_row = (n + rows - 1) // rows
        if titled:
            backdrop.append(("text", 0.3, y + 0.45, zone.name, "text"))
            y += 0.8
        for r in range(rows):
            count = min(per_row, n - r * per_row)
            backdrop.append(("rrect", 0.0, y - 0.15, count + 0.6, 1.3, 0.3, "tape"))
            if r + 1 < rows:  # the bend to the next row, on the side this row ends at
                x = per_row + 0.6 if r % 2 == 0 else 0.0
                out = 0.9 if r % 2 == 0 else -0.9
                bend = [("M", x, y + 0.5), ("C", x + out, y + 0.5, x + out, y + pitch + 0.5, x, y + pitch + 0.5)]
                backdrop.append(("path", bend, "band"))
            for c in range(count):
                k = r * per_row + c
                col = c if r % 2 == 0 else per_row - 1 - c  # folded: every second row runs back
                label = str(k + 1) if n <= 40 else ""
                geoms.append(KeyGeom(zone.start_idx + k, 0.3 + col, y, 1.0, 1.0, label, "dot", (float(k), float(zi))))
            y += pitch
        width = max(width, per_row + 0.6 + (0.9 if rows > 1 else 0.0))
        y += 0.5
    for g in geoms:
        g.x += 0.9  # room for the bends on the left
    shifted = []
    for step in backdrop:
        if step[0] == "rrect":
            shifted.append((step[0], step[1] + 0.9, *step[2:]))
        elif step[0] == "text":
            shifted.append((step[0], step[1] + 0.9, *step[2:]))
        else:
            shifted.append(
                (
                    "path",
                    [(c[0], *[v + 0.9 if i % 2 == 0 else v for i, v in enumerate(c[1:])]) for c in step[1]],
                    step[2],
                )
            )
    return Layout(geoms, shifted, (width + 1.8, y - 0.3), "strip")


# ----------------------------------------------------------------------------- mainboard
def _board_backdrop() -> list:
    steps = [
        ("rrect", 0.3, 0.3, 11.4, 12.0, 0.45, "body"),
        ("rrect", 0.6, 1.0, 1.3, 4.6, 0.2, "inset"),  # rear connectors
        ("rrect", 3.6, 2.0, 2.8, 2.8, 0.25, "inset"),  # CPU socket
        ("rrect", 4.2, 2.6, 1.6, 1.6, 0.1, "detail"),
        ("rrect", 7.9, 8.2, 2.0, 2.0, 0.3, "inset"),  # chipset
    ]
    steps += [("rrect", 7.3 + i * 0.5, 1.4, 0.24, 5.0, 0.1, "inset") for i in range(4)]  # memory slots
    steps += [("rrect", 1.4, 7.5 + i * 1.25, 5.6, 0.3, 0.12, "inset") for i in range(3)]  # card slots
    return steps


def build_mainboard(dev: Device) -> Optional[Layout]:
    if _has_matrix(dev):
        return None
    leds = [(led, _short(dev, z, led, n)) for z in dev.zones for n, led in enumerate(_leds(z))]
    if not leds:
        return None
    n = len(leds)
    geoms: List[KeyGeom] = []
    right, bottom = 10.6, 9.0  # the lit edge: down the right side, then along the bottom
    if n <= 11:
        pts = [(10.75, 0.95 + i * 0.95) for i in range(n)]
        size = 0.85
    else:
        step = (right + bottom) / n
        size = min(0.85, step * 0.92)
        pts = []
        for i in range(n):
            d = step * (i + 0.5)
            pts.append((10.75, 0.85 + d) if d < right else (9.9 - (d - right), 11.55))
    for (led, label), (x, y) in zip(leds, pts):
        geoms.append(KeyGeom(led, x - size / 2, y - size / 2, size, size, label if size > 0.7 else "", "dot"))
    return Layout(geoms, _board_backdrop(), (12.0, 12.6), "mainboard")


# ----------------------------------------------------------------------------- which picture for which device
def build(dev: Device) -> Optional[Layout]:
    """The picture for this device, or None when the plain grid serves it better."""
    kind = dev.type_name
    if kind == "Mouse":
        return build_mouse(dev)
    if kind == "Mousemat":
        return build_mousemat(dev)
    if kind == "Headset":
        return build_headset(dev)
    if kind == "Motherboard":
        return build_mainboard(dev)
    zones = [z for z in dev.zones if z.leds_count]
    if kind in STRIP_TYPES or (zones and all(z.type == ZONE_LINEAR and z.leds_count >= 2 for z in zones)):
        return build_strip(dev)
    return None


def glyph(type_name: str) -> Optional[Layout]:
    """The outline of a device without separately addressable lights (it is filled with its effect's color)."""
    if type_name == "Mouse":
        return Layout(
            [], _mouse_backdrop(MOUSE_PAD, MOUSE_PAD), (MOUSE_W + 2 * MOUSE_PAD, MOUSE_H + 2 * MOUSE_PAD), "mouse"
        )
    if type_name == "Headset":
        return Layout([], _headset_backdrop(), (10.0, 9.0), "headset")
    if type_name == "Mousemat":
        return Layout([], [("rrect", 0.8, 0.8, 14.0, 9.0, 0.7, "body")], (15.6, 10.6), "mousemat")
    if type_name == "Motherboard":
        return Layout([], _board_backdrop(), (12.0, 12.6), "mainboard")
    if type_name in STRIP_TYPES:
        return Layout([], [("rrect", 0.5, 0.5, 12.0, 1.3, 0.3, "body")], (13.0, 2.3), "strip")
    return None
