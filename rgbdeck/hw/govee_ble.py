# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Bluetooth LE transport for Govee lights: the way in when a light cannot be reached over the LAN API
(LAN Control switched off, a Bluetooth-only model, another network). govee_lan.py builds the packets and owns
the driver; this module only finds lights, keeps a connection to each and delivers 20-byte packets.

    advertised name      Govee_H618F_2A24  (also ihoment_ / GBK_ / Minger_ / GVH_): model and the last two
                         bytes of the light's id
    service              00010203-0405-0607-0809-0a0b0c0d1910
    write                ...2b11  (20 bytes, last byte = XOR of the others)
    notify               ...2b10 / 2b11: every packet is answered with one that starts with the same two bytes

Verified on an H618F (firmware 3.04.35): the strip handles about ten colour packets a second and drops the
connection when it is flooded. Packets therefore go out strictly one at a time, each waiting for its answer.
A light also hangs up on a silent connection, so a state query goes out every couple of seconds; its answers
(power, brightness) double as the "somebody else changed the light" signal.

Qt's Bluetooth module does the radio work (BlueZ on Linux, WinRT on Windows, CoreBluetooth on macOS). All
Qt objects live in one thread with its own event loop; the rest of the program talks to it through Link
objects and a condition variable. Without a Q(Core)Application, or without QtBluetooth, available() is False
and nothing here does anything.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

log = logging.getLogger("rgbdeck.govee.ble")

NAME_RE = re.compile(r"^(?:Govee|ihoment|GBK|GVH|Minger)_(H[0-9A-Z]{4,5})_([0-9A-F]{4})$", re.I)
SERVICE = "{00010203-0405-0607-0809-0a0b0c0d1910}"
WRITE_CHAR = "{00010203-0405-0607-0809-0a0b0c0d2b11}"
NOTIFY_CHAR = "{00010203-0405-0607-0809-0a0b0c0d2b10}"

SCAN_MS = 5000  # one look around
ACK_TIMEOUT = 0.45  # a packet nobody answered counts as delivered after this long
POLL_INTERVAL = 2.0  # state query on an idle connection (also keeps the light from hanging up)
RETRY_DELAYS = (0.8, 2.5, 6.0)  # further connection attempts before a light counts as out of reach
MAX_SILENT = 6  # unanswered packets in a row: the connection is dead, whatever the radio says
ADVERT_FRESH = 60.0


class Advert:
    """One Govee light heard on the air."""

    def __init__(self, key: str, name: str, sku: str, suffix: str):
        self.key, self.name, self.sku, self.suffix = key, name, sku, suffix
        self.rssi = 0
        self.seen = 0.0


class Link:
    """Connection to one light, usable from any thread."""

    def __init__(self, advert: Advert):
        self.key, self.name, self.sku, self.suffix = advert.key, advert.name, advert.sku, advert.suffix
        self.state = "connecting"  # connecting | ready | gone
        self.power: Optional[bool] = None
        self.brightness: Optional[int] = None
        self.status_at = 0.0  # when power / brightness were last reported by the light
        self.replies: Dict[bytes, bytes] = {}  # first three bytes of a query -> the light's answer
        self._queue: Deque[bytes] = deque()
        self._handed = 0
        self._done = 0

    def send(self, packets: List[bytes], wait: bool = True) -> bool:
        """Deliver packets in order, one at a time. Blocks until the light answered each (or stopped
        answering); False when the connection is not up."""
        radio = _radio
        with _cv:
            if self.state != "ready" or radio is None:
                return False
            self._queue.extend(bytes(p) for p in packets)
            self._handed += len(packets)
            target = self._handed
        radio.nudge(self.key)
        if not wait:
            return True
        deadline = time.monotonic() + 1.0 + len(packets) * ACK_TIMEOUT
        with _cv:
            while self._done < target and self.state == "ready":
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                _cv.wait(left)
            return self._done >= target

    def ask(self, packet: bytes) -> Optional[bytes]:
        """Send a query (aa ..) and return the light's 20-byte answer."""
        key = _reply_key(packet)
        with _cv:
            self.replies.pop(key, None)
        if not self.send([packet]):
            return None
        with _cv:
            return self.replies.get(key)


_cv = threading.Condition()
_adverts: Dict[str, Advert] = {}
_links: Dict[str, Link] = {}
_radio = None  # the _Radio object once start() succeeded
_thread = None
_want: Callable[[Advert], bool] = lambda advert: False  # noqa: E731 - which lights to connect to
_scan = {"running": False, "finished": 0.0, "error": ""}
_qt = None  # False: QtBluetooth is not usable here


def checksum(body: bytes) -> bytes:
    """One packet: 19 bytes, zero padded, plus the XOR of them."""
    raw = bytearray(bytes(body[:19]).ljust(19, b"\x00"))
    x = 0
    for b in raw:
        x ^= b
    raw.append(x)
    return bytes(raw)


def _reply_key(packet: bytes) -> bytes:
    """Queries and their answers share the first two bytes; segment queries (aa a5 n) also the third."""
    return bytes(packet[:3]) if packet[1] == 0xA5 else bytes(packet[:2]) + b"\x00"


def parse_name(name: str):
    """(model, id suffix) for a Govee advertising name, else None."""
    m = NAME_RE.match(name or "")
    return (m.group(1).upper(), m.group(2).upper()) if m else None


# ----------------------------------------------------------------------------- Qt side
def _load_qt():
    global _qt
    if _qt is None:
        if os.environ.get("RGBDECK_NO_BLUETOOTH"):
            _qt = False
        else:
            try:
                from PySide6 import QtBluetooth, QtCore

                _qt = (QtCore, QtBluetooth)
            except Exception as e:  # noqa: BLE001 - a build without the module simply has no Bluetooth
                log.info("Bluetooth support not available: %s", e)
                _qt = False
    return _qt


def _make_radio():
    QtCore, QtBluetooth = _load_qt()
    QObject, QTimer, Signal, Slot = QtCore.QObject, QtCore.QTimer, QtCore.Signal, QtCore.Slot
    QByteArray, QUuid = QtCore.QByteArray, QtCore.QUuid
    Agent = QtBluetooth.QBluetoothDeviceDiscoveryAgent
    Controller = QtBluetooth.QLowEnergyController
    Service = QtBluetooth.QLowEnergyService
    Uuid = QtBluetooth.QBluetoothUuid

    class _Radio(QObject):
        sig_setup = Signal()
        sig_scan = Signal()
        sig_nudge = Signal(str)
        sig_drop = Signal(str)
        sig_stop = Signal()

        def __init__(self):
            super().__init__()
            self._agent = None
            self._infos = {}  # key -> QBluetoothDeviceInfo
            self._rt: Dict[str, dict] = {}  # key -> controller, service, pending packet, timers
            self._stopped = False
            self.sig_setup.connect(self._setup)
            self.sig_scan.connect(self._start_scan)
            self.sig_nudge.connect(self._pump)
            self.sig_drop.connect(self._drop)
            self.sig_stop.connect(self._stop)

        # thread-safe entry points -------------------------------------------------
        def nudge(self, key: str):
            self.sig_nudge.emit(key)

        # everything below runs in the radio thread ----------------------------------
        @Slot()
        def _setup(self):
            self._agent = Agent(self)
            self._agent.setLowEnergyDiscoveryTimeout(SCAN_MS)
            self._agent.deviceDiscovered.connect(self._heard)
            self._agent.deviceUpdated.connect(lambda info, _fields: self._heard(info))
            self._agent.finished.connect(self._scan_over)
            self._agent.canceled.connect(self._scan_over)
            self._agent.errorOccurred.connect(self._scan_error)
            self._poll = QTimer(self)
            self._poll.timeout.connect(self._poll_links)
            self._poll.start(500)

        @Slot()
        def _start_scan(self):
            if self._stopped or self._agent is None or self._agent.isActive():
                return
            with _cv:
                _scan["running"], _scan["error"] = True, ""
            self._agent.start(Agent.DiscoveryMethod.LowEnergyMethod)

        def _scan_over(self):
            with _cv:
                _scan["running"], _scan["finished"] = False, time.monotonic()
                _cv.notify_all()

        def _scan_error(self, err):
            text = self._agent.errorString() or str(err)
            log.info("Bluetooth scan: %s", text)
            with _cv:
                _scan["running"], _scan["finished"], _scan["error"] = False, time.monotonic(), text
                _cv.notify_all()

        def _heard(self, info):
            parsed = parse_name(info.name())
            if not parsed:
                return
            address = info.address().toString()
            key = address if address and address != "00:00:00:00:00:00" else info.deviceUuid().toString()
            with _cv:
                advert = _adverts.get(key)
                if advert is None:
                    advert = _adverts[key] = Advert(key, info.name(), parsed[0], parsed[1])
                    log.info("Bluetooth: heard %s (%s, %d dBm)", info.name(), key, info.rssi())
                advert.rssi, advert.seen = info.rssi(), time.monotonic()
                wanted = key not in _links
                _cv.notify_all()
            self._infos[key] = info
            if wanted and not self._stopped:
                try:
                    wanted = bool(_want(advert))
                except Exception:  # noqa: BLE001
                    log.exception("Bluetooth policy check failed")
                    wanted = False
                if wanted:
                    with _cv:
                        link = _links[key] = Link(advert)
                    self._connect(link, 0)

        # connections ----------------------------------------------------------------
        def _connect(self, link: Link, attempt: int):
            info = self._infos.get(link.key)
            if info is None or self._stopped or _links.get(link.key) is not link:
                return
            ctl = Controller.createCentral(info, self)
            rt = {"ctl": ctl, "svc": None, "wr": None, "pending": None, "counts": False, "silent": 0}
            rt.update(attempt=attempt, last_poll=0.0, poll_n=0, dead=False)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._ack_timeout(link.key, rt))
            rt["timer"] = timer
            self._rt[link.key] = rt
            ctl.connected.connect(lambda: ctl.discoverServices())
            ctl.discoveryFinished.connect(lambda: self._services(link, rt))
            ctl.errorOccurred.connect(lambda err: self._lost(link, rt, f"error {err}"))
            ctl.disconnected.connect(lambda: self._lost(link, rt, "disconnected"))
            # a light that is out of range may leave the attempt hanging for half a minute
            QTimer.singleShot(8000, lambda: None if rt["svc"] is not None else self._lost(link, rt, "timeout"))
            ctl.connectToDevice()

        def _services(self, link: Link, rt: dict):
            if rt["dead"]:
                return
            svc = rt["ctl"].createServiceObject(Uuid(QUuid(SERVICE)), rt["ctl"])
            if svc is None:
                self._lost(link, rt, "no Govee service")
                return
            rt["svc"] = svc
            svc.stateChanged.connect(lambda state: self._service_state(link, rt, state))
            svc.characteristicChanged.connect(lambda _c, value: self._notified(link, rt, bytes(value.data())))
            svc.errorOccurred.connect(lambda err: log.debug("Bluetooth %s: service error %s", link.name, err))
            svc.discoverDetails()

        def _service_state(self, link: Link, rt: dict, state):
            if rt["dead"] or state != Service.ServiceState.RemoteServiceDiscovered or rt["wr"] is not None:
                return
            svc = rt["svc"]
            wr = svc.characteristic(Uuid(QUuid(WRITE_CHAR)))
            if not wr.isValid():
                self._lost(link, rt, "no write characteristic")
                return
            rt["wr"] = wr
            cccd = Uuid(Uuid.DescriptorType.ClientCharacteristicConfiguration)
            for uuid in (NOTIFY_CHAR, WRITE_CHAR):
                desc = svc.characteristic(Uuid(QUuid(uuid))).descriptor(cccd)
                if desc.isValid():
                    svc.writeDescriptor(desc, QByteArray(b"\x01\x00"))
            log.info("Bluetooth: connected to %s", link.name)
            rt["attempt"] = 0
            with _cv:
                link.state = "ready"
                _cv.notify_all()
            self._pump(link.key)

        def _lost(self, link: Link, rt: dict, why: str):
            if rt["dead"]:
                return
            rt["dead"] = True
            rt["timer"].stop()
            if self._rt.get(link.key) is rt:
                del self._rt[link.key]
            ctl = rt["ctl"]
            try:
                ctl.disconnectFromDevice()
            except RuntimeError:
                pass
            ctl.deleteLater()
            was_ready = link.state == "ready"
            attempt = 0 if was_ready else rt["attempt"] + 1
            with _cv:
                link._queue.clear()
                link._done = link._handed
                current = _links.get(link.key) is link
                retry = current and not self._stopped and attempt <= len(RETRY_DELAYS)
                if retry:
                    link.state = "connecting"
                else:
                    link.state = "gone"
                    if current:
                        del _links[link.key]
                _cv.notify_all()
            log.info("Bluetooth: %s %s%s", link.name, why, ", trying again" if retry else "")
            if retry:
                delay = RETRY_DELAYS[max(0, attempt - 1)]
                QTimer.singleShot(int(delay * 1000), lambda: self._connect(link, attempt))

        @Slot(str)
        def _drop(self, key: str):
            with _cv:
                link = _links.pop(key, None)
            rt = self._rt.get(key)
            if link is not None and rt is not None:
                self._lost(link, rt, "released")
            elif link is not None:
                with _cv:
                    link.state = "gone"
                    _cv.notify_all()

        # packets --------------------------------------------------------------------
        @Slot(str)
        def _pump(self, key: str):
            rt = self._rt.get(key)
            link = _links.get(key)
            if rt is None or link is None or rt["wr"] is None or rt["pending"] is not None or rt["dead"]:
                return
            with _cv:
                packet = link._queue.popleft() if link._queue else None
            if packet is None:
                return
            self._write(rt, packet, counts=True)

        def _write(self, rt: dict, packet: bytes, counts: bool):
            rt["pending"], rt["counts"] = packet, counts
            rt["svc"].writeCharacteristic(rt["wr"], QByteArray(packet), Service.WriteMode.WriteWithoutResponse)
            rt["timer"].start(int(ACK_TIMEOUT * 1000))
            rt["last_poll"] = time.monotonic()  # any traffic keeps the connection up

        def _finish(self, link: Link, rt: dict):
            counted = rt["counts"]
            rt["pending"] = None
            rt["timer"].stop()
            if counted:
                with _cv:
                    link._done += 1
                    _cv.notify_all()
            self._pump(link.key)

        def _notified(self, link: Link, rt: dict, data: bytes):
            if len(data) < 3 or rt["dead"]:
                return
            now = time.monotonic()
            with _cv:
                if data[0] == 0xAA:
                    link.replies[_reply_key(data)] = data
                    if data[1] == 0x01:
                        link.power, link.status_at = bool(data[2]), now
                    elif data[1] == 0x04:
                        link.brightness, link.status_at = data[2], now
            pending = rt["pending"]
            if pending is None or data[:2] != pending[:2]:
                return
            if data[0] == 0xAA and data[1] == 0xA5 and data[2] != pending[2]:
                return
            rt["silent"] = 0
            self._finish(link, rt)

        def _ack_timeout(self, key: str, rt: dict):
            link = _links.get(key)
            if link is None or rt["dead"] or rt["pending"] is None:
                return
            rt["silent"] += 1
            if rt["silent"] >= MAX_SILENT:
                self._lost(link, rt, "stopped answering")
                return
            self._finish(link, rt)

        def _poll_links(self):
            now = time.monotonic()
            for key, rt in list(self._rt.items()):
                link = _links.get(key)
                if link is None or rt["wr"] is None or rt["pending"] is not None or rt["dead"]:
                    continue
                if link._queue or now - rt["last_poll"] < POLL_INTERVAL:
                    continue
                rt["poll_n"] += 1
                self._write(rt, checksum(bytes([0xAA, 0x01 if rt["poll_n"] % 2 else 0x04])), counts=False)

        @Slot()
        def _stop(self):
            self._stopped = True
            if self._agent is not None and self._agent.isActive():
                self._agent.stop()
            for key in list(self._rt):
                self._drop(key)
            with _cv:
                for link in _links.values():
                    link.state = "gone"
                _links.clear()
                _cv.notify_all()
            self.thread().quit()

    return _Radio()


# ----------------------------------------------------------------------------- module API
def available() -> bool:
    """Can this process use Bluetooth at all (QtBluetooth present and a Qt application object exists)?"""
    qt = _load_qt()
    return bool(qt) and qt[0].QCoreApplication.instance() is not None


def start(want: Callable[[Advert], bool]) -> bool:
    """Bring the radio thread up (idempotent). want(advert) decides which lights get a connection."""
    global _radio, _thread, _want
    _want = want
    if _radio is not None:
        return True
    if not available():
        return False
    QtCore = _qt[0]
    radio = _make_radio()
    thread = QtCore.QThread()
    thread.setObjectName("rgbdeck-govee-ble")
    radio.moveToThread(thread)
    thread.start()
    radio.sig_setup.emit()
    _radio, _thread = radio, thread
    return True


def stop():
    """Hang up everywhere and end the radio thread. Must run before the Qt application object goes away."""
    global _radio, _thread
    radio, thread = _radio, _thread
    _radio = _thread = None
    if radio is None:
        return
    radio.sig_stop.emit()
    if not thread.wait(3000):
        thread.terminate()
        thread.wait(1000)
    with _cv:
        _links.clear()
        _adverts.clear()
        _scan.update(running=False, finished=0.0, error="")
        _cv.notify_all()


atexit.register(stop)


def scan() -> bool:
    """Start one look around (about five seconds); lights that are wanted get connected as they turn up."""
    if _radio is None:
        return False
    with _cv:
        _scan["running"] = True
    _radio.sig_scan.emit()
    return True


def scan_age() -> float:
    """Seconds since the last scan ended (a huge number if there never was one; 0 while one is running)."""
    with _cv:
        if _scan["running"]:
            return 0.0
        return time.monotonic() - _scan["finished"] if _scan["finished"] else 1e9


def scanning() -> bool:
    with _cv:
        return bool(_scan["running"])


def scan_error() -> str:
    """Why the last scan failed (Bluetooth switched off, no adapter), or ""."""
    with _cv:
        return _scan["error"]


def drop(key: str):
    """Hang up on one light and forget its connection."""
    if _radio is not None:
        _radio.sig_drop.emit(key)


def links(state: Optional[str] = None) -> List[Link]:
    with _cv:
        return [ln for ln in _links.values() if state is None or ln.state == state]


def adverts(fresh: float = ADVERT_FRESH) -> List[Advert]:
    """Lights heard within the last `fresh` seconds."""
    now = time.monotonic()
    with _cv:
        return [a for a in _adverts.values() if now - a.seen <= fresh]


def wait(deadline: float, done: Callable[[], bool]) -> bool:
    """Sleep until done() holds (checked whenever something changes) or the deadline passes."""
    with _cv:
        while not done():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _cv.wait(min(left, 0.25))
    return True
