# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
A few numbers about the PC that a device can show as a color: CPU temperature, GPU temperature, CPU load.

    CPU temperature   Linux: /sys/class/hwmon (coretemp, k10temp, zenpower, cpu_thermal). Windows has no
                      unprivileged source, so it is not offered there.
    GPU temperature   nvidia-smi where it exists (both systems), else the amdgpu / nouveau hwmon entry.
    CPU load          /proc/stat on Linux, GetSystemTimes on Windows.

Reading is done by one background thread every couple of seconds while somebody is interested (nvidia-smi
takes a noticeable moment); value() just returns the latest number.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("rgbdeck.sensors")

HWMON = "/sys/class/hwmon"
CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "zenergy")
GPU_CHIPS = ("amdgpu", "nouveau")
INTERVAL = 2.0
# source -> (label, unit, value shown as "cool", value shown as "hot")
SOURCES: Dict[str, Tuple[str, str, float, float]] = {
    "cpu": ("CPU temperature", "°C", 40.0, 85.0),
    "gpu": ("GPU temperature", "°C", 40.0, 85.0),
    "load": ("CPU load", "%", 5.0, 90.0),
}


def _hwmon_temp(chips, root: str = HWMON) -> Optional[float]:
    best = None
    for d in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        try:
            with open(os.path.join(d, "name")) as f:
                if f.read().strip() not in chips:
                    continue
            for path in glob.glob(os.path.join(d, "temp*_input")):
                with open(path) as f:
                    v = int(f.read().strip()) / 1000.0
                if 0.0 < v < 150.0 and (best is None or v > best):
                    best = v
        except (OSError, ValueError):
            continue
    return best


def cpu_temp(root: str = HWMON) -> Optional[float]:
    return _hwmon_temp(CPU_CHIPS, root) if sys.platform != "win32" else None


def gpu_temp(root: str = HWMON) -> Optional[float]:
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # no console window
            out = subprocess.run(
                [exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                **flags,
            ).stdout
            temps = [float(x) for x in out.replace(",", " ").split() if x.replace(".", "", 1).isdigit()]
            if temps:
                return max(temps)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return _hwmon_temp(GPU_CHIPS, root) if sys.platform != "win32" else None


class _Load:
    """CPU load in percent since the previous call."""

    def __init__(self):
        self._prev: Optional[Tuple[int, int]] = None

    def _times(self) -> Optional[Tuple[int, int]]:  # (busy, total)
        if sys.platform == "win32":
            import ctypes

            idle, kernel, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            total = kernel.value + user.value  # kernel time includes idle time
            return total - idle.value, total
        try:
            with open("/proc/stat") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts) - idle, sum(parts)

    def read(self) -> Optional[float]:
        now = self._times()
        prev, self._prev = self._prev, now
        if now is None or prev is None or now[1] <= prev[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (now[0] - prev[0]) / (now[1] - prev[1])))


_lock = threading.Lock()
_values: Dict[str, Optional[float]] = {}
_wanted: set = set()
_thread: Optional[threading.Thread] = None
_wake = threading.Event()
_load = _Load()
_READERS = {"cpu": cpu_temp, "gpu": gpu_temp, "load": _load.read}


def _run():
    global _thread
    while True:
        with _lock:
            wanted = set(_wanted)
            if not wanted:
                _thread = None
                return
        for src in wanted:
            try:
                v = _READERS[src]()
            except Exception:  # noqa: BLE001
                log.exception("sensor %s failed", src)
                v = None
            with _lock:
                _values[src] = v
        _wake.wait(INTERVAL)
        _wake.clear()


def watch(sources: List[str]):
    """Say which sources are being shown right now (an empty list stops the reading thread)."""
    global _thread
    with _lock:
        _wanted.clear()
        _wanted.update(s for s in sources if s in _READERS)
        start = bool(_wanted) and _thread is None
        if start:
            _thread = threading.Thread(target=_run, name="rgbdeck-sensors", daemon=True)
    if start:
        _thread.start()
    _wake.set()


def value(source: str) -> Optional[float]:
    with _lock:
        return _values.get(source)


def level(source: str) -> Optional[float]:
    """The latest value as 0..1 between the source's "cool" and "hot" marks (None: no reading)."""
    v = value(source)
    if v is None or source not in SOURCES:
        return None
    _label, _unit, lo, hi = SOURCES[source]
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def offered() -> List[str]:
    """Sources that can work on this machine."""
    out = []
    if sys.platform != "win32" and cpu_temp() is not None:
        out.append("cpu")
    if shutil.which("nvidia-smi") or (sys.platform != "win32" and _hwmon_temp(GPU_CHIPS) is not None):
        out.append("gpu")
    out.append("load")
    return out
