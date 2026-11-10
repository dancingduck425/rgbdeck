# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
Things the lights do by themselves:

    schedule        at a time of day: dim, brightness back up, a preset, off, on
    away            lights off while the screen is locked or the PC sleeps, the look back afterwards
    notifications   a short flash in a color when a desktop notification arrives
    profiles        a preset while a chosen program is in front, the previous look back when it is not

The rules themselves (Schedule, notification parsing, Profiles) are plain classes without Qt, so they are tested
without a desktop. Automation wires them to the running system:

                    Linux                                                    Windows
    lock            org.freedesktop.ScreenSaver.ActiveChanged (session bus)  desktop switch check every 2 s
    sleep           logind PrepareForSleep (system bus)                      -
    notifications   dbus-monitor on org.freedesktop.Notifications.Notify     not available without packaging
    focus           a KWin script reporting the active window (KDE Plasma)   foreground window's program name

Settings shape (settings["automation"]):
    {"away": bool, "schedule": [{"at": "22:00", "do": "dim 30"}], "notify": {"on": bool, "color": 0xRRGGBB,
     "apps": {"discord": 0x5865F2}, "mute": ["spotify"]}, "profiles": [{"app": "steam_app", "preset": "Game"}]}
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import List, Optional, Tuple

log = logging.getLogger("rgbdeck.automation")

ACTIONS = ("dim", "preset", "off", "on")


# ----------------------------------------------------------------------------- schedule
def parse_rule(rule: dict) -> Optional[Tuple[dt.time, str]]:
    """(time of day, Director command) for one schedule entry, or None if it makes no sense."""
    try:
        hh, mm = str(rule.get("at", "")).split(":")
        at = dt.time(int(hh), int(mm))
    except ValueError:
        return None
    verb, _, arg = str(rule.get("do", "")).strip().partition(" ")
    arg = arg.strip()
    if verb == "dim" and arg.isdigit():
        return at, f"brightness {max(5, min(100, int(arg)))}"
    if verb == "preset" and arg:
        return at, f"preset {arg}"
    if verb in ("off", "on"):
        return at, verb
    return None


class Schedule:
    """Which rules came due between two moments (each rule fires once, when its time of day is crossed)."""

    def __init__(self, rules: List[dict]):
        self.rules = sorted(r for r in (parse_rule(x) for x in rules or []) if r)

    def due(self, before: dt.datetime, now: dt.datetime) -> List[str]:
        if now <= before or now - before > dt.timedelta(hours=12):
            return []  # the clock jumped (resume from sleep, time change): nothing fires late
        out = []
        day = before.date()
        while day <= now.date():
            for at, command in self.rules:
                moment = dt.datetime.combine(day, at)
                if before < moment <= now:
                    out.append(command)
            day += dt.timedelta(days=1)
        return out


# ----------------------------------------------------------------------------- notifications
class NotifyParser:
    """Reads dbus-monitor's text output line by line; feed() returns an app name when a Notify call starts."""

    def __init__(self):
        self._armed = False

    def feed(self, line: str) -> Optional[str]:
        text = line.strip()
        if text.startswith("method call") and "member=Notify" in text and "org.freedesktop.Notifications" in text:
            self._armed = True
            return None
        if self._armed and text.startswith('string "'):
            self._armed = False
            return text[8:-1] if text.endswith('"') else text[8:]
        if self._armed and text and not text.startswith("string"):
            self._armed = False
        return None


def notify_color(cfg: dict, app: str) -> Optional[int]:
    """Flash color for a notification from this app (None: stay quiet)."""
    if not cfg or not cfg.get("on"):
        return None
    name = (app or "").lower()
    if any(m and m.lower() in name for m in cfg.get("mute") or []):
        return None
    for key, color in (cfg.get("apps") or {}).items():
        if key and key.lower() in name:
            return int(color) & 0xFFFFFF
    return int(cfg.get("color", 0xFFFFFF)) & 0xFFFFFF


# ----------------------------------------------------------------------------- per-app profiles
class Profiles:
    """Decides what a change of the focused program means: ("apply", preset) when a rule starts to match,
    ("restore", None) when no rule matches any more, None when nothing changes."""

    def __init__(self, rules: List[dict]):
        self.rules = [
            (str(r.get("app", "")).lower(), str(r.get("preset", "")))
            for r in rules or []
            if r.get("app") and r.get("preset")
        ]
        self.active: Optional[str] = None  # preset applied by a rule right now
        self.recent: List[str] = []  # programs seen in front, newest first (offered when writing a rule)

    def on_focus(self, app: str) -> Optional[Tuple[str, Optional[str]]]:
        app = (app or "").strip()
        if not app:
            return None  # the desktop itself, a panel: keep things as they are
        if app in self.recent:
            self.recent.remove(app)
        self.recent.insert(0, app)
        del self.recent[12:]
        match = next((preset for key, preset in self.rules if key in app.lower()), None)
        if match == self.active:
            return None
        self.active = match
        return ("apply", match) if match else ("restore", None)


KWIN_SCRIPT = """
function report(w) {
    callDBus("org.rgbdeck.Focus", "/Focus", "org.rgbdeck.Focus", "Changed", w ? String(w.resourceClass) : "");
}
workspace.windowActivated.connect(report);
report(workspace.activeWindow);
"""


def _windows_foreground() -> str:
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("\\", "/").rsplit("/", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def _windows_locked() -> bool:
    import ctypes

    user32 = ctypes.windll.user32
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not user32.SwitchDesktop(desk)
    finally:
        user32.CloseDesktop(desk)


# ----------------------------------------------------------------------------- wiring
def _hooks_off() -> bool:
    """Tests (and anyone who wants none of it) switch the desktop hooks off with RGBDECK_NO_DESKTOP_HOOKS=1."""
    return bool(os.environ.get("RGBDECK_NO_DESKTOP_HOOKS"))


def _make_automation():
    from PySide6.QtCore import SLOT, ClassInfo, QObject, QTimer, Signal, Slot

    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except Exception:  # noqa: BLE001 - a build without QtDBus (Windows) has no bus to listen on
        QDBusConnection = QDBusInterface = None

    @ClassInfo({"D-Bus Interface": "org.rgbdeck.Focus"})
    class _FocusReceiver(QObject):
        changed = Signal(str)

        @Slot(str)
        def Changed(self, app: str):  # noqa: N802 - the D-Bus method name
            self.changed.emit(app)

    class Automation(QObject):
        """host: command(text) -> reply (the Director's), snapshot() -> look, restore(look), blackout(reason, on),
        flash(rgb)."""

        notified = Signal(str)
        status = Signal(str)

        def __init__(self, host, parent=None):
            super().__init__(parent)
            self.host = host
            self.cfg: dict = {}
            self.schedule = Schedule([])
            self.profiles = Profiles([])
            self._saved_look = None
            self._last_tick = dt.datetime.now()
            self._last_flash = 0.0
            self._monitor: Optional[subprocess.Popen] = None
            self._kwin_script: Optional[int] = None
            self._focus = _FocusReceiver(self)
            self._focus.changed.connect(self.focus_changed)
            self._bus_ready = False
            self.notified.connect(self._on_notification)
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(2000 if sys.platform == "win32" else 20000)

        # ---- configuration -----------------------------------------------------------
        def configure(self, cfg: dict):
            cfg = dict(cfg or {})
            old, self.cfg = self.cfg, cfg
            self.schedule = Schedule(cfg.get("schedule") or [])
            recent = self.profiles.recent
            if (cfg.get("profiles") or []) != (old.get("profiles") or []) or not self.profiles.rules:
                active = self.profiles.active
                self.profiles = Profiles(cfg.get("profiles") or [])
                self.profiles.recent, self.profiles.active = recent, active
            self._connect_bus()
            want_notify = bool((cfg.get("notify") or {}).get("on"))
            if want_notify and self._monitor is None:
                self._start_monitor()
            elif not want_notify and self._monitor is not None:
                self._stop_monitor()
            self._watch_focus(True)  # also feeds the "recently in front" list for writing rules
            if not cfg.get("away", False):
                self.host.blackout("away", False)

        def shutdown(self):
            self._timer.stop()
            self._stop_monitor()
            self._watch_focus(False)

        # ---- schedule + Windows polling -------------------------------------------------
        def _tick(self):
            now = dt.datetime.now()
            for command in self.schedule.due(self._last_tick, now):
                log.info("schedule: %s -> %s", command, self.host.command(command))
            self._last_tick = now
            if sys.platform == "win32" and not _hooks_off():
                try:
                    self.away_changed(_windows_locked())
                    self.focus_changed(_windows_foreground())
                except Exception:  # noqa: BLE001
                    log.debug("windows polling failed", exc_info=True)

        # ---- lock / sleep ---------------------------------------------------------------
        def _connect_bus(self):
            if self._bus_ready or QDBusConnection is None or sys.platform == "win32" or _hooks_off():
                return
            self._bus_ready = True
            session, system = QDBusConnection.sessionBus(), QDBusConnection.systemBus()
            for path in ("/ScreenSaver", "/org/freedesktop/ScreenSaver"):
                session.connect(
                    "", path, "org.freedesktop.ScreenSaver", "ActiveChanged", self, SLOT("away_changed(bool)")
                )
            system.connect(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                self,
                SLOT("sleep_changed(bool)"),
            )

        @Slot(bool)
        def away_changed(self, away: bool):
            if self.cfg.get("away", False):
                self.host.blackout("away", bool(away))

        @Slot(bool)
        def sleep_changed(self, going: bool):
            if self.cfg.get("away", False):
                self.host.blackout("sleep", bool(going))

        # ---- notifications ----------------------------------------------------------------
        def _start_monitor(self):
            exe = shutil.which("dbus-monitor")
            if _hooks_off():
                return
            if sys.platform == "win32" or not exe:
                self.status.emit("Notification flashes need 'dbus-monitor' (Linux desktops only).")
                return
            rule = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
            try:
                self._monitor = subprocess.Popen(
                    [exe, "--session", rule], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
                )
            except OSError as e:
                self.status.emit(f"Could not watch notifications: {e}")
                return
            proc = self._monitor

            def read():
                parser = NotifyParser()
                for line in proc.stdout:
                    app = parser.feed(line)
                    if app is not None:
                        try:
                            self.notified.emit(app)
                        except RuntimeError:
                            return

            threading.Thread(target=read, name="rgbdeck-notify", daemon=True).start()

        def _stop_monitor(self):
            if self._monitor is not None:
                try:
                    self._monitor.terminate()
                except OSError:
                    pass
                self._monitor = None

        def _on_notification(self, app: str):
            color = notify_color(self.cfg.get("notify") or {}, app)
            if color is None or time.monotonic() - self._last_flash < 2.0:
                return
            self._last_flash = time.monotonic()
            self.host.flash((color >> 16 & 0xFF, color >> 8 & 0xFF, color & 0xFF))

        # ---- focus ------------------------------------------------------------------------
        def _watch_focus(self, on: bool):
            if sys.platform == "win32" or QDBusConnection is None or _hooks_off():
                return
            bus = QDBusConnection.sessionBus()
            if on and self._kwin_script is None:
                if not bus.registerService("org.rgbdeck.Focus"):
                    return  # another RGB Deck owns it
                bus.registerObject("/Focus", self._focus, QDBusConnection.ExportAllSlots)
                kwin = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus)
                if not kwin.isValid():
                    self._kwin_script = -1  # not KDE Plasma: no source for the focused program
                    return
                with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="rgbdeck-focus-", delete=False) as f:
                    f.write(KWIN_SCRIPT)
                kwin.call("unloadScript", "rgbdeck-focus")
                reply = kwin.call("loadScript", f.name, "rgbdeck-focus")
                args = reply.arguments()
                self._kwin_script = int(args[0]) if args and isinstance(args[0], int) else -1
                if self._kwin_script >= 0:
                    QDBusInterface(
                        "org.kde.KWin", f"/Scripting/Script{self._kwin_script}", "org.kde.kwin.Script", bus
                    ).call("run")
            elif not on and self._kwin_script is not None:
                if self._kwin_script >= 0:
                    QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus).call(
                        "unloadScript", "rgbdeck-focus"
                    )
                bus.unregisterObject("/Focus")
                bus.unregisterService("org.rgbdeck.Focus")
                self._kwin_script = None

        @property
        def focus_supported(self) -> bool:
            return sys.platform == "win32" or (self._kwin_script is not None and self._kwin_script >= 0)

        @Slot(str)
        def focus_changed(self, app: str):
            verdict = self.profiles.on_focus(app)
            if verdict is None:
                return
            what, preset = verdict
            if what == "apply":
                if self._saved_look is None:
                    self._saved_look = self.host.snapshot()
                log.info("profile: %s in front -> preset %s (%s)", app, preset, self.host.command(f"preset {preset}"))
            elif self._saved_look is not None:
                look, self._saved_look = self._saved_look, None
                self.host.restore(look)
                log.info("profile: back to the previous look")

    return Automation


def create(host, parent=None):
    """The Automation object (needs a running Qt application)."""
    return _make_automation()(host, parent)


__all__ = ["ACTIONS", "NotifyParser", "Profiles", "Schedule", "create", "notify_color", "parse_rule"]
