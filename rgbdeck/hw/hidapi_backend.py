# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
hidapi backend (Windows, macOS): the same enumerate_hid() / HidDevice surface as the Linux hidraw module.

Windows exposes every top-level HID collection as its own device. A USB interface that carries several
report types - a Logitech keyboard has the HID++ long report 0x11 on usage 0xFF43/0x0602 and the very-long
0x12 on 0xFF43/0x0604 - therefore shows up as several hidapi entries, while Linux hands the drivers one node
per interface. The drivers are written for the Linux shape, so this module restores it: entries are grouped
back by interface into one HidInfo (descriptor = the collections' descriptors concatenated), and one
HidDevice opens every collection of the interface, routes each write / feature report to the collection that
owns its report ID, and merges the collections' input reports into one read queue.

Set RGBDECK_HIDAPI_MODULE=hidraw to run this backend on Linux through hidapi's hidraw build (testing).
"""

from __future__ import annotations

import importlib
import logging
import os
import queue
import re
import threading
from typing import Dict, List, Optional, Set, Tuple

from rgbdeck.hw.hidinfo import HidInfo, parse_collections
from rgbdeck.models import DeviceError

log = logging.getLogger("rgbdeck.hidapi")

_hid = None
_hid_error = ""
try:
    _hid = importlib.import_module(os.environ.get("RGBDECK_HIDAPI_MODULE", "hid"))
    if not hasattr(_hid, "enumerate") or not hasattr(_hid, "device"):
        _hid, _hid_error = None, "the installed 'hid' module is not cython-hidapi (pip install hidapi)"
except ImportError as e:
    _hid_error = f"the 'hidapi' Python package is not installed (pip install hidapi): {e}"

# \\?\hid#vid_046d&pid_c33c&mi_01&col03#7&2b4c1234&0&0002#{guid}: every collection carries its own index both in
# the hardware id (col03) and in the last field of the instance id (&0002), so both must go to find the interface.
_COL_RE = re.compile(r"&col[0-9a-f]+(#[0-9a-f]+&[0-9a-f]+&[0-9a-f]+)&[0-9a-f]+#", re.I)
_COL_ONLY_RE = re.compile(r"&col[0-9a-f]+", re.I)
VENDOR_PAGE = 0xFF00  # usage pages >= this are vendor-defined: the collections the drivers talk to


def _lib():
    if _hid is None:
        raise DeviceError(_hid_error)
    return _hid


def _group_key(path: str) -> str:
    r"""\\?\hid#vid_046d&pid_c33c&mi_01&col03#7&2b4c...&0&0002#{guid} -> same string without the collection index."""
    key, n = _COL_RE.subn(r"\1#", path)
    if n:
        return key
    key, n = _COL_ONLY_RE.subn("", path)
    return re.sub(r"&[0-9a-f]+#\{", "#{", key, flags=re.I) if n else key  # odd instance id: still drop its last field


class _Collection:
    def __init__(self, path: str, page: int, usage: int):
        self.path, self.page, self.usage = path, page, usage
        self.descriptor = b""
        self.rids: Set[int] = set()

    def synthetic_descriptor(self) -> bytes:
        """Minimal stand-in when the real descriptor can't be read: Usage Page + Usage + an application collection."""
        return bytes(
            [0x06, self.page & 0xFF, self.page >> 8, 0x0A, self.usage & 0xFF, self.usage >> 8, 0xA1, 0x01, 0xC0]
        )


_GROUPS: Dict[str, List[_Collection]] = {}  # group key -> collections, refreshed by enumerate_hid


def _read_descriptor(col: _Collection) -> bytes:
    dev = _lib().device()
    try:
        dev.open_path(col.path.encode() if isinstance(col.path, str) else col.path)
        try:
            desc = bytes(dev.get_report_descriptor())
        finally:
            dev.close()
    except Exception as e:  # old hidapi without get_report_descriptor, or an unopenable collection
        log.debug("no descriptor for %s: %s", col.path, e)
        return b""
    return desc


def enumerate_hid(vid: Optional[int] = None, pid: Optional[int] = None, with_descriptors: bool = True) -> List[HidInfo]:
    groups: Dict[str, Tuple[int, int, str, int, List[_Collection]]] = {}
    order: List[str] = []
    for e in _lib().enumerate(vid or 0, pid or 0):
        v, p = e["vendor_id"], e["product_id"]
        if (vid is not None and v != vid) or (pid is not None and p != pid):
            continue
        path = e["path"]
        if isinstance(path, bytes):
            path = path.decode(errors="replace")
        key = _group_key(path)
        if key not in groups:
            # Linux names devices "<manufacturer> <product>"; match it so presets carry across platforms
            product, maker = e.get("product_string") or "", e.get("manufacturer_string") or ""
            name = product if not maker or product.lower().startswith(maker.lower()) else f"{maker} {product}"
            groups[key] = (v, p, name, e.get("interface_number", -1), [])
            order.append(key)
        groups[key][4].append(_Collection(path, e.get("usage_page") or 0, e.get("usage") or 0))
    out = []
    for key in order:
        v, p, name, iface, cols = groups[key]
        if with_descriptors:
            seen: Set[bytes] = set()
            for c in cols:
                # Windows hands out a system keyboard / mouse collection without read/write access, but
                # its preparsed data (= the descriptor) and feature reports still work on that handle.
                c.descriptor = _read_descriptor(c)
                if c.descriptor:
                    for page, usage, rids in parse_collections(c.descriptor):
                        if (page, usage) == (c.page, c.usage) or len(cols) == 1:
                            c.rids |= rids
                else:
                    c.descriptor = c.synthetic_descriptor()
                seen.add(c.descriptor)
            desc = b"".join(sorted(seen, key=len))  # dedupe: hidapi's Linux build returns the whole node each time
        else:
            desc = b""
        if with_descriptors or key not in _GROUPS:  # a cheap hotplug poll must not drop the known report ids
            _GROUPS[key] = cols
        out.append(HidInfo(key, v, p, name, iface if iface is not None else -1, desc))
    return out


class HidDevice:
    """One USB interface: every openable collection behind one write/read surface.

    Vendor collections carry the lighting protocol; a keyboard / mouse collection opens on Windows without
    read/write access, which is still enough for the feature reports some drivers (Razer ...) send there."""

    def __init__(self, path: str):
        self.path = path
        cols = _GROUPS.get(path)
        if cols is None:
            enumerate_hid()
            cols = _GROUPS.get(path)
        if not cols:
            raise DeviceError(f"can't open {path}: device vanished")
        self._handles: List[Tuple[_Collection, object]] = []
        errors = []
        opened: Dict[str, object] = {}  # hidapi's Linux build lists one node per collection: open each node once
        # vendor collections first so an unknown report id is tried where it most likely belongs
        for c in sorted(cols, key=lambda c: c.page < VENDOR_PAGE):
            if c.path in opened:
                self._handles.append((c, opened[c.path]))
                continue
            dev = _lib().device()
            try:
                dev.open_path(c.path.encode() if isinstance(c.path, str) else c.path)
            except Exception as e:
                errors.append(f"{c.path}: {e}")
                continue
            opened[c.path] = dev
            self._handles.append((c, dev))
        if not self._handles:
            raise DeviceError(
                f"can't open {path}: {'; '.join(errors) or 'no collection'}. "
                "Another program is probably holding the device (close the vendor software: G HUB, "
                "Armoury Crate, Glorious Core, iCUE, Synapse ...)."
            )
        self._route: Dict[int, object] = {}
        for c, dev in self._handles:
            for rid in c.rids:
                self._route.setdefault(rid, dev)
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._reader, args=(dev,), name=f"rgbdeck-hid-{i}", daemon=True)
            for i, dev in enumerate(opened.values())
        ]
        for t in self._threads:
            t.start()

    # -------------------------------------------------------------- plumbing
    def _reader(self, dev):
        failures = 0
        while not self._stop.is_set():
            try:
                buf = dev.read(1024, 100)  # hidapi copies min(len, report size): never clip a 65-byte report
            except Exception:
                if self._stop.is_set():
                    return
                failures += 1
                if failures >= 3:
                    return  # a handle without read access (system keyboard / mouse collection on Windows)
                self._stop.wait(0.1)
                continue
            failures = 0
            if buf:
                self._q.put(bytes(buf))

    def _target(self, report_id: int):
        dev = self._route.get(report_id)
        if dev is not None:
            return [dev]
        # unknown report id (or none at all): try every collection, remember the winner
        return list({id(d): d for _c, d in self._handles}.values())

    def _dispatch(self, report_id: int, what: str, fn) -> object:
        last = None
        for dev in self._target(report_id):
            try:
                r = fn(dev)
            except Exception as e:  # hidapi raises IOError/ValueError; Windows rejects a foreign report id
                last = e
                continue
            if r is not None and not isinstance(r, bool) and r is not False and (not isinstance(r, int) or r >= 0):
                self._route.setdefault(report_id, dev)
                return r
            last = last or DeviceError("hidapi returned an error")
        raise DeviceError(f"{what} to {self.path} failed: {last}")

    # -------------------------------------------------------------- HidDevice surface
    def close(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=0.5)
        for dev in {id(d): d for _c, d in self._handles}.values():
            try:
                dev.close()
            except Exception:
                pass
        self._handles = []

    def write(self, data: bytes) -> int:
        return int(self._dispatch(data[0] if data else 0, "write", lambda d: d.write(bytes(data))))

    def read(self, size: int = 64, timeout: float = 0.5) -> Optional[bytes]:
        try:
            buf = self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        return buf[:size]

    def drain(self):
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def send_feature(self, data: bytes):
        self._dispatch(data[0] if data else 0, "feature report", lambda d: d.send_feature_report(bytes(data)))

    def get_feature(self, report_id: int, size: int) -> bytes:
        r = self._dispatch(report_id, "feature read", lambda d: d.get_feature_report(report_id, size))
        buf = bytes(r)
        return buf + bytes(max(0, size - len(buf)))


__all__ = ["HidInfo", "HidDevice", "enumerate_hid"]
