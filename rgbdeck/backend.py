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

from rgbdeck.models import Device, DeviceError, Mode
from rgbdeck.hw import hidraw
from rgbdeck.hw import logitech_hidpp, glorious_gmow, asus_aura_usb

log = logging.getLogger("rgbdeck.backend")

NATIVE_DRIVERS = (logitech_hidpp, glorious_gmow, asus_aura_usb)


class LocalBackend:
    def __init__(self):
        self.devices: List[Device] = []
        self._drivers: Dict[int, object] = {}
        self._seen: set = set()
        self.last_error = ""
        self._io_lock = threading.Lock()
        self._cv = threading.Condition()
        self._jobs: "OrderedDict[Tuple[int, str], tuple]" = OrderedDict()
        self._busy = False
        self._errors: List[str] = []
        self._stop = False
        self._worker = threading.Thread(target=self._run, name="rgbdeck-io", daemon=True)
        self._worker.start()

    # ---- worker ---------------------------------------------------------------------
    def _run(self):
        while True:
            with self._cv:
                while not self._jobs and not self._stop:
                    self._cv.wait()
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
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def _fingerprint(self) -> set:
        return {(i.path, i.vid, i.pid) for i in hidraw.enumerate_hid()}

    def refresh(self):
        """Re-enumerate everything. Devices still present keep their colors/mode state."""
        old = {(d.name, d.location): d for d in self.devices}
        self.close()
        errors = []
        with self._io_lock:
            for mod in NATIVE_DRIVERS:
                try:
                    for drv in mod.probe():
                        self._adopt(drv, old)
                except DeviceError as e:
                    errors.append(str(e))
                except Exception as e:  # noqa: BLE001
                    log.exception("probe failed in %s", mod.__name__)
                    errors.append(f"{mod.__name__}: {e}")
        self.last_error = "; ".join(errors)
        self._seen = self._fingerprint()
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
        except OSError:
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

    def battery(self, dev: Device) -> Optional[Tuple[int, bool]]:
        drv = self._drivers.get(dev.index)
        if drv and hasattr(drv, "battery"):
            self.wait_idle(1.0)
            with self._io_lock:
                return drv.battery()
        return None
