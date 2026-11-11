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

On Windows the radio does not run in this process at all. Qt's Bluetooth backend there ends the whole process
when Windows refuses one of its calls (radio switched off or taken away, suspend and resume, a restarted
Bluetooth service) and has been seen to hang on Windows 11. So the program starts itself a second time as a
small helper (helper_main) that holds the radio, and talks to it over its standard input and output, one
JSON object per line. When the helper dies or stops answering, the lights it served count as gone, the reason
goes into the log, and everything else carries on. RGBDECK_BLE_HELPER=1 / 0 forces either way on any system.

    to the helper     {"op": "scan"} · {"op": "connect", "key"} · {"op": "send", "key", "packets": [hex]}
                      {"op": "drop", "key"} · {"op": "stop"}
    from the helper   {"ev": "heard", "key", "name", "sku", "suffix", "rssi"} · {"ev": "scan", "running", "error"}
                      {"ev": "link", "key", "state"} · {"ev": "rx", "key", "data": hex} · {"ev": "done", "key"}
                      {"ev": "log", "level", "msg"} · {"ev": "tick"} · {"ev": "fatal", "error"}
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import sys
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
SCAN_GRACE = 10.0  # a scan that has not reported its end this long after it should have counts as over
HELPER_SILENCE = 20.0  # the helper reports in every two seconds; this long without a word and it is replaced
HELPER_CRASHES = 3  # helpers lost in a row before Bluetooth is left alone until the program is restarted
HELPER_STEADY = 600.0  # a helper that ran this long had a good run: its loss does not count towards the limit


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
_radio = None  # the _Radio (this process) or _RemoteRadio (helper process) once start() succeeded
_want: Callable[[Advert], bool] = lambda advert: False  # noqa: E731 - which lights to connect to
_scan = {"running": False, "started": 0.0, "finished": 0.0, "error": ""}
_qt = None  # False: QtBluetooth is not usable here
_notify: Optional[Callable[..., None]] = None  # set in the helper process: tells the main program what happened
_helper = {"crashes": 0, "retry_at": 0.0, "disabled": ""}
_zombies: list = []  # radio threads that did not end in time: kept referenced, never killed


def checksum(body: bytes) -> bytes:
    """One packet: 19 bytes, zero padded, plus the XOR of them."""
    raw = bytearray(bytes(body[:19]).ljust(19, b"\x00"))
    x = 0
    for b in raw:
        x ^= b
    raw.append(x)
    return bytes(raw)


def _tell(ev: str, **fields):
    if _notify is not None:
        _notify(ev, **fields)


def _absorb(link: "Link", data: bytes, now: float):
    """Note what a light reported (call with _cv held)."""
    if data[0] == 0xAA:
        link.replies[_reply_key(data)] = data
        if data[1] == 0x01:
            link.power, link.status_at = bool(data[2]), now
        elif data[1] == 0x04:
            link.brightness, link.status_at = data[2], now


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
        sig_connect = Signal(str)
        sig_stop = Signal()

        def __init__(self):
            super().__init__()
            self._agent = None
            self._infos = {}  # key -> QBluetoothDeviceInfo
            self._rt: Dict[str, dict] = {}  # key -> controller, service, pending packet, timers
            self._stopped = False
            self._starting = False
            self._told: Dict[str, float] = {}  # key -> when the main program last heard of this advert
            self._ticks = 0
            self.qthread = None
            self.sig_setup.connect(self._setup)
            self.sig_scan.connect(self._start_scan)
            self.sig_nudge.connect(self._pump)
            self.sig_drop.connect(self._drop)
            self.sig_connect.connect(self._adopt)
            self.sig_stop.connect(self._stop)

        # thread-safe entry points -------------------------------------------------
        def nudge(self, key: str):
            self.sig_nudge.emit(key)

        def request_scan(self):
            self.sig_scan.emit()

        def request_drop(self, key: str):
            self.sig_drop.emit(key)

        def request_connect(self, key: str):
            self.sig_connect.emit(key)

        def shutdown(self):
            thread = self.qthread
            self.sig_stop.emit()
            if thread is not None and not thread.wait(3000):
                # Killing a thread that is inside the system's Bluetooth calls can take the process with it.
                log.warning("Bluetooth thread did not end in time; leaving it be")
                _zombies.append((self, thread))

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
            # On Windows start() handles events while it waits for the system, so a second request can arrive
            # in the middle of the first: _starting keeps that one out.
            if self._stopped or self._agent is None or self._starting or self._agent.isActive():
                return
            self._starting = True
            with _cv:
                _scan["running"], _scan["started"], _scan["error"] = True, time.monotonic(), ""
            _tell("scan", running=True, error="")
            try:
                self._agent.start(Agent.DiscoveryMethod.LowEnergyMethod)
            finally:
                self._starting = False

        def _scan_over(self):
            with _cv:
                _scan["running"], _scan["finished"] = False, time.monotonic()
                error = _scan["error"]
                _cv.notify_all()
            _tell("scan", running=False, error=error)

        def _scan_error(self, err):
            text = self._agent.errorString() or str(err)
            log.info("Bluetooth scan: %s", text)
            with _cv:
                _scan["running"], _scan["finished"], _scan["error"] = False, time.monotonic(), text
                _cv.notify_all()
            _tell("scan", running=False, error=text)

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
            if _notify is not None and advert.seen - self._told.get(key, 0.0) >= 3.0:
                self._told[key] = advert.seen
                _tell("heard", key=key, name=advert.name, sku=advert.sku, suffix=advert.suffix, rssi=advert.rssi)
            if wanted and not self._stopped:
                try:
                    wanted = bool(_want(advert))
                except Exception:  # noqa: BLE001
                    log.exception("Bluetooth policy check failed")
                    wanted = False
                if wanted:
                    self._adopt(key)

        @Slot(str)
        def _adopt(self, key: str):
            """Start a connection to a light that was heard."""
            with _cv:
                advert = _adverts.get(key)
                if advert is None or key in _links or self._stopped:
                    return
                link = _links[key] = Link(advert)
            _tell("link", key=key, state="connecting")
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
            _tell("link", key=link.key, state="ready")
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
            _tell("link", key=link.key, state=link.state)
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
                _tell("link", key=key, state="gone")

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
                _tell("done", key=link.key)
            self._pump(link.key)

        def _notified(self, link: Link, rt: dict, data: bytes):
            if len(data) < 3 or rt["dead"]:
                return
            with _cv:
                _absorb(link, data, time.monotonic())
            if data[0] == 0xAA:
                _tell("rx", key=link.key, data=data.hex())
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
            self._ticks += 1
            if self._ticks % 4 == 0:
                _tell("tick")
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
            self._poll.stop()
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


# ----------------------------------------------------------------------------- the radio in a helper process
def _exit_text(code: int) -> str:
    if sys.platform == "win32":
        return f"exit code 0x{code & 0xFFFFFFFF:08X}"
    if code < 0:
        import signal

        try:
            return f"signal {signal.Signals(-code).name}"
        except ValueError:
            return f"signal {-code}"
    return f"exit code {code}"


def _end_process(proc):
    """End the helper for certain. In the packaged Windows build it is two processes (the unpacker and the
    program it started), and ending only the first would leave the second holding the radio."""
    if sys.platform == "win32":
        import subprocess

        try:
            subprocess.run(  # noqa: S603, S607
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


class _RemoteRadio:
    """The radio held by a helper process (see the module text). Takes the same requests as _Radio and keeps
    the adverts, links and scan state of this process in step with what the helper reports."""

    def __init__(self, proc, fault_file: str = ""):
        self.proc = proc
        self.fault_file = fault_file
        self.started = time.monotonic()
        self.last_op = "start"
        self.stopping = False
        self.why = ""  # why this side ended the helper, if it did
        self.rx_count = 0
        self._wlock = threading.RLock()

    def run(self):
        threading.Thread(target=self._read, name="rgbdeck-ble-helper", daemon=True).start()
        threading.Thread(target=self._watch, name="rgbdeck-ble-watch", daemon=True).start()

    # requests ---------------------------------------------------------------------
    def _tx(self, op: str, **fields):
        fields["op"] = op
        self.last_op = f"{op} {fields.get('key', '')}".strip()
        try:
            with self._wlock:
                self.proc.stdin.write((json.dumps(fields) + "\n").encode())
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass  # the helper is gone: the reader thread deals with that

    def nudge(self, key: str):
        with self._wlock:  # taking the packets and writing them is one step, so their order holds
            with _cv:
                link = _links.get(key)
                packets = list(link._queue) if link is not None else []
                if link is not None:
                    link._queue.clear()
            if packets:
                self._tx("send", key=key, packets=[p.hex() for p in packets])

    def request_scan(self):
        self._tx("scan")

    def request_drop(self, key: str):
        self._tx("drop", key=key)

    def request_connect(self, key: str):
        self._tx("connect", key=key)

    def shutdown(self):
        import subprocess

        self.stopping = True
        self._tx("stop")
        with self._wlock:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=4.0)
        except subprocess.TimeoutExpired:
            _end_process(self.proc)

    # what the helper reports ------------------------------------------------------
    def handle(self, msg: dict):
        self.rx_count += 1
        ev, key = msg.get("ev"), str(msg.get("key", ""))
        if ev == "tick":
            return
        if ev == "log":
            log.log(int(msg.get("level", logging.INFO)), "%s", msg.get("msg", ""))
        elif ev == "heard":
            with _cv:
                advert = _adverts.get(key)
                if advert is None:
                    advert = _adverts[key] = Advert(key, str(msg["name"]), str(msg["sku"]), str(msg["suffix"]))
                advert.rssi, advert.seen = int(msg.get("rssi", 0)), time.monotonic()
                wanted = key not in _links
                _cv.notify_all()
            if wanted and not self.stopping:
                try:
                    wanted = bool(_want(advert))
                except Exception:  # noqa: BLE001
                    log.exception("Bluetooth policy check failed")
                    wanted = False
                if wanted:
                    with _cv:
                        _links.setdefault(key, Link(advert))
                    self.request_connect(key)
        elif ev == "scan":
            now = time.monotonic()
            with _cv:
                if msg.get("running"):
                    _scan["running"], _scan["started"], _scan["error"] = True, now, ""
                else:
                    _scan["running"], _scan["finished"], _scan["error"] = False, now, str(msg.get("error", ""))
                _cv.notify_all()
        elif ev == "link":
            state = msg.get("state")
            unknown = False
            with _cv:
                link = _links.get(key)
                if link is None:
                    unknown = state != "gone"
                else:
                    if state != "ready":  # whatever was on its way is lost: nobody waits for it any longer
                        link._queue.clear()
                        link._done = link._handed
                    link.state = "gone" if state == "gone" else str(state)
                    if state == "gone":
                        del _links[key]
                _cv.notify_all()
            if unknown:
                self.request_drop(key)  # a connection this side never asked for (or has given up on)
        elif ev == "rx":
            data = bytes.fromhex(msg.get("data", ""))
            with _cv:
                link = _links.get(key)
                if link is not None and len(data) >= 3:
                    _absorb(link, data, time.monotonic())
        elif ev == "done":
            with _cv:
                link = _links.get(key)
                if link is not None:
                    link._done = min(link._handed, link._done + 1)
                    _cv.notify_all()
        elif ev == "fatal":
            _helper["disabled"] = str(msg.get("error", "")) or "Bluetooth is not usable here"
            self.stopping = True
            log.info("Bluetooth: %s", _helper["disabled"])

    def _read(self):
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    try:
                        self.handle(msg)
                    except Exception:  # noqa: BLE001 - one odd message must not end the conversation
                        log.exception("Bluetooth helper sent %r", msg)
        except (OSError, ValueError):
            pass
        self._ended()

    def _watch(self, step: float = 2.0):
        """End a helper that has stopped reporting. Counted in steps of this thread, not in clock time: after
        the PC slept, hours have passed on the clock without the helper having done anything wrong."""
        seen, quiet = -1, 0
        while self.proc.poll() is None:
            time.sleep(step)
            quiet = quiet + 1 if self.rx_count == seen else 0
            seen = self.rx_count
            # the very first word takes longer: the helper has to start up (and unpack, in the Windows build)
            limit = HELPER_SILENCE if seen > 0 else HELPER_SILENCE * 3
            if quiet * step >= limit and not self.stopping:
                self.why = f"it stopped answering for {limit:.0f} s"
                _end_process(self.proc)
                return

    def _ended(self):
        global _radio
        try:
            code = self.proc.wait(timeout=5.0)
        except Exception:  # noqa: BLE001 - output closed but still running: it is of no use any more
            _end_process(self.proc)
            code = self.proc.wait()
        now = time.monotonic()
        expected = self.stopping
        ran = now - self.started
        with _cv:
            if _radio is self:
                _radio = None
                for link in _links.values():
                    link.state = "gone"
                    link._queue.clear()
                    link._done = link._handed
                _links.clear()
                _scan["running"], _scan["finished"] = False, now
                if not expected:
                    _scan["error"] = "Bluetooth stopped unexpectedly on this PC, details in rgbdeck.log"
            if not expected:
                _helper["crashes"] = 1 if ran >= HELPER_STEADY else _helper["crashes"] + 1
                _helper["retry_at"] = now + 20.0 * _helper["crashes"]
                if _helper["crashes"] >= HELPER_CRASHES and not _helper["disabled"]:
                    _helper["disabled"] = (
                        "Bluetooth keeps stopping on this PC, so RGB Deck leaves it alone until it is started again"
                    )
            _cv.notify_all()
        if expected:
            return
        trace = ""
        try:
            with open(self.fault_file) as f:
                trace = f.read(4000).strip()
        except OSError:
            pass
        log.warning(
            "Bluetooth helper ended unexpectedly (%s) after %.0f s, last request: %s.%s%s",
            self.why or _exit_text(code),
            ran,
            self.last_op,
            " " + _helper["disabled"] + "." if _helper["disabled"] else "",
            "\n" + trace if trace else "",
        )


def _helper_mode() -> bool:
    """Does the radio live in a helper process (always on Windows) instead of this one?"""
    if _notify is not None:  # this is the helper
        return False
    forced = os.environ.get("RGBDECK_BLE_HELPER")
    if forced in ("0", "1"):
        return forced == "1"
    return sys.platform == "win32"


def _spawn_helper() -> Optional[_RemoteRadio]:
    import subprocess

    if _helper["disabled"] or time.monotonic() < _helper["retry_at"]:
        return None
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--ble-helper"]
    else:
        cmd = [sys.executable, "-m", "rgbdeck.blehelper"]
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    fault_file = ""
    try:
        from rgbdeck import paths

        paths.config_dir().mkdir(parents=True, exist_ok=True)
        fault_file = str(paths.config_dir() / "bluetooth-helper-fault.txt")
        env["RGBDECK_BLE_FAULTLOG"] = fault_file
    except OSError:
        pass
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(  # noqa: S603 - our own program, fixed arguments
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, creationflags=flags
        )
    except OSError as e:
        _helper["disabled"] = f"the Bluetooth helper could not be started: {e}"
        log.warning("Bluetooth: %s", _helper["disabled"])
        return None
    log.info("Bluetooth runs in a helper process (pid %d)", proc.pid)
    return _RemoteRadio(proc, fault_file)


def helper_main() -> int:
    """The helper process: holds the radio, takes requests on standard input, reports on standard output."""
    global _notify
    import faulthandler

    fault_file = None
    if os.environ.get("RGBDECK_BLE_FAULTLOG"):
        try:
            fault_file = open(os.environ["RGBDECK_BLE_FAULTLOG"], "w")  # noqa: SIM115 - open for the process' life
            faulthandler.enable(file=fault_file, all_threads=True)
        except (OSError, RuntimeError):
            fault_file = None
    try:
        inp = open(0, "rb", closefd=False)  # noqa: SIM115
        out = open(1, "wb", closefd=False)  # noqa: SIM115
    except OSError:
        return 3
    wlock = threading.Lock()

    def tell(ev: str, **fields):
        fields["ev"] = ev
        try:
            with wlock:
                out.write((json.dumps(fields) + "\n").encode())
                out.flush()
        except (OSError, ValueError):
            pass  # the main program is gone: the reader below notices and ends the helper

    class _Forward(logging.Handler):
        def emit(self, record):
            try:
                tell("log", level=record.levelno, msg=record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    log.addHandler(_Forward())
    log.setLevel(logging.INFO)
    log.propagate = False
    _notify = tell
    qt = _load_qt()
    if not qt:
        tell("fatal", error="this build has no Bluetooth support")
        return 2
    QtCore = qt[0]
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication(sys.argv[:1])

    def qt_message(mode, _context, message):
        if mode not in (QtCore.QtMsgType.QtDebugMsg, QtCore.QtMsgType.QtInfoMsg):
            tell("log", level=logging.INFO, msg=f"Qt: {message}")

    QtCore.qInstallMessageHandler(qt_message)

    class _Quit(QtCore.QObject):
        go = QtCore.Signal()

    quitter = _Quit()
    quitter.go.connect(app.quit)
    if not start(lambda advert: False):  # the main program decides which lights get a connection
        tell("fatal", error="Bluetooth could not be started")
        return 2
    fault = os.environ.get("RGBDECK_BLE_HELPER_FAULT", "")  # tests: die the way a failing system call does

    def reader():
        try:
            for raw in inp:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                op, key = msg.get("op"), str(msg.get("key", ""))
                radio = _radio
                if op == "stop" or radio is None:
                    break
                if op == "scan":
                    if fault == "scan":
                        os.abort()
                    scan()
                elif op == "connect":
                    radio.request_connect(key)
                elif op == "send":
                    with _cv:
                        link = _links.get(key)
                    if link is not None:
                        link.send([bytes.fromhex(p) for p in msg.get("packets", [])], wait=False)
                elif op == "drop":
                    radio.request_drop(key)
        except (OSError, ValueError):
            pass
        finally:
            quitter.go.emit()

    threading.Thread(target=reader, name="rgbdeck-ble-requests", daemon=True).start()
    tell("tick")
    app.exec()
    stop()
    return 0


def helper_selftest() -> List[str]:
    """Checks of a finished build, no hardware needed: the helper starts and talks, a scan comes to an end
    one way or another, and this process survives the helper dying. Returns what went wrong."""
    wrong: List[str] = []
    saved = dict(_helper)
    _helper.update(crashes=0, retry_at=0.0, disabled="")
    try:
        if not start(lambda advert: False):
            return [f"the helper did not start: {scan_error() or 'no reason given'}"]
        radio = _radio
        scan()
        ended = wait(time.monotonic() + SCAN_MS / 1000 + SCAN_GRACE + 30, lambda: not _scan["running"])
        log.info(
            "selftest: Bluetooth helper talked: %s, scan ended: %s, said: %r", radio.rx_count > 0, ended, scan_error()
        )
        if radio.rx_count == 0:
            wrong.append("the helper never said a word")
        if not ended or radio.proc.poll() is not None:
            wrong.append("the scan did not come to an orderly end")
        stop()
        if _helper["crashes"]:
            wrong.append("an asked-for stop was counted as a crash")
        os.environ["RGBDECK_BLE_HELPER_FAULT"] = "scan"
        try:
            if start(lambda advert: False):
                scan()
                died = wait(time.monotonic() + 60, lambda: _radio is None)
                log.info("selftest: helper killed in a scan, noticed: %s, said: %r", died, scan_error())
                if not died or "stopped unexpectedly" not in scan_error():
                    wrong.append("a dying helper went unnoticed")
            else:
                wrong.append("the helper did not start a second time")
        finally:
            del os.environ["RGBDECK_BLE_HELPER_FAULT"]
            stop()
    finally:
        _helper.update(saved)
        with _cv:
            _scan.update(running=False, error="")
    return wrong


# ----------------------------------------------------------------------------- module API
_start_lock = threading.Lock()


def available() -> bool:
    """Can this process use Bluetooth at all? In this process that takes QtBluetooth and a Qt application
    object; with the helper it only takes a helper that has not given up."""
    if _helper_mode():
        return not os.environ.get("RGBDECK_NO_BLUETOOTH") and not _helper["disabled"]
    qt = _load_qt()
    return bool(qt) and qt[0].QCoreApplication.instance() is not None


def start(want: Callable[[Advert], bool]) -> bool:
    """Bring the radio up (idempotent). want(advert) decides which lights get a connection."""
    global _radio, _want
    _want = want
    if _radio is not None:
        return True
    with _start_lock:
        if _radio is not None:
            return True
        if not available():
            return False
        if _helper_mode():
            radio = _spawn_helper()
            if radio is None:
                return False
            _radio = radio
            radio.run()
            return True
        QtCore = _qt[0]
        radio = _make_radio()
        thread = QtCore.QThread()
        thread.setObjectName("rgbdeck-govee-ble")
        radio.qthread = thread
        radio.moveToThread(thread)
        thread.start()
        radio.sig_setup.emit()
        _radio = radio
        return True


def stop():
    """Hang up everywhere and end the radio. Must run before the Qt application object goes away."""
    global _radio
    with _start_lock:
        radio, _radio = _radio, None
    if radio is None:
        return
    radio.shutdown()
    with _cv:
        _links.clear()
        _adverts.clear()
        _scan.update(running=False, started=0.0, finished=0.0, error="")
        _cv.notify_all()


atexit.register(stop)


def _settle_scan(now: float):
    """A scan whose end was never reported is over all the same (call with _cv held)."""
    if _scan["running"] and now - _scan["started"] > SCAN_MS / 1000 + SCAN_GRACE:
        _scan["running"], _scan["finished"] = False, now
        _scan["error"] = _scan["error"] or "Bluetooth did not answer"


def scan() -> bool:
    """Start one look around (about five seconds); lights that are wanted get connected as they turn up."""
    radio = _radio
    if radio is None:
        return False
    with _cv:
        now = time.monotonic()
        _settle_scan(now)
        if not _scan["running"]:
            _scan["running"], _scan["started"], _scan["error"] = True, now, ""
    radio.request_scan()
    return True


def scan_age() -> float:
    """Seconds since the last scan ended (a huge number if there never was one; 0 while one is running)."""
    with _cv:
        _settle_scan(time.monotonic())
        if _scan["running"]:
            return 0.0
        return time.monotonic() - _scan["finished"] if _scan["finished"] else 1e9


def scanning() -> bool:
    with _cv:
        _settle_scan(time.monotonic())
        return bool(_scan["running"])


def scan_error() -> str:
    """Why Bluetooth could not look (switched off, no adapter, it keeps stopping on this PC), or ""."""
    with _cv:
        return _scan["error"] or _helper["disabled"]


def drop(key: str):
    """Hang up on one light and forget its connection."""
    radio = _radio
    if radio is not None:
        radio.request_drop(key)


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
