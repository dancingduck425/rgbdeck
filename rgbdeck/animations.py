# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Animations the app renders itself and plays on every device at once.

render() is a pure function of time: all devices are given the same clock, so a wave leaves the keyboard and
arrives on the strip behind the desk in step. Positions are normalised per device (x and y in 0..1); a device
with a single LED sits at the centre.
"""

from __future__ import annotations

import colorsys
import math
from typing import List, Sequence, Tuple

from rgbdeck.models import rgb_to_u32, u32_to_rgb

# kind, label, what it does
KINDS: List[Tuple[str, str, str]] = [
    ("wave", "Rainbow wave", "A rainbow travelling across all your devices"),
    ("cycle", "Color cycle", "Everything drifts through the colors together"),
    ("breathe", "Breathing", "Your own colors fade out and back in"),
    ("aurora", "Aurora", "Slow green, teal and violet curtains"),
    ("fire", "Fire", "Flickering embers"),
    ("twinkle", "Starlight", "Your colors dimmed, with stars glinting across them"),
]
NAMES = {k: label for k, label, _tip in KINDS}
TAU = 2 * math.pi


def _hsv(h: float, s: float, v: float) -> int:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return rgb_to_u32(int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))


def _scaled(color: int, k: float) -> int:
    r, g, b = u32_to_rgb(color)
    return rgb_to_u32(int(r * k + 0.5), int(g * k + 0.5), int(b * k + 0.5))


def _noise(n: int) -> float:
    """A fixed pseudo-random number in 0..1 for an integer (the same on every run)."""
    n = (n * 374761393 + 668265263) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((n ^ (n >> 16)) & 0xFFFF) / 65535.0


def rate(speed: int) -> float:
    """Cycles per second for the speed slider (1..100): a slow drift up to a lively run."""
    return 0.03 * (40.0 ** (max(1, min(100, speed)) / 100.0))


def render(
    kind: str, t: float, xs: Sequence[float], ys: Sequence[float], base: Sequence[int], speed: int = 50
) -> List[int]:
    """One frame: a color per LED. base = the device's own colors (some animations build on them)."""
    n = len(xs)
    w = rate(speed)
    if kind == "wave":
        return [_hsv(xs[i] * 0.85 - t * w, 1.0, 1.0) for i in range(n)]
    if kind == "cycle":
        c = _hsv(t * w * 0.5, 1.0, 1.0)
        return [c] * n
    if kind == "breathe":
        k = 0.06 + 0.94 * (0.5 - 0.5 * math.cos(TAU * t * w)) ** 1.6
        src = base if any(base) else [0xFFFFFF] * n
        return [_scaled(src[i] if i < len(src) else 0, k) for i in range(n)]
    if kind == "aurora":
        out = []
        for i in range(n):
            x, y = xs[i], ys[i]
            hue = (
                0.42
                + 0.17 * math.sin(TAU * (x * 0.9 + t * w * 0.21))
                + 0.10 * math.sin(TAU * (x * 2.1 - t * w * 0.13) + y * 2.0)
            )
            val = 0.55 + 0.45 * math.sin(TAU * (x * 0.7 - t * w * 0.17) + y * 1.5 + math.sin(t * w * 0.9))
            out.append(_hsv(hue, 0.85, 0.12 + 0.88 * max(0.0, val)))
        return out
    if kind == "fire":
        out = []
        f = 2.0 + w * 3.0
        for i in range(n):
            x, y = xs[i], ys[i]
            heat = (
                0.52 + 0.26 * math.sin(t * f * 1.9 + x * 23.0 + y * 5.0) + 0.22 * math.sin(t * f * 3.1 + x * 57.0 + 1.3)
            )
            heat = max(0.0, min(1.0, heat * (0.75 + 0.5 * y)))  # the bottom rows burn hotter
            level = 0.25 + 0.75 * heat
            out.append(rgb_to_u32(int(255 * level), int(150 * heat**1.6 * level), int(30 * heat**3 * level)))
        return out
    if kind == "twinkle":
        out = []
        src = base if any(base) else [rgb_to_u32(40, 60, 160)] * n
        period = 1.0 / max(0.05, w * 0.6)
        for i in range(n):
            salt = int(xs[i] * 977) * 131 + int(ys[i] * 613) * 17 + i
            phase = (t + _noise(salt) * period * 4.0) / period
            cycle = int(phase)
            lit = _noise(salt * 31 + cycle) < 0.22  # roughly one LED in five glints per period
            glint = math.sin(math.pi * (phase - cycle)) ** 3 if lit else 0.0
            r, g, b = u32_to_rgb(src[i] if i < len(src) else 0)
            k = 0.22
            out.append(rgb_to_u32(*(int(c * k + (255 - c * k) * glint + 0.5) for c in (r, g, b))))
        return out
    return list(base)


def normalise(points: Sequence[Tuple[float, float]]) -> Tuple[List[float], List[float]]:
    """LED centres in any unit -> x and y lists in 0..1 (a device that is one point wide sits at 0.5)."""
    if not points:
        return [], []
    px = [p[0] for p in points]
    py = [p[1] for p in points]
    x0, y0 = min(px), min(py)
    wx, wy = max(px) - x0, max(py) - y0
    return [((x - x0) / wx if wx > 1e-9 else 0.5) for x in px], [((y - y0) / wy if wy > 1e-9 else 0.5) for y in py]
