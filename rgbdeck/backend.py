# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Local hardware backend.

- Native drivers (rgbdeck.hw.*) talk straight to /dev/hidraw. Each exposes probe() -> [driver],
  where a driver has .device, set_colors(), set_mode(), optional read_colors()/battery(), close().
- All writes go through one worker thread, coalesced per (device, kind), so the UI never blocks.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import importlib
import pkgutil

from rgbdeck.models import Device, DeviceError, Mode
from rgbdeck.hw import hidraw
import rgbdeck.hw as hw_pkg

log = logging.getLogger("rgbdeck.backend")

# Drivers probed first, in this order (the owner-tested ones); every other module under rgbdeck.hw
# that exposes probe() is discovered automatically and probed afterwards.
PREFERRED_DRIVERS = ("logitech_hidpp", "glorious_gmow", "asus_aura_usb")  # verified on real hardware
KEEPALIVE_INTERVAL = 5.0
# pkgutil can't list a PyInstaller bundle; this is the fallback (keep in sync with rgbdeck/hw/).
FROZEN_DRIVER_MODULES = (
    "logitech_hidpp",
    "glorious_gmow",
    "asus_aura_usb",
    "razer",
    "logitech_mouse",
    "corsair_peripheral",
    "corsair_lighting_node",
    "steelseries",
    "hyperx",
    "govee_lan",
    "wled",
)


def discover_drivers():
    """All driver modules under rgbdeck.hw with a probe() function."""
    mods = {}
    names = [i.name for i in pkgutil.iter_modules(hw_pkg.__path__)] or list(FROZEN_DRIVER_MODULES)
    for name in names:
        if name in ("hidraw", "hidinfo", "hidapi_backend") or name.endswith("_devices"):
            continue
        try:
            mod = importlib.import_module(f"rgbdeck.hw.{name}")
        except Exception as e:  # noqa: BLE001 - a broken driver must not stop the others
            log.error("driver module %s failed to import: %s", name, e)
            continue
        if callable(getattr(mod, "probe", None)):
            mods[name] = mod
    ordered = [mods[n] for n in PREFERRED_DRIVERS if n in mods]
    ordered += [m for n, m in sorted(mods.items()) if n not in PREFERRED_DRIVERS]
    return ordered


NATIVE_DRIVERS = ()  # filled lazily by discover_drivers()


class LocalBackend:
    def __init__(self):
        self.devices: List[Device] = []
        self._drivers: Dict[int, object] = {}
        self._seen: set = set()
        self._net_mods: list = []  # driver modules with their own presence check (network lights)
        self.last_error = ""
        self._io_lock = threading.Lock()
        self._cv = threading.Condition()
        self._jobs: "OrderedDict[Tuple[int, str], tuple]" = OrderedDict()
        self._busy = False
        self._errors: List[str] = []
        self._last_touch: Dict[int, float] = {}  # driver id -> last write/keepalive time
        self._stop = False
        self._worker = threading.Thread(target=self._run, name="rgbdeck-io", daemon=True)
        self._worker.start()

    # ---- worker ---------------------------------------------------------------------
    def _run(self):
        while True:
            with self._cv:
                while not self._jobs and not self._stop:
                    if not self._cv.wait(self._next_keepalive_wait()):
                        self._keepalive_locked()
                if self._stop:
                    return
                _key, job = self._jobs.popitem(last=False)
                self._busy = True
            try:
                with self._io_lock:
                    kind, dev, payload = job
                    drv = self._drivers.get(dev.index)
                    if drv is None or drv.device is not dev:
                        raise DeviceError(f"{dev.name} is gone")
                    if kind == "leds":
                        drv.set_colors(list(payload))
                    else:
                        mode, save = payload
                        drv.set_mode(mode, save)
                    self._last_touch[id(drv)] = time.monotonic()
            except DeviceError as e:
                log.warning("device error: %s", e)
                with self._cv:
                    self._errors.append(str(e))
            except Exception as e:  # noqa: BLE001 - a driver bug must not kill the worker
                log.exception("driver crashed")
                with self._cv:
                    self._errors.append(f"{type(e).__name__}: {e}")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _keepalive_drivers(self):
        return [d for d in self._drivers.values() if callable(getattr(d, "keepalive", None))]

    def _next_keepalive_wait(self) -> float:
        """How long the worker may sleep before some driver needs its heartbeat."""
        now = time.monotonic()
        soonest = KEEPALIVE_INTERVAL
        for d in self._keepalive_drivers():
            interval = max(0.02, float(getattr(d, "keepalive_interval", KEEPALIVE_INTERVAL)))
            due = self._last_touch.get(id(d), 0.0) + interval - now
            soonest = min(soonest, max(0.005, due))
        return soonest

    def _keepalive_locked(self):
        """Called from the worker (holding _cv) when idle: ping drivers whose heartbeat interval elapsed."""
        now = time.monotonic()
        due = [
            d
            for d in self._keepalive_drivers()
            if now - self._last_touch.get(id(d), 0.0)
            >= max(0.02, float(getattr(d, "keepalive_interval", KEEPALIVE_INTERVAL)))
        ]
        if not due:
            return
        self._busy = True
        self._cv.release()
        try:
            with self._io_lock:
                for d in due:
                    try:
                        d.keepalive()
                    except DeviceError as e:
                        log.warning("keepalive failed: %s", e)
                    except Exception:  # noqa: BLE001
                        log.exception("keepalive crashed")
                    self._last_touch[id(d)] = time.monotonic()
        finally:
            self._cv.acquire()
            self._busy = False
            self._cv.notify_all()

    def _enqueue(self, key: Tuple[int, str], job: tuple):
        with self._cv:
            self._jobs[key] = job
            self._cv.notify_all()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._jobs or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True

    def take_error(self) -> str:
        with self._cv:
            if not self._errors:
                return ""
            msg = self._errors[-1]
            self._errors.clear()
            return msg

    # ---- lifecycle --------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return True

    def connect(self):
        self.refresh()

    def close(self):
        self.wait_idle(2.0)
        with self._cv:
            self._jobs.clear()
        with self._io_lock:
            for drv in self._drivers.values():
                try:
                    drv.close()
                except Exception:  # noqa: BLE001
                    pass
            self._drivers.clear()
            self.devices = []

    def shutdown(self):
        self.close()
        for mod in self._net_mods:  # network drivers may hold connections / threads of their own
            if callable(getattr(mod, "shutdown", None)):
                try:
                    mod.shutdown()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown failed in %s", mod.__name__)
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def _fingerprint(self, adopted: bool = False) -> set:
        """What is plugged in / on the network right now; a change means "rescan".

        Drivers for things that are not USB (network lights) add to it through two module functions:
        fingerprint() = present now, probed() = what the last probe() handed out. adopted=True asks for
        the latter, so a light that answered a moment too late for the probe still triggers a rescan."""
        try:
            fp = {(i.path, i.vid, i.pid) for i in hidraw.enumerate_hid(with_descriptors=False)}
        except DeviceError:
            fp = set()
        for mod in self._net_mods:
            fn = getattr(mod, "probed" if adopted and callable(getattr(mod, "probed", None)) else "fingerprint")
            try:
                fp |= {(mod.__name__, x) for x in fn()}
            except Exception:  # noqa: BLE001 - presence checks must never take the poll down
                log.exception("presence check failed in %s", mod.__name__)
        return fp

    def refresh(self):
        """Re-enumerate everything. Devices still present keep their colors/mode state."""
        old = {(d.name, d.location): d for d in self.devices}
        self.close()
        errors = []
        with self._io_lock:
            mods = discover_drivers()
            self._net_mods = [m for m in mods if callable(getattr(m, "fingerprint", None))]
            for mod in mods:
                experimental = bool(
                    getattr(mod, "EXPERIMENTAL", mod.__name__.rsplit(".", 1)[-1] not in PREFERRED_DRIVERS)
                )
                try:
                    for drv in mod.probe():
                        drv.device.experimental = experimental
                        self._adopt(drv, old)
                except DeviceError as e:
                    errors.append(str(e))
                except Exception as e:  # noqa: BLE001
                    log.exception("probe failed in %s", mod.__name__)
                    errors.append(f"{mod.__name__}: {e}")
        self.last_error = "; ".join(errors)
        self._seen = self._fingerprint(adopted=True)
        log.info("devices: %s", [f"{d.type_name}:{d.name}" for d in self.devices])

    def _adopt(self, drv, old):
        dev = drv.device
        dev.index = len(self.devices)
        prev = old.get((dev.name, dev.location))
        if prev is not None:
            dev.active_mode = prev.active_mode if prev.active_mode < len(dev.modes) else 0
            if len(prev.colors) == len(dev.colors):
                dev.colors = list(prev.colors)
            for m_new in dev.modes:
                m_old = prev.mode_named(m_new.name)
                if m_old:
                    m_new.speed, m_new.brightness, m_new.direction = m_old.speed, m_old.brightness, m_old.direction
                    m_new.color_mode, m_new.colors = m_old.color_mode, list(m_old.colors)
        self.devices.append(dev)
        self._drivers[dev.index] = drv

    def poll_notifications(self) -> bool:
        try:
            return self._fingerprint() != self._seen
        except (OSError, DeviceError):
            return False

    def driver_for(self, dev: Device):
        return self._drivers.get(dev.index)

    # ---- commands ----------------------------------------------------------------------
    def update_leds(self, dev: Device, colors: List[int]):
        if dev.index not in self._drivers:
            raise DeviceError(f"{dev.name} is gone")
        dev.colors = list(colors)
        self._enqueue((dev.index, "leds"), ("leds", dev, list(colors)))

    def update_mode(self, dev: Device, mode: Mode, save: bool = False):
        if dev.index not in self._drivers:
            raise DeviceError(f"{dev.name} is gone")
        dev.active_mode = mode.index
        self._enqueue((dev.index, "mode"), ("mode", dev, (mode, save)))

    def read_colors(self, dev: Device) -> Optional[List[int]]:
        drv = self._drivers.get(dev.index)
        if drv and hasattr(drv, "read_colors"):
            self.wait_idle()
            with self._io_lock:
                return drv.read_colors()
        return None

    def resize_zone(self, dev: Device, zone_index: int, count: int) -> bool:
        """For zones with leds_min != leds_max (LED strips / fan hubs): set the LED count on the driver."""
        drv = self._drivers.get(dev.index)
        if not drv or not callable(getattr(drv, "resize_zone", None)):
            return False
        self.wait_idle()
        with self._io_lock:
            drv.resize_zone(zone_index, count)
        return True

    def battery(self, dev: Device) -> Optional[Tuple[int, bool]]:
        drv = self._drivers.get(dev.index)
        if drv and hasattr(drv, "battery"):
            self.wait_idle(1.0)
            with self._io_lock:
                return drv.battery()
        return None
