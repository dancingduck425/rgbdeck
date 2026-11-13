# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""Finding out what went wrong on a PC nobody can look at.

- A crash in a library (Qt, the HID library, a graphics driver) leaves no Python error behind. faulthandler
  writes where every thread was into crash.txt at that moment; the next start copies it into the log.
- Errors nobody caught, in any thread, and Qt's own warnings go to the log instead of a console that a
  windowed program does not have.
- A marker file exists while the program runs. Finding it at the start means the last run did not end the
  way it should (crash, ended from outside, power cut).
- write_report() puts everything worth sending along into one text file.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("rgbdeck")

_crash_file = None  # kept open for the life of the process: faulthandler writes to it when nothing else works
_marker: Optional[Path] = None
_seen_qt: set = set()


def _read(path: Path, limit: int = 20000) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode(errors="replace").strip()
    except OSError:
        return ""


def begin(folder: Path, version: str) -> str:
    """Call once the devices are this process's. Returns what is known about the previous run going wrong
    ("" if it ended properly)."""
    global _crash_file, _marker
    folder.mkdir(parents=True, exist_ok=True)
    crash, marker = folder / "crash.txt", folder / "running.json"
    note = ""
    left = _read(marker, 400)
    if left:
        try:
            info = json.loads(left)
        except ValueError:
            info = {}
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.get("started", 0))) if info else "?"
        note = f"the previous run (version {info.get('version', '?')}, started {started}) did not end properly"
    trace = _read(crash)
    if trace:
        note = (note or "the previous run left a crash trace") + ":\n" + trace
        try:
            (folder / "crash-previous.txt").write_text(trace + "\n", encoding="utf-8")
        except OSError:
            pass
    try:
        _crash_file = open(crash, "w", encoding="utf-8")  # noqa: SIM115 - open for the life of the process
        faulthandler.enable(file=_crash_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        _crash_file = None
    try:
        marker.write_text(json.dumps({"pid": os.getpid(), "version": version, "started": time.time()}))
        _marker = marker
    except OSError:
        _marker = None

    def uncaught(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):
            return
        log.critical("error nobody handled", exc_info=(kind, value, tb))

    def uncaught_in_thread(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical("error nobody handled in thread %s", name, exc_info=(args.exc_type, args.exc_value, None))
        if args.exc_traceback is not None:
            import traceback

            log.critical("%s", "".join(traceback.format_tb(args.exc_traceback)))

    sys.excepthook = uncaught
    threading.excepthook = uncaught_in_thread
    return note


def end():
    """The program is ending the way it should."""
    global _marker
    marker, _marker = _marker, None
    if marker is not None:
        try:
            marker.unlink()
        except OSError:
            pass


def watch_qt():
    """Qt's warnings and errors into the log (each text once: some repeat with every repaint)."""
    from PySide6 import QtCore

    def handler(mode, _context, message):
        if mode in (QtCore.QtMsgType.QtDebugMsg, QtCore.QtMsgType.QtInfoMsg):
            return
        if message in _seen_qt or len(_seen_qt) > 200:
            return
        _seen_qt.add(message)
        serious = mode in (QtCore.QtMsgType.QtCriticalMsg, QtCore.QtMsgType.QtFatalMsg)
        log.log(logging.ERROR if serious else logging.INFO, "Qt: %s", message)

    QtCore.qInstallMessageHandler(handler)


def _tail(path: Path, lines: int) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            return f.read().decode(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def report_text(version: str, folder: Path, log_file: Path, client=None, settings: Optional[dict] = None) -> str:
    """Everything worth sending along with a problem report, as text."""
    out: List[str] = []
    add = out.append
    add(f"RGB Deck {version} report, written {time.strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"system: {platform.platform()} · Python {platform.python_version()} · {platform.machine()}")
    try:
        from PySide6.QtCore import __version__ as qt_version

        add(f"Qt {qt_version}")
    except Exception:  # noqa: BLE001
        pass
    add(f"program: {sys.executable} ({'packaged' if getattr(sys, 'frozen', False) else 'from source'})")
    add("")
    add("== devices RGB Deck is driving")
    if client is not None:
        for d in list(client.devices):
            mode = d.current_mode.name if d.modes else "-"
            exp = ", experimental" if getattr(d, "experimental", False) else ""
            add(f"  {d.type_name}: {d.name} · {len(d.colors)} lights · mode {mode}{exp} · {d.location}")
        if not client.devices:
            add("  (none)")
        if getattr(client, "last_error", ""):
            add(f"  scan problems: {client.last_error}")
    add("")
    add("== USB devices with a control channel (HID), as the system lists them")
    try:
        from rgbdeck.hw import hidraw

        seen = set()
        for i in hidraw.enumerate_hid(with_descriptors=False):
            key = (i.vid, i.pid, i.interface, i.name)
            if key in seen:
                continue
            seen.add(key)
            add(f"  {i.vid:04x}:{i.pid:04x} interface {i.interface} · {i.name or '(no name)'}")
        if not seen:
            add("  (none)")
    except Exception as e:  # noqa: BLE001 - a report must come out whatever state the PC is in
        add(f"  could not list them: {e}")
    add("")
    add("== settings")
    shown = dict(settings or {})
    if isinstance(shown.get("remote"), dict) and shown["remote"].get("token"):
        shown["remote"] = {**shown["remote"], "token": "(hidden)"}
    add(json.dumps(shown, indent=1, default=str))
    for name, title in (
        ("crash.txt", "crash trace of this run"),
        ("crash-previous.txt", "crash trace of an earlier run"),
        ("bluetooth-helper-fault.txt", "crash trace of the Bluetooth helper"),
    ):
        text = _read(folder / name)
        if text:
            add("")
            add(f"== {title}")
            add(text)
    add("")
    add(f"== end of the log ({log_file})")
    out.extend(_tail(log_file, 400) or ["(empty)"])
    return "\n".join(out) + "\n"


def report_target() -> Path:
    """Where a report goes: the desktop if there is one, else the home folder."""
    home = Path.home()
    for folder in (home / "Desktop", home / "OneDrive" / "Desktop", home):
        if folder.is_dir():
            return folder / "RGB Deck report.txt"
    return home / "RGB Deck report.txt"
