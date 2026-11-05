#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""RGB Deck - standalone lighting control for your keyboard, mouse and motherboard (direct hidraw)."""

from __future__ import annotations

import colorsys
import json
import logging
import math
import os
import random
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

from PySide6.QtCore import (
    QPointF,
    QRect,
    QRectF,
    QSize,
    QSocketNotifier,
    Qt,
    QTimer,
    Signal,
    __version__ as QT_VERSION_STR,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


from rgbdeck.models import (
    Device,
    Mode,
    DeviceError,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_PER_LED,  # noqa: E402
    MODE_COLORS_RANDOM,
    DIRECTION_NAMES,
    rgb_to_u32,
    u32_to_rgb,
)
from rgbdeck.backend import LocalBackend  # noqa: E402
from rgbdeck import __version__  # noqa: E402

log = logging.getLogger("rgbdeck")
IS_WINDOWS = sys.platform == "win32"


from rgbdeck.paths import config_root as _config_root  # noqa: E402


LOG_FILE = _config_root() / "rgbdeck" / "rgbdeck.log"
TRAY_AUTOSTART_FILE = _config_root() / "autostart" / "rgbdeck.desktop"  # Linux; Windows uses the Run registry key


def setup_logging(verbose: bool):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_FILE)]
    if verbose and sys.stderr is not None:  # a windowed Windows build has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=handlers,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )


from rgbdeck.keyboard_layout import KeyGeom, build_geometry  # noqa: E402
from rgbdeck.hw import govee_lan  # noqa: E402
from rgbdeck.effects import EffectEngine, EffectSettings, KeyListener, find_keyboard_events  # noqa: E402

APP_NAME = "RGB Deck"
CONFIG_DIR = _config_root() / "rgbdeck"
PRESETS_FILE = CONFIG_DIR / "presets.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
AUTOSTART_FILE = _config_root() / "autostart" / "rgbdeck-apply.desktop"  # Linux; Windows uses the registry
CONTROL_FILE = CONFIG_DIR / "control.json"  # Windows: port + token of the running instance

ACCENT = "#7c6cff"
LAST_LOOK = "Last look"  # preset written on close so a per-key layout comes back at login
# One running instance per user. Linux: an abstract unix socket. Windows (no AF_UNIX in Python): a loopback
# TCP port, published with a random token in CONTROL_FILE so only this user's processes can drive it.
CONTROL_SOCKET = "\0rgbdeck-" + (str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", ""))
CONTROL_TOKEN = ""
TCP_CONTROL = not hasattr(socket, "AF_UNIX")  # Windows: loopback TCP + token instead of a unix socket


def _control_connect() -> socket.socket:
    """Connect to the running instance's control channel (OSError / ValueError if there is none)."""
    if TCP_CONTROL:
        info = json.loads(CONTROL_FILE.read_text())
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.settimeout(3.0)
        sk.connect(("127.0.0.1", int(info["port"])))
        sk.sendall((str(info["token"]) + " ").encode())
        return sk
    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sk.settimeout(3.0)
    sk.connect(CONTROL_SOCKET)
    return sk


def send_control(cmd: str) -> Optional[str]:
    """Talk to a running RGB Deck. None if none is running."""
    try:
        with _control_connect() as sk:
            sk.sendall(cmd.encode() + b"\n")
            reply = sk.recv(4096).decode(errors="replace").strip()
    except (OSError, ValueError, KeyError):
        return None
    # a stale control.json may point at a port some other program now owns: only our own replies count
    if reply.split(" ", 1)[0] in ("pong", "ok", "applied", "no", "unknown"):
        return reply
    return None


def forget_control_file():
    if TCP_CONTROL:
        try:
            CONTROL_FILE.unlink()
        except OSError:
            pass


def control_listen() -> Optional[socket.socket]:
    """Bind the control channel for this instance; None if another instance already owns it."""
    global CONTROL_TOKEN
    if TCP_CONTROL:
        if send_control("ping") == "pong":
            return None
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.bind(("127.0.0.1", 0))
        CONTROL_TOKEN = secrets.token_hex(16)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONTROL_FILE.write_text(json.dumps({"port": sk.getsockname()[1], "token": CONTROL_TOKEN}))
    else:
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sk.bind(CONTROL_SOCKET)
        except OSError:
            sk.close()
            return None
    sk.listen(4)
    sk.setblocking(False)
    return sk


def launch_command() -> List[str]:
    """How to start this very installation of RGB Deck (for the login entries)."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable]
    if IS_WINDOWS:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        return [str(pyw if pyw.exists() else sys.executable), "-m", "rgbdeck"]
    launcher = Path.home() / ".local/bin/rgbdeck"
    return [str(launcher)] if launcher.exists() else [sys.executable, "-m", "rgbdeck"]


def open_in_desktop(path: Path):
    """Open a file with the desktop's default application (Explorer association on Windows, xdg-open elsewhere)."""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        log.warning("could not open %s: %s", path, e)


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART = {"tray": (TRAY_AUTOSTART_FILE, "RGB Deck"), "apply": (AUTOSTART_FILE, "RGB Deck preset")}


def _desktop_quote(arg: str) -> str:
    """Quote one argument for a desktop-entry Exec= line (double quotes, not shell quoting)."""
    if arg and not any(ch in arg for ch in " \t\"'\\$`<>~|&;*?#()"):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def autostart_enabled(kind: str) -> bool:
    path, value = _AUTOSTART[kind]
    if not IS_WINDOWS:
        return path.exists()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, value)
        return True
    except OSError:
        return False


def set_autostart(kind: str, args: Optional[List[str]]):
    """Start RGB Deck with these extra arguments at login ("tray" / "apply" entry); None removes the entry."""
    path, value = _AUTOSTART[kind]
    if IS_WINDOWS:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            if args is None:
                try:
                    winreg.DeleteValue(key, value)
                except OSError:
                    pass
            else:
                winreg.SetValueEx(key, value, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_command() + args))
        return
    if args is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(_desktop_quote(a) for a in launch_command() + args)
    path.write_text(
        "[Desktop Entry]\nType=Application\n"
        + ("Name=RGB Deck (apply preset)\nNoDisplay=true\n" if kind == "apply" else "Name=RGB Deck\n")
        + f"Exec={exec_line}\nIcon=rgbdeck\nTerminal=false\nX-GNOME-Autostart-Delay=5\n"
    )


DEFAULT_PALETTE = [
    "#ff2d55",
    "#ff6a00",
    "#ffc800",
    "#8cff00",
    "#00ff6a",
    "#00ffd0",
    "#00b4ff",
    "#2f5bff",
    "#8a2bff",
    "#e02bff",
    "#ff2bb0",
    "#ffffff",
    "#ff9d9d",
    "#9dffd0",
    "#9dcdff",
    "#d9b3ff",
]

ICONS = {
    "Keyboard": "⌨",
    "Mouse": "🖱",
    "Motherboard": "▣",
    "GPU": "▤",
    "DRAM": "▥",
    "Headset": "🎧",
    "LED Strip": "〰",
    "Light": "💡",
    "Cooler": "❄",
    "Mousemat": "▭",
}

QSS = f"""
* {{ font-family: "Inter", "Noto Sans", "Segoe UI", sans-serif; font-size: 13px; color: #e8e9ee; }}
QMainWindow, QWidget#root {{ background: #121317; }}
QFrame#card {{ background: #1b1d24; border: 1px solid #272a34; border-radius: 14px; }}
QLabel#cardTitle {{ font-size: 12px; font-weight: 600; color: #9aa0b4; letter-spacing: 1px; }}
QLabel#hint {{ color: #7d8398; font-size: 12px; }}
QLabel#devTitle {{ font-size: 18px; font-weight: 700; }}
QPushButton, QToolButton {{ background: #262a35; border: 1px solid #30354200; border-radius: 9px; padding: 7px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: #313645; }}
QPushButton:pressed, QToolButton:pressed {{ background: {ACCENT}; }}
QPushButton:disabled, QToolButton:disabled {{ color: #666c7d; background: #1f222b; }}
QPushButton#primary {{ background: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #9385ff; }}
QPushButton#danger:hover {{ background: #b33a4a; }}
QToolButton#chip {{ border-radius: 15px; padding: 6px 14px; background: #262a35; }}
QToolButton#chip:checked {{ background: {ACCENT}; color: white; font-weight: 600; }}
QToolButton#seg {{ border-radius: 0px; padding: 7px 14px; background: #262a35; border: none; }}
QToolButton#seg:checked {{ background: {ACCENT}; color: white; font-weight: 600; }}
QToolButton#segL {{ border-top-left-radius: 9px; border-bottom-left-radius: 9px; }}
QToolButton#segR {{ border-top-right-radius: 9px; border-bottom-right-radius: 9px; }}
QToolButton#devtab {{ background: #1b1d24; border: 1px solid #272a34; border-radius: 12px; padding: 8px 14px; text-align: left; }}
QToolButton#devtab:hover {{ background: #232732; }}
QToolButton#devtab:checked {{ border: 2px solid {ACCENT}; background: #22243a; }}
QSlider::groove:horizontal {{ height: 6px; background: #2b2f3a; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; background: #ffffff; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSpinBox, QLineEdit {{ background: #262a35; border: 1px solid #30354200; border-radius: 7px; padding: 4px 6px; }}
QCheckBox::indicator {{ width: 36px; height: 20px; border-radius: 10px; background: #2b2f3a; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; }}
QCheckBox {{ spacing: 8px; }}
QMenuBar {{ background: #121317; }} QMenuBar::item:selected {{ background: #262a35; border-radius: 6px; }}
QMenu {{ background: #1b1d24; border: 1px solid #272a34; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }} QMenu::item:selected {{ background: {ACCENT}; }}
QStatusBar {{ color: #9aa0b4; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ width: 8px; background: transparent; }} QScrollBar::handle:vertical {{ background: #2f3340; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: #262a35; color: #e8e9ee; border: 1px solid #3a3f4f; padding: 4px; }}
"""


def qcolor_to_u32(c: QColor) -> int:
    return rgb_to_u32(c.red(), c.green(), c.blue())


def u32_to_qcolor(v: int) -> QColor:
    r, g, b = u32_to_rgb(v)
    return QColor(r, g, b)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def card(title: str = "") -> (QFrame, QVBoxLayout):
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    if title:
        t = QLabel(title.upper())
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return f, lay


def device_colors_preview(dev: Device) -> List[int]:
    """A handful of colors that represent what the device is showing."""
    m = dev.current_mode
    if m.color_mode == MODE_COLORS_PER_LED and dev.colors:
        n = len(dev.colors)
        step = max(1, n // 12)
        return dev.colors[::step][:12]
    if m.color_mode == MODE_COLORS_MODE_SPECIFIC and m.colors:
        return list(m.colors)
    if m.name.lower() == "off":
        return [0]
    return [rgb_to_u32(*(int(x * 255) for x in colorsys.hsv_to_rgb(i / 12, 1, 1))) for i in range(12)]


# =============================================================================
# Color wheel + panel
# =============================================================================
class ColorWheel(QWidget):
    colorChanged = Signal(QColor, bool)  # live=True while dragging

    def __init__(self, parent=None):
        super().__init__(parent)
        self._h, self._s, self._v = 0.56, 1.0, 1.0
        self._dragging = False
        self.setFixedSize(210, 210)
        self.setCursor(Qt.CrossCursor)

    def color(self) -> QColor:
        return QColor.fromHsvF(self._h, self._s, self._v)

    def setColor(self, c: QColor):
        h, s, v, _ = c.getHsvF()
        if h < 0:
            h = self._h
        self._h, self._s, self._v = h, s, v
        self.update()

    def _geom(self):
        side = min(self.width(), self.height()) - 10
        return self.width() / 2, self.height() / 2, side / 2

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy, r = self._geom()
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        shadow = QRadialGradient(cx, cy + 4, r + 8)
        shadow.setColorAt(0.85, QColor(0, 0, 0, 90))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(shadow)
        p.drawEllipse(rect.adjusted(-8, -4, 8, 12))
        cone = QConicalGradient(cx, cy, 0)
        for i in range(0, 361, 20):
            cone.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1, 1))
        p.setBrush(QBrush(cone))
        p.drawEllipse(rect)
        rad = QRadialGradient(cx, cy, r)
        rad.setColorAt(0.0, QColor(255, 255, 255, 255))
        rad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(rad))
        p.drawEllipse(rect)
        if self._v < 1.0:
            p.setBrush(QColor(0, 0, 0, int(255 * (1 - self._v))))
            p.drawEllipse(rect)
        ang = self._h * 2 * math.pi
        mx, my = cx + math.cos(ang) * self._s * r, cy - math.sin(ang) * self._s * r
        p.setPen(QPen(Qt.white, 2.5))
        p.setBrush(self.color())
        p.drawEllipse(QPointF(mx, my), 9, 9)
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.drawEllipse(QPointF(mx, my), 10.5, 10.5)

    def _pick(self, pos, live):
        cx, cy, r = self._geom()
        dx, dy = pos.x() - cx, cy - pos.y()
        self._s = min(1.0, math.hypot(dx, dy) / r) if r > 0 else 0
        self._h = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
        self.update()
        self.colorChanged.emit(self.color(), live)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self._pick(e.position(), True)

    def mouseMoveEvent(self, e):
        if self._dragging:
            self._pick(e.position(), True)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self._pick(e.position(), False)


class Swatch(QToolButton):
    def __init__(self, color: QColor, size=26, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(size, size)
        self._apply()

    def color(self) -> QColor:
        return QColor(self._color)

    def setColor(self, c: QColor):
        self._color = QColor(c)
        self._apply()

    def _apply(self):
        self.setStyleSheet(
            f"QToolButton{{background:{self._color.name()};border:2px solid #0000;border-radius:7px;padding:0;}}"
            f"QToolButton:hover{{border:2px solid #fff;}}"
        )
        self.setToolTip(self._color.name().upper())


class ColorPanel(QWidget):
    """Wheel + brightness + fine sliders + palette. colorChanged(QColor, live)."""

    colorChanged = Signal(QColor, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = QColor("#00b4ff")
        self._updating = False
        self.palette_colors: List[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.wheel = ColorWheel()
        self.wheel.colorChanged.connect(self._from_wheel)
        top.addWidget(self.wheel, 0, Qt.AlignTop)
        side = QVBoxLayout()
        side.setSpacing(6)
        self.preview = QFrame()
        self.preview.setFixedHeight(54)
        side.addWidget(self.preview)
        self.hex = QLineEdit()
        self.hex.setMaxLength(7)
        self.hex.setAlignment(Qt.AlignCenter)
        self.hex.editingFinished.connect(self._from_hex)
        side.addWidget(self.hex)
        self.save_btn = QPushButton("＋ Save to palette")
        self.save_btn.clicked.connect(self._add_to_palette)
        side.addWidget(self.save_btn)
        side.addStretch(1)
        top.addLayout(side, 1)
        lay.addLayout(top)

        self.v_slider = QSlider(Qt.Horizontal)
        self.v_slider.setRange(0, 100)
        self.v_slider.setToolTip("Brightness of the active color")
        lay.addWidget(self._labeled("Brightness", self.v_slider))

        self.adv_btn = QToolButton()
        self.adv_btn.setText("▸ Fine tune (RGB / HSV)")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setStyleSheet(
            "QToolButton{background:transparent;color:#9aa0b4;padding:2px;}QToolButton:hover{color:#fff;}"
        )
        self.adv_btn.toggled.connect(self._toggle_adv)
        lay.addWidget(self.adv_btn)
        self.adv = QWidget()
        grid = QGridLayout(self.adv)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        self.sliders: Dict[str, QSlider] = {}
        self.spins: Dict[str, QSpinBox] = {}
        for i, (name, mx) in enumerate([("H", 359), ("S", 100), ("R", 255), ("G", 255), ("B", 255)]):
            lbl = QLabel(name)
            lbl.setFixedWidth(14)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, mx)
            sp = QSpinBox()
            sp.setRange(0, mx)
            sp.setFixedWidth(56)
            sp.setButtonSymbols(QSpinBox.NoButtons)
            sl.valueChanged.connect(lambda v, n=name: self._from_slider(n, v, True))
            sl.sliderReleased.connect(lambda n=name: self._from_slider(n, self.sliders[n].value(), False))
            sp.editingFinished.connect(lambda n=name: self._from_slider(n, self.spins[n].value(), False))
            grid.addWidget(lbl, i, 0)
            grid.addWidget(sl, i, 1)
            grid.addWidget(sp, i, 2)
            self.sliders[name], self.spins[name] = sl, sp
        self.adv.setVisible(False)
        lay.addWidget(self.adv)
        self.v_slider.valueChanged.connect(lambda v: self._from_slider("V", v, True))
        self.v_slider.sliderReleased.connect(lambda: self._from_slider("V", self.v_slider.value(), False))

        pal_lbl = QLabel("PALETTE  ·  click = apply")
        pal_lbl.setObjectName("cardTitle")
        lay.addWidget(pal_lbl)
        self.palette_box = QWidget()
        self.palette_grid = QGridLayout(self.palette_box)
        self.palette_grid.setContentsMargins(0, 0, 0, 0)
        self.palette_grid.setSpacing(5)
        lay.addWidget(self.palette_box)
        self._sync_widgets()

    @staticmethod
    def _labeled(text, w):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(text)
        lbl.setFixedWidth(72)
        h.addWidget(lbl)
        h.addWidget(w, 1)
        return box

    def _toggle_adv(self, on):
        self.adv.setVisible(on)
        self.adv_btn.setText(("▾" if on else "▸") + " Fine tune (RGB / HSV)")

    def color(self) -> QColor:
        return QColor(self._color)

    def setColor(self, c: QColor, emit=False, live=False):
        self._color = QColor(c)
        self._sync_widgets()
        if emit:
            self.colorChanged.emit(self.color(), live)

    def _sync_widgets(self):
        self._updating = True
        c = self._color
        self.wheel.setColor(c)
        h, s, v, _ = c.getHsv()
        vals = {"H": max(0, h), "S": int(s / 255 * 100 + 0.5), "R": c.red(), "G": c.green(), "B": c.blue()}
        for k, val in vals.items():
            self.sliders[k].setValue(val)
            self.spins[k].setValue(val)
        self.v_slider.setValue(int(v / 255 * 100 + 0.5))
        self.hex.setText(c.name().upper())
        self.preview.setStyleSheet(f"background:{c.name()};border-radius:10px;")
        self._updating = False

    def _from_wheel(self, c, live):
        self._color = QColor(c)
        self._sync_widgets()
        self.colorChanged.emit(self.color(), live)

    def _from_slider(self, name, v, live):
        if self._updating:
            return
        c = QColor(self._color)
        if name in "HSV":
            h, s, val, _ = c.getHsv()
            h = max(0, h)
            if name == "H":
                h = v
            elif name == "S":
                s = int(v / 100 * 255 + 0.5)
            else:
                val = int(v / 100 * 255 + 0.5)
                if s == 0 and val > 0 and self._color.value() == 0:
                    s = 0
            c.setHsv(h, s, val)
        else:
            r, g, b = c.red(), c.green(), c.blue()
            r, g, b = (v if name == "R" else r), (v if name == "G" else g), (v if name == "B" else b)
            c.setRgb(r, g, b)
        self._color = c
        self._sync_widgets()
        self.colorChanged.emit(self.color(), live)

    def _from_hex(self):
        t = self.hex.text().strip()
        c = QColor(t if t.startswith("#") else "#" + t)
        if c.isValid():
            self._color = c
            self._sync_widgets()
            self.colorChanged.emit(self.color(), False)
        else:
            self._sync_widgets()

    def set_palette(self, colors: List[str]):
        self.palette_colors = list(colors)
        while self.palette_grid.count():
            w = self.palette_grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        per_row = 8
        for i, hx in enumerate(DEFAULT_PALETTE + self.palette_colors):
            sw = Swatch(QColor(hx), 30)
            sw.clicked.connect(lambda _=False, col=QColor(hx): self.setColor(col, emit=True))
            if i >= len(DEFAULT_PALETTE):
                sw.setContextMenuPolicy(Qt.CustomContextMenu)
                sw.customContextMenuRequested.connect(lambda _p, col=hx: self._remove_from_palette(col))
                sw.setToolTip(f"{hx.upper()}  (right-click to remove)")
            self.palette_grid.addWidget(sw, i // per_row, i % per_row)

    def _add_to_palette(self):
        hx = self._color.name()
        if hx not in self.palette_colors:
            self.palette_colors.append(hx)
            self.set_palette(self.palette_colors)

    def _remove_from_palette(self, hx):
        if hx in self.palette_colors:
            self.palette_colors.remove(hx)
            self.set_palette(self.palette_colors)


# =============================================================================
# Device canvas
# =============================================================================
class LedCanvas(QWidget):
    paintRequested = Signal(list)
    selectionChanged = Signal()
    colorPicked = Signal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.geoms: List[KeyGeom] = []
        self.colors: List[int] = []
        self.selected: Set[int] = set()
        self.mode = "device"  # device | paint | select
        self.dev_type = ""
        self.fallback_colors: List[int] = []  # for LED-less devices (mode colors)
        self._drag_painted: Set[int] = set()
        self._rubber: Optional[QRect] = None
        self._press_pos = None
        self._press_mods = Qt.NoModifier
        self._hover: Optional[int] = None
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    def set_device(self, geoms, colors, dev_type, fallback):
        self.geoms, self.colors, self.dev_type, self.fallback_colors = geoms, list(colors), dev_type, list(fallback)
        self.selected = set()
        self.selectionChanged.emit()
        self.update()

    def set_colors(self, colors):
        self.colors = list(colors)
        self.update()

    def set_fallback(self, colors):
        self.fallback_colors = list(colors)
        self.update()

    def _scale(self):
        maxx = max(g.x + g.w for g in self.geoms)
        maxy = max(g.y + g.h for g in self.geoms)
        pad = 26
        s = min((self.width() - 2 * pad) / maxx, (self.height() - 2 * pad) / maxy)
        return s, (self.width() - maxx * s) / 2, (self.height() - maxy * s) / 2

    def _rect(self, g, s, ox, oy):
        gap = max(2.0, s * 0.08)
        return QRectF(ox + g.x * s + gap / 2, oy + g.y * s + gap / 2, g.w * s - gap, g.h * s - gap)

    def _hit(self, pos):
        if not self.geoms:
            return None
        s, ox, oy = self._scale()
        for g in self.geoms:
            if self._rect(g, s, ox, oy).contains(pos):
                return g.led
        return None

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0, QColor("#15171d"))
        bg.setColorAt(1, QColor("#0f1014"))
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 14, 14)
        p.fillPath(path, bg)
        if not self.geoms:
            self._paint_glyph(p)
            return
        s, ox, oy = self._scale()
        # board plate
        maxx = max(g.x + g.w for g in self.geoms)
        maxy = max(g.y + g.h for g in self.geoms)
        plate = QRectF(ox - 12, oy - 12, maxx * s + 24, maxy * s + 24)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1d2029"))
        p.drawRoundedRect(plate, 16, 16)
        font = QFont(self.font())
        font.setPixelSize(max(7, int(s * 0.27)))
        p.setFont(font)
        fm = QFontMetrics(font)
        for g in self.geoms:
            r = self._rect(g, s, ox, oy)
            col = u32_to_qcolor(self.colors[g.led]) if g.led < len(self.colors) else QColor(0, 0, 0)
            lum = 0.299 * col.red() + 0.587 * col.green() + 0.114 * col.blue()
            kp = QPainterPath()
            kp.addRoundedRect(r, s * 0.14, s * 0.14)
            if lum > 2:
                glow = QRadialGradient(r.center(), max(r.width(), r.height()) * 0.9)
                gc = QColor(col)
                gc.setAlpha(60)
                glow.setColorAt(0, gc)
                gc2 = QColor(col)
                gc2.setAlpha(0)
                glow.setColorAt(1, gc2)
                p.setPen(Qt.NoPen)
                p.setBrush(glow)
                p.drawEllipse(r.center(), r.width() * 0.95, r.height() * 0.95)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2b2f3a"))
            p.drawPath(kp)
            if lum > 2:
                p.setBrush(col)  # exact color you picked, no tint
                p.drawPath(kp)
            if g.led in self.selected:
                p.setPen(QPen(Qt.white, max(2.0, s * 0.07)))
                p.setBrush(Qt.NoBrush)
                p.drawPath(kp)
            elif g.led == self._hover and self.mode != "device":
                p.setPen(QPen(QColor(255, 255, 255, 150), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawPath(kp)
            if g.label and s > 13:
                p.setPen(QColor(255, 255, 255, 215) if lum < 150 else QColor(0, 0, 0, 210))
                p.drawText(r, Qt.AlignCenter, fm.elidedText(g.label, Qt.ElideRight, int(r.width() - 2)))
        if self._rubber:
            p.setPen(QPen(QColor(ACCENT), 1.5, Qt.DashLine))
            p.setBrush(QColor(124, 108, 255, 40))
            p.drawRect(self._rubber)

    def _paint_glyph(self, p: QPainter):
        """LED-less device: draw a shape filled with its effect color(s)."""
        cols = [u32_to_qcolor(c) for c in (self.fallback_colors or [0])]
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        size = min(w, h) * 0.6
        if self.dev_type == "Mouse":
            body = QPainterPath()
            body.addRoundedRect(
                QRectF(cx - size * 0.28, cy - size * 0.48, size * 0.56, size * 0.96), size * 0.26, size * 0.26
            )
        else:
            body = QPainterPath()
            body.addRoundedRect(QRectF(cx - size * 0.45, cy - size * 0.32, size * 0.9, size * 0.64), 18, 18)
        main = cols[0]
        glow = QRadialGradient(cx, cy, size * 0.8)
        gc = QColor(main)
        gc.setAlpha(120)
        glow.setColorAt(0, gc)
        gc2 = QColor(main)
        gc2.setAlpha(0)
        glow.setColorAt(1, gc2)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), size * 0.8, size * 0.8)
        p.setBrush(QColor("#262a35"))
        p.drawPath(body)
        if len(cols) > 1:
            grad = QLinearGradient(cx - size * 0.3, 0, cx + size * 0.3, 0)
            for i, c in enumerate(cols):
                grad.setColorAt(i / max(1, len(cols) - 1), c)
            p.setBrush(grad)
        else:
            p.setBrush(main)
        p.setOpacity(0.9)
        p.drawPath(body)
        p.setOpacity(1.0)
        if self.dev_type == "Mouse":
            p.setPen(QPen(QColor(0, 0, 0, 120), 2))
            p.drawLine(QPointF(cx, cy - size * 0.48), QPointF(cx, cy - size * 0.1))
            p.setBrush(QColor(0, 0, 0, 120))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(cx - size * 0.035, cy - size * 0.36, size * 0.07, size * 0.14), 4, 4)
        p.setPen(QColor("#8d93a8"))
        p.drawText(
            QRectF(0, cy + size * 0.52, w, 30),
            Qt.AlignCenter,
            "This device is lit by its effect — pick a color or an effect on the right.",
        )

    # interaction
    def mousePressEvent(self, e: QMouseEvent):
        led = self._hit(e.position())
        self._press_pos = e.position().toPoint()
        self._press_mods = e.modifiers()
        if e.button() == Qt.RightButton:
            if led is not None and led < len(self.colors):
                self.colorPicked.emit(u32_to_qcolor(self.colors[led]))
            return
        if e.button() != Qt.LeftButton or not self.geoms:
            return
        selecting = self.mode == "select" or bool(e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
        if selecting:
            if led is not None:
                if e.modifiers() & Qt.ControlModifier:
                    self.selected ^= {led}
                elif e.modifiers() & Qt.ShiftModifier:
                    self.selected.add(led)
                else:
                    self.selected = {led}
                self.selectionChanged.emit()
            else:
                if not e.modifiers():
                    self.selected = set()
                    self.selectionChanged.emit()
                self._rubber = QRect(self._press_pos, QSize())
            self.update()
        elif self.mode == "paint":
            self._drag_painted = set()
            if led is not None:
                self._drag_painted.add(led)
                self.paintRequested.emit([led])

    def mouseMoveEvent(self, e):
        led = self._hit(e.position())
        if led != self._hover:
            self._hover = led
            self.update()
        if self._rubber is not None:
            self._rubber = QRect(self._press_pos, e.position().toPoint()).normalized()
            self.update()
        elif (
            e.buttons() & Qt.LeftButton
            and self._press_pos is not None
            and self.mode == "paint"
            and not (self._press_mods & (Qt.ControlModifier | Qt.ShiftModifier))
        ):
            if led is not None and led not in self._drag_painted:
                self._drag_painted.add(led)
                self.paintRequested.emit([led])

    def mouseReleaseEvent(self, _e):
        if self._rubber is not None:
            s, ox, oy = self._scale()
            band = QRectF(self._rubber)
            hits = {g.led for g in self.geoms if self._rect(g, s, ox, oy).intersects(band)}
            if self._press_mods & Qt.ControlModifier:
                self.selected ^= hits
            else:
                self.selected |= hits
            self._rubber = None
            self.selectionChanged.emit()
            self.update()
        self._press_pos = None
        self._drag_painted = set()

    def leaveEvent(self, _e):
        self._hover = None
        self.update()

    def select_all(self):
        self.selected = {g.led for g in self.geoms}
        self.selectionChanged.emit()
        self.update()

    def clear_selection(self):
        self.selected = set()
        self.selectionChanged.emit()
        self.update()

    def invert_selection(self):
        self.selected = {g.led for g in self.geoms} - self.selected
        self.selectionChanged.emit()
        self.update()

    def targets(self) -> List[KeyGeom]:
        return [g for g in self.geoms if not self.selected or g.led in self.selected]


# =============================================================================
# Device tab button
# =============================================================================
class DeviceTab(QToolButton):
    def __init__(self, dev: Device, parent=None):
        super().__init__(parent)
        self.dev = dev
        self.setObjectName("devtab")
        self.setCheckable(True)
        self.setMinimumWidth(210)
        self.setFixedHeight(64)
        self.setToolTip(f"{dev.type_name} · {dev.description}")

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = self.dev
        icon = ICONS.get(d.type_name, "●")
        f = QFont(self.font())
        f.setPixelSize(20)
        p.setFont(f)
        p.setPen(QColor("#e8e9ee"))
        p.drawText(QRectF(14, 8, 30, 30), Qt.AlignCenter, icon)
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        name = d.name
        short = {"Keyboard": "Keyboard", "Mouse": "Mouse", "Motherboard": "Motherboard"}.get(d.type_name, d.type_name)
        p.drawText(QRectF(50, 8, self.width() - 60, 18), Qt.AlignVCenter | Qt.AlignLeft, short)
        f.setBold(False)
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor("#9aa0b4"))
        fm = QFontMetrics(f)
        p.drawText(
            QRectF(50, 25, self.width() - 60, 14),
            Qt.AlignVCenter | Qt.AlignLeft,
            fm.elidedText(f"{name} · {d.current_mode.name}", Qt.ElideRight, self.width() - 64),
        )
        cols = device_colors_preview(d)
        x0, y0, wdt = 50, 45, self.width() - 64
        seg = wdt / max(1, len(cols))
        p.setPen(Qt.NoPen)
        for i, c in enumerate(cols):
            qc = u32_to_qcolor(c)
            if qc.value() < 8:
                qc = QColor("#2b2f3a")
            p.setBrush(qc)
            p.drawRoundedRect(QRectF(x0 + i * seg, y0, seg - 1.5, 7), 2, 2)


class PresetCard(QToolButton):
    def __init__(self, name: str, snap: dict, autostart: bool, parent=None):
        super().__init__(parent)
        self.name_, self.snap, self.autostart = name, snap, autostart
        self.setObjectName("devtab")
        self.setFixedSize(150, 58)
        self.setToolTip(f"{name}\nClick to apply · right-click for options")

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e8e9ee"))
        fm = QFontMetrics(f)
        p.drawText(
            QRectF(12, 8, self.width() - 24, 18),
            Qt.AlignVCenter | Qt.AlignLeft,
            fm.elidedText(("★ " if self.autostart else "") + self.name_, Qt.ElideRight, self.width() - 24),
        )
        cols: List[int] = []
        for dv in self.snap.get("devices", []):
            if dv.get("colors"):
                c = dv["colors"]
                cols += c[:: max(1, len(c) // 8)][:8]
            else:
                am = dv.get("active_mode", 0)
                modes = list(dv.get("modes", {}).values())
                if 0 <= am < len(modes) and modes[am].get("colors"):
                    cols += modes[am]["colors"][:3]
        cols = cols[:16] or [0]
        seg = (self.width() - 24) / len(cols)
        p.setPen(Qt.NoPen)
        for i, c in enumerate(cols):
            qc = u32_to_qcolor(c)
            if qc.value() < 8:
                qc = QColor("#2b2f3a")
            p.setBrush(qc)
            p.drawRoundedRect(QRectF(12 + i * seg, 36, seg - 1.5, 10), 3, 3)


# =============================================================================
# Effects panel
# =============================================================================
class EffectsPanel(QWidget):
    changed = Signal()  # params changed -> UpdateMode
    modePicked = Signal(Mode)  # user clicked an effect chip

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dev: Optional[Device] = None
        self._building = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.chips_box = QWidget()
        self.chips = QGridLayout(self.chips_box)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        lay.addWidget(self.chips_box)

        # --- app-driven effects (react to typing) ---
        self.app_settings = EffectSettings()
        self.app_active: Optional[str] = None
        self.app_lbl = QLabel("REACTS TO YOUR TYPING")
        self.app_lbl.setObjectName("cardTitle")
        lay.addWidget(self.app_lbl)
        self.app_box = QWidget()
        ag = QGridLayout(self.app_box)
        ag.setContentsMargins(0, 0, 0, 0)
        ag.setSpacing(6)
        self.app_chips: Dict[str, QToolButton] = {}
        for i, (kind, text, tip) in enumerate(
            [
                ("ripple", "✦ Ripple", "Every key you press sends a ring of color rippling across the board"),
                ("glow", "✦ Key glow", "Keys light up when pressed and fade back out"),
                ("ripple+glow", "✦ Ripple + glow", "Both: the key lights up and a ring spreads from it"),
            ]
        ):
            b = QToolButton()
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setText(text)
            b.setToolTip(tip)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _c=False, k=kind: self.appEffectPicked.emit(k))
            self.group.addButton(b, 1000 + i)
            ag.addWidget(b, 0, i)
            self.app_chips[kind] = b
        lay.addWidget(self.app_box)
        self.app_opts = QWidget()
        og = QGridLayout(self.app_opts)
        og.setContentsMargins(0, 2, 0, 0)
        og.setHorizontalSpacing(8)
        og.setVerticalSpacing(6)
        self.src_group = QButtonGroup(self)
        self.bg_group = QButtonGroup(self)
        for row, (label, group, options) in enumerate(
            [
                (
                    "Ripple color",
                    self.src_group,
                    [("active", "Active color"), ("rainbow", "Rainbow"), ("random", "Random"), ("key", "Contrast")],
                ),
                ("Under it", self.bg_group, [("colors", "My colors"), ("dim", "Dimmed"), ("black", "Black")]),
            ]
        ):
            lbl = QLabel(label)
            lbl.setFixedWidth(84)
            og.addWidget(lbl, row, 0)
            hb = QHBoxLayout()
            hb.setSpacing(0)
            for i, (val, text) in enumerate(options):
                b = QToolButton()
                b.setCheckable(True)
                b.setText(text)
                b.setProperty("val", val)
                b.setStyleSheet(
                    f"QToolButton{{border-radius:0;padding:5px 9px;background:#262a35;}}QToolButton:checked{{background:{ACCENT};font-weight:600;}}"
                    + ("QToolButton{border-top-left-radius:8px;border-bottom-left-radius:8px;}" if i == 0 else "")
                    + (
                        "QToolButton{border-top-right-radius:8px;border-bottom-right-radius:8px;}"
                        if i == len(options) - 1
                        else ""
                    )
                )
                b.clicked.connect(self._app_opts_changed)
                group.addButton(b)
                hb.addWidget(b)
            hb.addStretch(1)
            og.addLayout(hb, row, 1, 1, 2)
        self.app_sliders: Dict[str, QSlider] = {}
        for row, (key, label, tip) in enumerate(
            [
                ("speed", "Speed", "How fast the ring travels"),
                ("size", "Ring size", "Thickness of the ring"),
                ("fade", "Glow time", "How long a pressed key stays lit"),
            ],
            start=2,
        ):
            lbl = QLabel(label)
            lbl.setFixedWidth(84)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setToolTip(tip)
            sl.valueChanged.connect(self._app_opts_changed)
            og.addWidget(lbl, row, 0)
            og.addWidget(sl, row, 1, 1, 2)
            self.app_sliders[key] = sl
        self.test_btn = QPushButton("Try it (fake key press)")
        og.addWidget(self.test_btn, 5, 0, 1, 2)
        self.listen_lbl = QLabel("")
        self.listen_lbl.setObjectName("hint")
        self.listen_lbl.setWordWrap(True)
        og.addWidget(self.listen_lbl, 6, 0, 1, 3)
        lay.addWidget(self.app_opts)
        self.app_opts.setVisible(False)
        self._load_app_settings()

        self.params = QWidget()
        g = QGridLayout(self.params)
        g.setContentsMargins(0, 4, 0, 0)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.speed_lbl, self.speed, self.speed_val = QLabel("Speed"), QSlider(Qt.Horizontal), QLabel("")
        self.bright_lbl, self.bright, self.bright_val = QLabel("Brightness"), QSlider(Qt.Horizontal), QLabel("")
        for row, (lbl, sl, val) in enumerate(
            [(self.speed_lbl, self.speed, self.speed_val), (self.bright_lbl, self.bright, self.bright_val)]
        ):
            lbl.setFixedWidth(72)
            val.setFixedWidth(30)
            val.setAlignment(Qt.AlignRight)
            sl.valueChanged.connect(lambda v, lab=val: lab.setText(str(v)))
            sl.sliderReleased.connect(self._param_changed)
            sl.actionTriggered.connect(
                lambda a: None if a == QSlider.SliderMove else QTimer.singleShot(0, self._param_changed)
            )
            g.addWidget(lbl, row, 0)
            g.addWidget(sl, row, 1)
            g.addWidget(val, row, 2)
        self.dir_lbl = QLabel("Direction")
        self.dir_lbl.setFixedWidth(72)
        self.dir_box = QWidget()
        self.dir_lay = QHBoxLayout(self.dir_box)
        self.dir_lay.setContentsMargins(0, 0, 0, 0)
        self.dir_lay.setSpacing(4)
        self.dir_group = QButtonGroup(self)
        g.addWidget(self.dir_lbl, 2, 0)
        g.addWidget(self.dir_box, 2, 1, 1, 2)
        self.random = QCheckBox("Random colors")
        self.random.toggled.connect(lambda _b: self._param_changed())
        g.addWidget(self.random, 3, 0, 1, 3)
        self.colors_lbl = QLabel("Effect colors · click = use active color · right-click = pick it")
        self.colors_lbl.setObjectName("hint")
        g.addWidget(self.colors_lbl, 4, 0, 1, 3)
        self.colors_box = QWidget()
        self.colors_row = QHBoxLayout(self.colors_box)
        self.colors_row.setContentsMargins(0, 0, 0, 0)
        self.colors_row.setSpacing(5)
        g.addWidget(self.colors_box, 5, 0, 1, 3)
        lay.addWidget(self.params)

        row = QHBoxLayout()
        self.save_btn = QPushButton("💾 Keep on device")
        self.save_btn.setToolTip(
            "Store the current effect in the device's own memory, so it stays even without this app"
        )
        row.addWidget(self.save_btn)
        self.all_btn = QPushButton("Same effect on all devices")
        row.addWidget(self.all_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.info = QLabel("")
        self.info.setObjectName("hint")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

    def set_device(self, dev: Optional[Device]):
        self.dev = dev
        self._building = True
        for b in self.group.buttons():
            self.group.removeButton(b)
        while self.chips.count():
            w = self.chips.takeAt(0).widget()
            if w:
                w.deleteLater()
        if dev:
            per_row = 3
            for i, m in enumerate(dev.modes):
                b = QToolButton()
                b.setObjectName("chip")
                b.setCheckable(True)
                b.setText(m.name)
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                b.setChecked(i == dev.active_mode)
                b.clicked.connect(lambda _c=False, mm=m: self._chip(mm))
                self.group.addButton(b, i)
                self.chips.addWidget(b, i // per_row, i % per_row)
        self._building = False
        self._rebuild_params()

    def current_mode(self) -> Optional[Mode]:
        return self.dev.current_mode if self.dev else None

    def _chip(self, m: Mode):
        if self._building or not self.dev:
            return
        self.modePicked.emit(m)

    def sync_colors(self):
        """Update the effect-color swatches in place (no rebuild, no flicker)."""
        m = self.current_mode()
        if not m:
            return
        if len(getattr(self, "color_swatches", [])) != len(m.colors):
            self._rebuild_params()
            return
        for sw, c in zip(self.color_swatches, m.colors):
            qc = u32_to_qcolor(c)
            if sw.color() != qc:
                sw.setColor(qc)

    def refresh_active(self):
        if not self.dev:
            return
        self._building = True
        if self.app_active is None:
            b = self.group.button(self.dev.active_mode)
            if b:
                b.setChecked(True)
        self._building = False
        self._rebuild_params()

    def _rebuild_params(self):
        self._building = True
        m = self.current_mode()
        for w in (
            self.speed_lbl,
            self.speed,
            self.speed_val,
            self.bright_lbl,
            self.bright,
            self.bright_val,
            self.dir_lbl,
            self.dir_box,
            self.random,
            self.colors_lbl,
            self.colors_box,
            self.save_btn,
        ):
            w.setVisible(False)
        for lay in (self.colors_row, self.dir_lay):
            while lay.count():
                it = lay.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
        for b in self.dir_group.buttons():
            self.dir_group.removeButton(b)
        if not m:
            self._building = False
            return
        if m.has_speed:
            lo, hi = sorted((m.speed_min, m.speed_max))
            self.speed.setRange(lo, hi)
            self.speed.setInvertedAppearance(m.speed_min > m.speed_max)
            self.speed.setValue(m.speed)
            self.speed_val.setText(str(m.speed))
            for w in (self.speed_lbl, self.speed, self.speed_val):
                w.setVisible(True)
        if m.has_brightness:
            lo, hi = sorted((m.brightness_min, m.brightness_max))
            self.bright.setRange(lo, hi)
            self.bright.setValue(m.brightness)
            self.bright_val.setText(str(m.brightness))
            for w in (self.bright_lbl, self.bright, self.bright_val):
                w.setVisible(True)
        dirs = m.directions()
        if dirs:
            arrows = {0: "←", 1: "→", 2: "↑", 3: "↓", 4: "↔", 5: "↕"}
            for i, d in enumerate(dirs):
                b = QToolButton()
                b.setObjectName("seg" if 0 < i < len(dirs) - 1 else ("segL" if i == 0 else "segR"))
                b.setStyleSheet(
                    f"QToolButton{{border-radius:0;padding:5px 10px;}}QToolButton:checked{{background:{ACCENT};}}"
                    + ("QToolButton{border-top-left-radius:8px;border-bottom-left-radius:8px;}" if i == 0 else "")
                    + (
                        "QToolButton{border-top-right-radius:8px;border-bottom-right-radius:8px;}"
                        if i == len(dirs) - 1
                        else ""
                    )
                )
                b.setCheckable(True)
                b.setText(f"{arrows.get(d, '')} {DIRECTION_NAMES.get(d, d)}")
                b.setChecked(d == m.direction)
                b.clicked.connect(lambda _c=False, dd=d: self._set_dir(dd))
                self.dir_group.addButton(b, d)
                self.dir_lay.addWidget(b)
            self.dir_lay.addStretch(1)
            self.dir_lbl.setVisible(True)
            self.dir_box.setVisible(True)
        if m.has_random and m.has_mode_colors:
            self.random.setVisible(True)
            self.random.setChecked(m.color_mode == MODE_COLORS_RANDOM)
        self.color_swatches = []
        if m.has_mode_colors and m.color_mode != MODE_COLORS_RANDOM:
            self.colors_lbl.setVisible(True)
            self.colors_box.setVisible(True)
            for i, c in enumerate(m.colors):
                sw = Swatch(u32_to_qcolor(c), 32)
                self.color_swatches.append(sw)
                sw.clicked.connect(lambda _=False, k=i: self.requestColor.emit(k))
                sw.setContextMenuPolicy(Qt.CustomContextMenu)
                sw.customContextMenuRequested.connect(
                    lambda _p, k=i: self.pickColor.emit(u32_to_qcolor(self.current_mode().colors[k]))
                )
                self.colors_row.addWidget(sw)
            if len(m.colors) < m.colors_max:
                add = QToolButton()
                add.setText("+")
                add.setFixedSize(32, 32)
                add.clicked.connect(self._add_color)
                self.colors_row.addWidget(add)
            if len(m.colors) > m.colors_min:
                rem = QToolButton()
                rem.setText("−")
                rem.setFixedSize(32, 32)
                rem.clicked.connect(self._remove_color)
                self.colors_row.addWidget(rem)
            self.colors_row.addStretch(1)
        self.save_btn.setVisible(m.can_save)
        if m.has_per_led:
            self.info.setText(
                "Every key has its own color here. Use Paint / Select above the keyboard for detail work."
            )
        elif not (m.has_speed or m.has_brightness or dirs or m.has_mode_colors):
            self.info.setText("This effect has no settings.")
        else:
            self.info.setText("")
        self._building = False

    requestColor = Signal(int)
    pickColor = Signal(QColor)
    appEffectPicked = Signal(str)
    appSettingsChanged = Signal()

    def _load_app_settings(self):
        self._building = True
        st = self.app_settings
        for b in self.src_group.buttons():
            b.setChecked(b.property("val") == st.color_source)
        for b in self.bg_group.buttons():
            b.setChecked(b.property("val") == st.background)
        self.app_sliders["speed"].setValue(st.speed)
        self.app_sliders["size"].setValue(st.size)
        self.app_sliders["fade"].setValue(st.fade)
        self._building = False

    def _app_opts_changed(self, *_a):
        if self._building:
            return
        st = self.app_settings
        for b in self.src_group.buttons():
            if b.isChecked():
                st.color_source = b.property("val")
        for b in self.bg_group.buttons():
            if b.isChecked():
                st.background = b.property("val")
        st.speed = self.app_sliders["speed"].value()
        st.size = self.app_sliders["size"].value()
        st.fade = self.app_sliders["fade"].value()
        self.appSettingsChanged.emit()

    def set_app_effect(self, kind: Optional[str], settings: Optional[EffectSettings] = None):
        """Reflect an app effect being active (kind) or not (None)."""
        self.app_active = kind
        if settings:
            self.app_settings = settings
            self._load_app_settings()
        self._building = True
        for k, b in self.app_chips.items():
            b.setChecked(k == kind)
        if kind is None and self.dev:
            b = self.group.button(self.dev.active_mode)
            if b:
                b.setChecked(True)
        self._building = False
        self.app_opts.setVisible(kind is not None)
        self.app_sliders["fade"].setEnabled(kind != "ripple")
        self.app_sliders["size"].setEnabled(kind != "glow")
        self.app_sliders["speed"].setEnabled(kind != "glow")

    def show_app_section(self, visible: bool):
        self.app_lbl.setVisible(visible)
        self.app_box.setVisible(visible)
        if not visible:
            self.app_opts.setVisible(False)

    def _set_dir(self, d):
        m = self.current_mode()
        if m and not self._building:
            m.direction = d
            self.changed.emit()

    def _param_changed(self):
        if self._building:
            return
        m = self.current_mode()
        if not m:
            return
        if m.has_speed:
            m.speed = self.speed.value()
        if m.has_brightness:
            m.brightness = self.bright.value()
        if m.has_random and m.has_mode_colors:
            cm = MODE_COLORS_RANDOM if self.random.isChecked() else MODE_COLORS_MODE_SPECIFIC
            if cm != m.color_mode:
                m.color_mode = cm
                self._rebuild_params()
        self.changed.emit()

    def _add_color(self):
        m = self.current_mode()
        if m and len(m.colors) < m.colors_max:
            m.colors.append(m.colors[-1] if m.colors else 0xFFFFFF)
            self._rebuild_params()
            self.changed.emit()

    def _remove_color(self):
        m = self.current_mode()
        if m and len(m.colors) > m.colors_min:
            m.colors.pop()
            self._rebuild_params()
            self.changed.emit()

    def set_color(self, k: int, c: QColor):
        m = self.current_mode()
        if m and 0 <= k < len(m.colors):
            m.colors[k] = qcolor_to_u32(c)
            self.sync_colors()
            self.changed.emit()


# =============================================================================
# Settings dialog
# =============================================================================
LIGHT_STATES = {
    "connected": ("#3ddc84", "Connected."),
    "refused": (
        "#ffb020",
        "On your network, but it refuses local control. Switch on <b>LAN Control</b> for it in the Govee Home app; "
        "RGB Deck picks it up by itself after that.",
    ),
    "unreachable": ("#ff5d6c", "Not reachable. Is it powered and on this network?"),
    "blind": ("#8ab4ff", "No answer comes back, colors are sent anyway (a firewall may be dropping its replies)."),
    "silent": ("#ffb020", "Known light, not answering right now."),
    "checking": ("#7d8398", "Checking…"),
}


class LightsDialog(QDialog):
    """Network lights: what is connected, why something is not, how to connect one, add by address."""

    checked = Signal(str, str, str)  # address, verdict, model
    searched = Signal(object)  # govee_lan.search_network() result
    progressed = Signal(int)  # percent of the address sweep

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lights on your network")
        self.setMinimumWidth(600)
        self.changed = False  # the caller rescans devices when something was added / removed / switched
        self._shown = None
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        head = QLabel("💡  Govee lights")
        head.setObjectName("devTitle")
        lay.addWidget(head)
        intro = QLabel(
            "RGB Deck drives Govee Wi-Fi lights directly over your network. No account, no cloud. "
            "A connected light shows up as a device next to your keyboard and mouse."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hint")
        lay.addWidget(intro)
        self.enable = QCheckBox("Look for Govee lights on my network")
        self.enable.setChecked(govee_lan.get_settings()["enabled"])
        self.enable.setToolTip(
            "Windows asks once whether RGB Deck may use your network: allow it for private networks."
        )
        self.enable.toggled.connect(self._toggle)
        lay.addWidget(self.enable)

        self.search_btn = QPushButton("Search my network")
        self.search_btn.setObjectName("primary")
        self.search_btn.setToolTip(
            "Asks every address on your local network whether it is a Govee light, and looks for Govee\n"
            "hardware that has LAN Control switched off. Takes a few seconds."
        )
        self.search_btn.clicked.connect(self._search)
        lay.addWidget(self.search_btn, 0, Qt.AlignLeft)

        card = QFrame()
        card.setObjectName("card")
        self.rows = QVBoxLayout(card)
        self.rows.setContentsMargins(14, 12, 14, 12)
        self.rows.setSpacing(10)
        lay.addWidget(card)

        add = QHBoxLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("Light address, e.g. 192.168.1.50")
        self.addr.returnPressed.connect(self._check)
        add.addWidget(self.addr, 1)
        self.check_btn = QPushButton("Check and add")
        self.check_btn.clicked.connect(self._check)
        add.addWidget(self.check_btn)
        lay.addLayout(add)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)

        steps = QLabel(
            "<b>Connecting a Govee light</b><br>"
            "1. Open the <b>Govee Home</b> app on your phone and tap the light.<br>"
            "2. Tap the settings gear (top right) and switch on <b>LAN Control</b>.<br>"
            "3. Keep the light on the same network as this PC. It appears here within half a minute.<br><br>"
            "Not every model has the switch: Bluetooth-only Govee lights can't be reached this way. "
            "If a light with LAN Control on is still not found, type its address above. "
            "A firewall on this PC has to let incoming UDP port 4002 through for lights to be found by themselves."
        )
        steps.setWordWrap(True)
        steps.setObjectName("hint")
        lay.addWidget(steps)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)

        self.checked.connect(self._checked)
        self.searched.connect(self._searched)
        self.progressed.connect(lambda pct: self.search_btn.setText(f"Searching… {pct}%"))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        govee_lan.rescan()
        self._refresh()
        if self.enable.isChecked() and not any(r["state"] == "connected" for r in self._shown or []):
            QTimer.singleShot(200, self._search)  # nothing connected: go and look without being asked

    # ---- list -------------------------------------------------------------------
    def _refresh(self):
        rows = govee_lan.status_report() if self.enable.isChecked() else []
        if rows == self._shown:
            return
        self._shown = rows
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not rows:
            empty = QLabel(
                "No light found yet. Press “Search my network”, or follow the steps below first."
                if self.enable.isChecked()
                else "Not looking for lights. Switch it on above."
            )
            empty.setWordWrap(True)
            self.rows.addWidget(empty)
            return
        for r in rows:
            color, text = LIGHT_STATES.get(r["state"], LIGHT_STATES["checking"])
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(
                f"<span style='color:{color}'>●</span>&nbsp; <b>{r['name']}</b> "
                f"<span style='color:#7d8398'>· {r['ip']}</span><br>{text}"
            )
            lbl.setWordWrap(True)
            h.addWidget(lbl, 1)
            if r["state"] != "connected":
                rm = QPushButton("Remove")
                rm.clicked.connect(lambda _c=False, ip=r["ip"]: self._remove(ip))
                h.addWidget(rm, 0, Qt.AlignTop)
            self.rows.addWidget(w)

    def _toggle(self, on: bool):
        govee_lan.set_settings(on, govee_lan.get_settings()["manual"])
        self.changed = True
        govee_lan.rescan()
        self._refresh()

    def _search(self):
        if not self.search_btn.isEnabled():
            return
        self.search_btn.setEnabled(False)
        self.search_btn.setText("Searching…")
        self.result.setText(
            "Searching your network for Govee lights. A few seconds on most home networks, up to a quarter "
            "of a minute on a large one."
        )

        def tick(done: int, total: int):
            try:
                self.progressed.emit(min(99, done * 100 // total))
            except RuntimeError:
                pass

        def work():
            try:
                res = govee_lan.search_network(progress=tick)
            except Exception as e:  # noqa: BLE001 - report it in the panel instead of dying silently
                log.exception("network search failed")
                res = {"error": str(e)}
            try:
                self.searched.emit(res)
            except RuntimeError:
                pass  # the panel was closed meanwhile

        threading.Thread(target=work, name="rgbdeck-govee-search", daemon=True).start()

    def _searched(self, res: dict):
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search my network")
        self.enable.blockSignals(True)
        self.enable.setChecked(govee_lan.get_settings()["enabled"])
        self.enable.blockSignals(False)
        self.changed = True
        if res.get("error"):
            self.result.setText(f"The search failed: {res['error']}")
            return
        parts = []
        if res["found"]:
            names = ", ".join(f"Govee {sku} at {ip}" for sku, ip in res["found"])
            parts.append(f"Found {names}. It shows up as a device in a moment.")
        if res["locked"]:
            parts.append(
                f"Found Govee hardware at {', '.join(res['locked'])} with <b>LAN Control switched off</b>. Switch it on "
                "in the Govee Home app (the light, settings gear, LAN Control) and it connects by itself."
            )
        if res["blind"]:
            parts.append(
                f"Govee hardware at {', '.join(res['blind'])} takes commands but its answers do not arrive "
                "(allow incoming UDP port 4002 in this PC's firewall). Colors are sent to it anyway."
            )
        if not parts:
            parts.append(
                f"No Govee device among the {res['hosts']} addresses of your network. Is the light powered and on "
                "the same Wi-Fi as this PC? Bluetooth-only Govee lights can't be found this way."
            )
        self.result.setText(" ".join(parts))
        self._refresh()

    def _remove(self, ip: str):
        if govee_lan.forget(ip):
            self.changed = True
        self._refresh()

    # ---- add by address -------------------------------------------------------------
    def _check(self):
        ip = self.addr.text().strip()
        if not ip or not self.check_btn.isEnabled():
            return
        self.check_btn.setEnabled(False)
        self.result.setText(f"Checking {ip}…")

        def work():
            verdict, sku = govee_lan.diagnose(ip)
            self.checked.emit(ip, verdict, sku)

        threading.Thread(target=work, name="rgbdeck-govee-check", daemon=True).start()

    def _checked(self, ip: str, verdict: str, sku: str):
        self.check_btn.setEnabled(True)
        if verdict == "invalid":
            self.result.setText(
                "That is not an address. It looks like 192.168.1.50; your router's device list shows it."
            )
            return
        if verdict == "unreachable":
            self.result.setText(f"Nothing reachable at {ip}. Check the address and that the light is powered.")
            return
        cfg = govee_lan.get_settings()
        if verdict == "found":
            govee_lan.set_settings(True, cfg["manual"])
            self.result.setText(f"Found a Govee {sku} at {ip}. It appears as a device in a moment.")
        else:
            govee_lan.set_settings(True, cfg["manual"] + [ip])
            if verdict == "refused":
                self.result.setText(
                    f"Something is at {ip}, but it refuses local control. If that is your Govee light, LAN Control "
                    "is still off: Govee Home app, the light, settings gear, LAN Control. It is on the list now "
                    "and RGB Deck picks it up as soon as the switch is on."
                )
            else:
                self.result.setText(
                    f"No answer from {ip}. Added anyway: colors are sent there blind, which works when a "
                    "firewall is only dropping the light's replies."
                )
        self.enable.blockSignals(True)
        self.enable.setChecked(True)
        self.enable.blockSignals(False)
        self.changed = True
        self.addr.clear()
        self._refresh()


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RGB Deck settings")
        self.settings = settings
        form = QFormLayout(self)
        form.setSpacing(10)
        self.sync = QCheckBox("Colors apply to all devices by default")
        self.sync.setChecked(settings.get("sync", True))
        form.addRow("Sync", self.sync)
        self.tray_start = QCheckBox("Start RGB Deck minimized in the tray when I log in")
        self.tray_start.setChecked(autostart_enabled("tray"))
        form.addRow("Login", self.tray_start)
        self.keep = QCheckBox("Keep my look when I close RGB Deck")
        self.keep.setChecked(settings.get("keep_on_close", True))
        self.keep.setToolTip(
            "On close, store the current colors / effect in each device's own memory so they survive a reboot.\n"
            "Multi-color key layouts can't be stored in the keyboard; those are re-applied at login instead."
        )
        form.addRow("Closing", self.keep)
        self.fps = QSpinBox()
        self.fps.setRange(10, 40)
        self.fps.setValue(int(settings.get("fx_fps", 24)))
        self.fps.setToolTip("Frames per second for typing effects. Most keyboards manage 20–30.")
        form.addRow("Effect speed (fps)", self.fps)
        self.follow = QCheckBox("Room lights pulse along with typing effects")
        self.follow.setChecked(settings.get("fx_follow", True))
        self.follow.setToolTip("While a typing effect runs on the keyboard, network lights flash with every key press.")
        form.addRow("", self.follow)
        net = govee_lan.get_settings()
        self.network_changed = False
        self.govee = QCheckBox("Find Govee lights on my network")
        self.govee.setChecked(net["enabled"])
        self.govee.setToolTip(
            "Works with Govee Wi-Fi lights once 'LAN Control' is switched on for them in the Govee Home app\n"
            "(open the light, settings gear, LAN Control). No account or cloud is used.\n"
            "Windows asks once whether RGB Deck may use your network: allow it for private networks."
        )
        form.addRow("Network lights", self.govee)
        self.govee_ips = QLineEdit(", ".join(net["manual"]))
        self.govee_ips.setPlaceholderText("Optional: light addresses, e.g. 192.168.1.50")
        self.govee_ips.setToolTip(
            "Only needed when a light is not found by itself: another network segment,\n"
            "or a firewall that drops the light's answers (incoming UDP port 4002)."
        )
        form.addRow("", self.govee_ips)
        found = govee_lan.found_lights() if net["enabled"] else []
        gl = QLabel(
            "Found: " + ", ".join(f"{sku} at {ip}" for sku, ip in found)
            if found
            else "No Govee light is answering right now."
            if net["enabled"]
            else "Not looking for network lights."
        )
        gl.setObjectName("hint")
        form.addRow("", gl)
        lf = QLabel(f"Log file: {LOG_FILE}")
        lf.setObjectName("hint")
        form.addRow("", lf)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def apply(self) -> dict:
        self.settings["sync"] = self.sync.isChecked()
        self.settings["fx_fps"] = self.fps.value()
        self.settings["keep_on_close"] = self.keep.isChecked()
        self.settings["fx_follow"] = self.follow.isChecked()
        ips = [x for x in self.govee_ips.text().replace(";", ",").replace(" ", ",").split(",") if x]
        self.network_changed = govee_lan.set_settings(self.govee.isChecked(), ips)
        try:
            set_autostart("tray", ["--tray"] if self.tray_start.isChecked() else None)
        except OSError as e:
            log.error("could not update the login entry: %s", e)
        return self.settings


# =============================================================================
# Main window
# =============================================================================
class MainWindow(QMainWindow):
    scanFinished = Signal()

    def __init__(self, client: LocalBackend):
        super().__init__()
        self.client = client
        self._scanning = False
        self._scan_error = ""
        self._scan_resume_fx = None
        self.scanFinished.connect(self._scan_done)
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 860)
        self.settings = load_json(SETTINGS_FILE, {})
        self.presets: Dict[str, dict] = load_json(PRESETS_FILE, {})
        self.dev: Optional[Device] = None
        self.base_colors: Dict[int, List[int]] = {}
        self.dim: Dict[int, int] = {}
        self.tabs: List[DeviceTab] = []
        self._pending_leds: Set[int] = set()
        self._pending_mode_color: Dict[int, QColor] = {}
        self._mode_timer = QTimer(self)
        self._mode_timer.setSingleShot(True)
        self._mode_timer.timeout.connect(self._flush_mode_colors)
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush)
        self.engine: Optional[EffectEngine] = None
        self.engine_dev: Optional[Device] = None
        self.engine_kind: Optional[str] = None
        self._follow_last: Dict[int, List[int]] = {}  # device index -> frame last sent to a light that pulses along
        self.listener: Optional[KeyListener] = None
        self._fx_timer = QTimer(self)
        self._fx_timer.timeout.connect(self._fx_tick)
        self._fx_last_frame: Optional[List[int]] = None
        self._build_ui()
        self._build_tray()
        self._start_control_socket()
        self._build_menu()
        self.color_panel.set_palette(self.settings.get("palette", []))
        self.sync_chk.setChecked(self.settings.get("sync", True))
        self._populate_devices()
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_server)
        self._poll.start(2000)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(10)

        # top: device tabs + sync
        top = QHBoxLayout()
        top.setSpacing(8)
        self.tabs_lay = QHBoxLayout()
        self.tabs_lay.setSpacing(8)
        top.addLayout(self.tabs_lay)
        self.lights_btn = QPushButton("💡  Lights")
        self.lights_btn.setToolTip(
            "Govee lights on your network: what is connected, how to connect one, add by address"
        )
        self.lights_btn.clicked.connect(self._open_lights)
        top.addWidget(self.lights_btn)
        top.addStretch(1)
        self.sync_chk = QCheckBox("Sync all devices")
        self.sync_chk.setToolTip("When on, colors you apply go to every device (keyboard, mouse, …) at once")
        self.sync_chk.toggled.connect(self._sync_toggled)
        top.addWidget(self.sync_chk)
        off = QPushButton("⏻  All off")
        off.setObjectName("danger")
        off.clicked.connect(self._everything_off)
        top.addWidget(off)
        outer.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(10)
        outer.addLayout(body, 1)

        # left column
        left = QVBoxLayout()
        left.setSpacing(10)
        body.addLayout(left, 1)

        cv_card, cv_lay = card()
        head = QHBoxLayout()
        self.dev_title = QLabel("")
        self.dev_title.setObjectName("devTitle")
        head.addWidget(self.dev_title)
        self.exp_badge = QLabel("EXPERIMENTAL")
        self.exp_badge.setToolTip(
            "This driver was ported from protocol sources and has not been confirmed on real "
            "hardware yet. If something is off, please open a device-support issue."
        )
        self.exp_badge.setStyleSheet(
            "QLabel{background:#3a2f12;color:#ffc857;border:1px solid #6b5520;border-radius:8px;"
            "padding:3px 8px;font-size:10px;font-weight:700;letter-spacing:1px;}"
        )
        self.exp_badge.setVisible(False)
        head.addWidget(self.exp_badge)
        head.addStretch(1)
        self.seg_buttons: Dict[str, QToolButton] = {}
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for i, (key, text, tip) in enumerate(
            [
                ("device", "Whole device", "Colors apply to the entire device (and synced devices)"),
                ("paint", "🖌 Paint keys", "Click or drag over keys to paint them with the active color"),
                (
                    "select",
                    "⬚ Select keys",
                    "Pick keys (click, Ctrl-click, drag a box); colors then apply only to them",
                ),
            ]
        ):
            b = QToolButton()
            b.setObjectName("segL" if i == 0 else ("segR" if i == 2 else "seg"))
            b.setStyleSheet(
                f"QToolButton{{border-radius:0;padding:7px 14px;background:#262a35;}}QToolButton:checked{{background:{ACCENT};font-weight:600;}}"
                + ("QToolButton{border-top-left-radius:9px;border-bottom-left-radius:9px;}" if i == 0 else "")
                + ("QToolButton{border-top-right-radius:9px;border-bottom-right-radius:9px;}" if i == 2 else "")
            )
            b.setCheckable(True)
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, k=key: self._set_interaction(k))
            seg.addWidget(b)
            self.seg_buttons[key] = b
        self.seg_buttons["device"].setChecked(True)
        head.addLayout(seg)
        cv_lay.addLayout(head)
        self.zone_row = QWidget()
        zl = QHBoxLayout(self.zone_row)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.setSpacing(8)
        self.zone_lbl = QLabel("LED count")
        self.zone_lbl.setObjectName("hint")
        zl.addWidget(self.zone_lbl)
        self.zone_spins: Dict[int, QSpinBox] = {}
        self.zone_row.setVisible(False)
        cv_lay.addWidget(self.zone_row)
        self.canvas = LedCanvas()
        self.canvas.paintRequested.connect(self._paint_leds)
        self.canvas.colorPicked.connect(lambda c: self.color_panel.setColor(c))
        self.canvas.selectionChanged.connect(self._selection_changed)
        cv_lay.addWidget(self.canvas, 1)
        self.hint = QLabel("")
        self.hint.setObjectName("hint")
        cv_lay.addWidget(self.hint)

        # action bar
        act = QHBoxLayout()
        act.setSpacing(6)
        self.fill_btn = QPushButton("Fill")
        self.fill_btn.setObjectName("primary")
        self.fill_btn.setToolTip("Fill the selection (or whole device) with the active color  [F]")
        self.fill_btn.clicked.connect(self._fill_active)
        act.addWidget(self.fill_btn)
        b = QPushButton("Rainbow →")
        b.clicked.connect(lambda: self._rainbow("h"))
        act.addWidget(b)
        b = QPushButton("Rainbow ↓")
        b.clicked.connect(lambda: self._rainbow("v"))
        act.addWidget(b)
        self.grad_a, self.grad_b = Swatch(QColor("#ff2d55"), 28), Swatch(QColor("#00b4ff"), 28)
        for sw, tip in (
            (self.grad_a, "Gradient start (click = set to active color)"),
            (self.grad_b, "Gradient end (click = set to active color)"),
        ):
            sw.setToolTip(tip)
        self.grad_a.clicked.connect(lambda: self.grad_a.setColor(self.color_panel.color()))
        self.grad_b.clicked.connect(lambda: self.grad_b.setColor(self.color_panel.color()))
        act.addWidget(self.grad_a)
        b = QPushButton("Gradient →")
        b.clicked.connect(lambda: self._gradient("h"))
        act.addWidget(b)
        b = QPushButton("Gradient ↓")
        b.clicked.connect(lambda: self._gradient("v"))
        act.addWidget(b)
        act.addWidget(self.grad_b)
        b = QPushButton("Random")
        b.clicked.connect(self._random_fill)
        act.addWidget(b)
        b = QPushButton("Black")
        b.clicked.connect(self._fill_black)
        act.addWidget(b)
        act.addStretch(1)
        act.addWidget(QLabel("Dim"))
        self.dim_slider = QSlider(Qt.Horizontal)
        self.dim_slider.setRange(5, 100)
        self.dim_slider.setValue(100)
        self.dim_slider.setFixedWidth(130)
        self.dim_slider.setToolTip("Dims every key without losing your colors")
        self.dim_slider.valueChanged.connect(self._dim_changed)
        act.addWidget(self.dim_slider)
        self.dim_val = QLabel("100%")
        self.dim_val.setFixedWidth(38)
        act.addWidget(self.dim_val)
        cv_lay.addLayout(act)
        left.addWidget(cv_card, 1)

        # presets
        pr_card, pr_lay = card("Presets")
        prow = QHBoxLayout()
        prow.setSpacing(8)
        self.presets_lay = QHBoxLayout()
        self.presets_lay.setSpacing(8)
        prow.addLayout(self.presets_lay)
        prow.addStretch(1)
        b = QPushButton("＋ Save current look")
        b.clicked.connect(self._save_preset)
        prow.addWidget(b, 0, Qt.AlignTop)
        pr_lay.addLayout(prow)
        self.preset_hint = QLabel(
            "A preset remembers every device's effect and colors. ★ = applied automatically when you log in."
        )
        self.preset_hint.setObjectName("hint")
        pr_lay.addWidget(self.preset_hint)
        left.addWidget(pr_card, 0)

        # right column
        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        c_card, c_lay = card("Color")
        self.color_panel = ColorPanel()
        self.color_panel.colorChanged.connect(self._active_color_changed)
        c_lay.addWidget(self.color_panel)
        right.addWidget(c_card)
        e_card, e_lay = card("Effect")
        self.effects = EffectsPanel()
        self.effects.modePicked.connect(self._mode_picked)
        self.effects.changed.connect(self._mode_params_changed)
        self.effects.requestColor.connect(lambda k: self.effects.set_color(k, self.color_panel.color()))
        self.effects.pickColor.connect(lambda c: self.color_panel.setColor(c))
        self.effects.save_btn.clicked.connect(self._save_mode_to_device)
        self.effects.all_btn.clicked.connect(self._effect_everywhere)
        self.effects.appEffectPicked.connect(self._app_effect_picked)
        self.effects.appSettingsChanged.connect(self._app_settings_changed)
        self.effects.test_btn.clicked.connect(self._fx_test)
        e_lay.addWidget(self.effects)
        right.addWidget(e_card)
        right.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(right_w)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(470)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_w.setMaximumWidth(452)
        body.addWidget(scroll, 0)

        self.statusBar().showMessage("")
        QShortcut(QKeySequence("Ctrl+A"), self, activated=self.canvas.select_all)
        QShortcut(QKeySequence("Escape"), self, activated=self.canvas.clear_selection)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self.canvas.invert_selection)
        QShortcut(QKeySequence("F"), self, activated=self._fill_active)
        QShortcut(QKeySequence("1"), self, activated=lambda: self._set_interaction("device"))
        QShortcut(QKeySequence("2"), self, activated=lambda: self._set_interaction("paint"))
        QShortcut(QKeySequence("3"), self, activated=lambda: self._set_interaction("select"))

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        a = QAction("Rescan devices", self)
        a.triggered.connect(self._rescan)
        m.addAction(a)
        a = QAction("Network lights…", self)
        a.triggered.connect(self._open_lights)
        m.addAction(a)
        a = QAction("Settings…", self)
        a.triggered.connect(self._open_settings)
        m.addAction(a)
        m.addSeparator()
        a = QAction("Quit", self)
        a.setShortcut(QKeySequence.Quit)
        a.triggered.connect(self.close)
        m.addAction(a)
        h = self.menuBar().addMenu("&Help")
        a = QAction("How to use", self)
        a.triggered.connect(self._show_help)
        h.addAction(a)
        a = QAction("Troubleshooting", self)
        a.triggered.connect(self._show_troubleshooting)
        h.addAction(a)
        a = QAction("Open log file", self)
        a.triggered.connect(lambda: open_in_desktop(LOG_FILE))
        h.addAction(a)
        h.addSeparator()
        a = QAction("About RGB Deck", self)
        a.triggered.connect(self._show_about)
        h.addAction(a)

    # ------------------------------------------------------------ devices
    def _populate_devices(self):
        sizes = self.settings.get("zone_sizes", {})
        for d in self.client.devices:
            for z in d.zones:
                want = sizes.get(f"{d.name}|{z.index}")
                if want is not None and z.leds_min != z.leds_max and want != z.leds_count:
                    try:
                        self.client.resize_zone(d, z.index, int(want))
                    except DeviceError:
                        pass
        for t in self.tabs:
            self.tabs_lay.removeWidget(t)
            t.deleteLater()
        self.tabs = []
        keep = self.dev.index if self.dev else None
        for d in self.client.devices:
            self.base_colors.setdefault(d.index, list(d.colors))
            self.dim.setdefault(d.index, 100)
            t = DeviceTab(d)
            t.clicked.connect(lambda _c=False, idx=d.index: self._select_device(idx))
            self.tabs_lay.addWidget(t)
            self.tabs.append(t)
        if self.client.devices:
            if keep is None or keep >= len(self.client.devices):
                kb = [d.index for d in self.client.devices if d.type_name == "Keyboard"]
                keep = kb[0] if kb else 0
            self._select_device(keep)
        else:
            self.dev = None
            self.dev_title.setText("No devices")
            self.canvas.set_device([], [], "", [])
            self.effects.set_device(None)
        msg = f"{len(self.client.devices)} device(s) connected directly"
        if self.client.last_error:
            msg += f" · {self.client.last_error}"
        self.statusBar().showMessage(msg)

    def _select_device(self, idx: int):
        self.dev = self.client.devices[idx]
        for t in self.tabs:
            t.setChecked(t.dev.index == idx)
        d = self.dev
        title = f"{ICONS.get(d.type_name, '●')}  {d.name}"
        info = self.client.battery(d)
        if info:
            if True:
                pct, wired = info
                title += (
                    f"  <span style='color:#9aa0b4;font-size:13px;font-weight:normal'>🔋 {pct}%"
                    f"{' · on cable' if wired else ''}</span>"
                )
        self.dev_title.setText(title)
        self.exp_badge.setVisible(bool(getattr(d, "experimental", False)))
        geoms = build_geometry(d)
        self.canvas.set_device(geoms, d.colors, d.type_name, device_colors_preview(d))
        self._build_zone_row(d)
        has_keys = bool(geoms)
        for k in ("paint", "select"):
            self.seg_buttons[k].setEnabled(has_keys)
        if not has_keys:
            self._set_interaction("device")
        self.effects.set_device(d)
        typed = d.type_name == "Keyboard" or any(n.startswith("Key: ") for n in d.led_names)
        self.effects.show_app_section(has_keys and typed)
        self.effects.set_app_effect(
            self.engine_kind if (self.engine_dev is d) else None,
            self.engine.settings if (self.engine_dev is d and self.engine) else None,
        )
        self.dim_slider.blockSignals(True)
        self.dim_slider.setValue(self.dim.get(d.index, 100))
        self.dim_val.setText(f"{self.dim_slider.value()}%")
        self.dim_slider.blockSignals(False)
        self.dim_slider.setEnabled(has_keys)
        self._selection_changed()
        self._refresh_tabs()

    def _build_zone_row(self, d: Device):
        lay = self.zone_row.layout()
        while lay.count() > 1:
            w = lay.takeAt(1).widget()
            if w:
                w.deleteLater()
        self.zone_spins = {}
        resizable = [z for z in d.zones if z.leds_min != z.leds_max]
        self.zone_row.setVisible(bool(resizable))
        for z in resizable:
            lbl = QLabel(z.name)
            sp = QSpinBox()
            sp.setRange(z.leds_min, z.leds_max)
            sp.setValue(z.leds_count)
            sp.setToolTip(f"How many LEDs are connected to {z.name} ({z.leds_min}–{z.leds_max})")
            sp.editingFinished.connect(lambda zi=z.index, box=sp: self._resize_zone(zi, box.value()))
            lay.addWidget(lbl)
            lay.addWidget(sp)
            self.zone_spins[z.index] = sp
        lay.addStretch(1)

    def _resize_zone(self, zone_index: int, count: int):
        if not self.dev:
            return
        try:
            if self.client.resize_zone(self.dev, zone_index, count):
                self.settings.setdefault("zone_sizes", {})[f"{self.dev.name}|{zone_index}"] = count
                save_json(SETTINGS_FILE, self.settings)
                self._rescan()
        except DeviceError as e:
            self.statusBar().showMessage(f"Device error: {e}")

    def _refresh_tabs(self):
        for t in self.tabs:
            t.update()

    def _set_interaction(self, key: str):
        self.canvas.mode = key
        for k, b in self.seg_buttons.items():
            b.setChecked(k == key)
        self.canvas.setCursor({"device": Qt.ArrowCursor, "paint": Qt.PointingHandCursor, "select": Qt.CrossCursor}[key])
        if key != "select":
            self.canvas.clear_selection()
        self._selection_changed()

    def _selection_changed(self):
        n = len(self.canvas.selected)
        mode = self.canvas.mode
        if mode == "device":
            txt = "Pick a color or effect on the right — it applies to the whole device"
            txt += " and every synced device." if self.sync_chk.isChecked() else "."
        elif mode == "paint":
            txt = "Click or drag across keys to paint them with the active color. Right-click a key to grab its color."
        else:
            txt = (
                f"{n} key(s) selected — colors now apply only to them. "
                if n
                else "Click keys, Ctrl-click to toggle, or drag a box. "
            ) + "Ctrl+A all · Esc clear · Ctrl+I invert"
        self.hint.setText(txt)

    def _sync_toggled(self, on: bool):
        self.settings["sync"] = on
        save_json(SETTINGS_FILE, self.settings)
        self._selection_changed()

    # --------------------------------------------------------- per-LED
    def _per_led_mode(self, d: Device) -> Optional[Mode]:
        c = [m for m in d.modes if m.color_mode == MODE_COLORS_PER_LED]
        pref = [m for m in c if m.name.lower() == "direct"]
        return (pref or c or [None])[0]

    def _ensure_per_led(self, d: Device) -> bool:
        if d.current_mode.color_mode == MODE_COLORS_PER_LED:
            return True
        m = self._per_led_mode(d)
        if not m:
            return False
        self.client.update_mode(d, m)
        if d is self.dev:
            self.effects.refresh_active()
        return True

    def _paint_leds(self, leds: List[int]):
        if not self.dev or not self._ensure_per_led(self.dev):
            return
        col = qcolor_to_u32(self.color_panel.color())
        base = self.base_colors.setdefault(self.dev.index, list(self.dev.colors))
        for led in leds:
            if led < len(base):
                base[led] = col
        self._queue_leds(self.dev)

    def _set_targets(self, mapping: Dict[int, int], dev: Optional[Device] = None):
        d = dev or self.dev
        if not d or not self._ensure_per_led(d):
            return
        base = self.base_colors.setdefault(d.index, list(d.colors))
        for led, col in mapping.items():
            if led < len(base):
                base[led] = col
        self._queue_leds(d)

    def _queue_leds(self, d: Device):
        self._pending_leds.add(d.index)
        if not self._flush_timer.isActive():
            self._flush_timer.start(50)

    def _flush(self):
        if not self.client.connected:
            return
        try:
            for idx in list(self._pending_leds):
                d = self.client.devices[idx]
                base = self.base_colors[idx]
                f = self.dim.get(idx, 100) / 100.0
                out = (
                    list(base) if f >= 0.999 else [rgb_to_u32(*(int(x * f + 0.5) for x in u32_to_rgb(c))) for c in base]
                )
                if self.engine and self.engine_dev is d:
                    self.engine.background = out  # effect renders on top of the new colors
                    self._fx_last_frame = None
                else:
                    self.client.update_leds(d, out)
                if d is self.dev:
                    self.canvas.set_colors(out)
            self._pending_leds.clear()
            self._refresh_tabs()
        except DeviceError as e:
            self.statusBar().showMessage(f"Lost connection: {e}")
            self._try_reconnect()

    # ------------------------------------------------------- color apply
    def _apply_color_to_device(self, d: Device, c: QColor, leds: Optional[Set[int]] = None):
        """Whole device (or given LEDs) takes color c, whatever kind of device it is."""
        col = qcolor_to_u32(c)
        per_led = self._per_led_mode(d)
        if (
            per_led
            and d.led_names
            and (leds or d.current_mode.color_mode == MODE_COLORS_PER_LED or not d.current_mode.has_mode_colors)
        ):
            targets = leds if leds else range(len(d.colors))
            self._set_targets({i: col for i in targets}, d)
            return
        prev_mode, prev_cm = d.active_mode, d.current_mode.color_mode
        m = d.current_mode
        if not m.has_mode_colors:
            m = d.mode_named("Static") or next((x for x in d.modes if x.has_mode_colors), None)
            if not m:
                return
        if m.color_mode == MODE_COLORS_RANDOM:
            m.color_mode = MODE_COLORS_MODE_SPECIFIC
        n = len(m.colors) or max(1, m.colors_min)
        m.colors = [col] * n
        self.client.update_mode(d, m)
        if d is self.dev:
            if m.index != prev_mode or m.color_mode != prev_cm:
                self.effects.refresh_active()  # effect switched: rebuild once
            else:
                self.effects.sync_colors()  # same effect: just recolor swatches
            self.canvas.set_fallback(device_colors_preview(d))

    def _active_color_changed(self, c: QColor, live: bool):
        if not self.dev:
            return
        try:
            if self.canvas.mode == "paint":
                return  # just sets the brush
            if self.canvas.mode == "select":
                if self.canvas.selected:
                    self._apply_color_to_device(self.dev, c, set(self.canvas.selected))
                return
            # whole device (+ sync)
            targets = self.client.devices if self.sync_chk.isChecked() else [self.dev]
            for d in targets:
                if (
                    self._per_led_mode(d)
                    and d.led_names
                    and (d.current_mode.color_mode == MODE_COLORS_PER_LED or not d.current_mode.has_mode_colors)
                ):
                    self._apply_color_to_device(d, c)  # per-LED: coalesced by the frame timer
                else:
                    # effect-colored devices (mouse): a mode write per wheel pixel floods the firmware,
                    # so send at most every 150 ms while dragging and always once on release
                    self._pending_mode_color[d.index] = QColor(c)
                    if not live:
                        self._mode_timer.stop()
                        self._flush_mode_colors()
                    elif not self._mode_timer.isActive():
                        self._mode_timer.start(150)
            self._refresh_tabs()
        except DeviceError as e:
            self.statusBar().showMessage(f"Lost connection: {e}")
            self._try_reconnect()

    def _flush_mode_colors(self):
        pending, self._pending_mode_color = self._pending_mode_color, {}
        try:
            for idx, c in pending.items():
                self._apply_color_to_device(self.client.devices[idx], c)
            self._refresh_tabs()
        except DeviceError as e:
            self.statusBar().showMessage(f"Lost connection: {e}")
            self._try_reconnect()

    def _fill_active(self):
        if not self.dev:
            return
        c = self.color_panel.color()
        if self.canvas.mode == "select" and self.canvas.selected:
            self._apply_color_to_device(self.dev, c, set(self.canvas.selected))
        else:
            for d in self.client.devices if self.sync_chk.isChecked() else [self.dev]:
                self._apply_color_to_device(d, c)
        self._refresh_tabs()

    def _fill_black(self):
        self._set_targets({g.led: 0 for g in self.canvas.targets()})

    def _random_fill(self):
        mp = {}
        for g in self.canvas.targets():
            r, gg, b = colorsys.hsv_to_rgb(random.random(), 1.0, 1.0)
            mp[g.led] = rgb_to_u32(int(r * 255), int(gg * 255), int(b * 255))
        self._set_targets(mp)

    def _axis(self, targets, axis):
        vals = [g.x + g.w / 2 for g in targets] if axis == "h" else [g.y + g.h / 2 for g in targets]
        lo, hi = min(vals), max(vals)
        return vals, lo, max(hi, lo + 1e-6)

    def _rainbow(self, axis):
        t = self.canvas.targets()
        if not t:
            return
        vals, lo, hi = self._axis(t, axis)
        mp = {}
        for g, v in zip(t, vals):
            r, gg, b = colorsys.hsv_to_rgb(((v - lo) / (hi - lo)) * 0.95, 1.0, 1.0)
            mp[g.led] = rgb_to_u32(int(r * 255), int(gg * 255), int(b * 255))
        self._set_targets(mp)

    def _gradient(self, axis):
        t = self.canvas.targets()
        if not t:
            return
        vals, lo, hi = self._axis(t, axis)
        a, b = self.grad_a.color(), self.grad_b.color()
        mp = {}
        for g, v in zip(t, vals):
            k = (v - lo) / (hi - lo)
            mp[g.led] = rgb_to_u32(
                int(a.red() + (b.red() - a.red()) * k + 0.5),
                int(a.green() + (b.green() - a.green()) * k + 0.5),
                int(a.blue() + (b.blue() - a.blue()) * k + 0.5),
            )
        self._set_targets(mp)

    def _dim_changed(self, v):
        self.dim_val.setText(f"{v}%")
        if self.dev:
            self.dim[self.dev.index] = v
            if self.dev.current_mode.color_mode == MODE_COLORS_PER_LED:
                self._queue_leds(self.dev)

    # ------------------------------------------------------------- modes
    def _mode_picked(self, m: Mode):
        if not self.dev:
            return
        if self.engine_dev is self.dev:
            self._stop_app_effect()
        try:
            if m.has_mode_colors and m.color_mode != MODE_COLORS_RANDOM and all(c == 0 for c in m.colors):
                m.colors = [qcolor_to_u32(self.color_panel.color())] * max(1, len(m.colors) or m.colors_min)
            self.client.update_mode(self.dev, m)
            if m.color_mode == MODE_COLORS_PER_LED:
                self._queue_leds(self.dev)
            self.effects.refresh_active()
            self.canvas.set_fallback(device_colors_preview(self.dev))
            self._refresh_tabs()
            self.statusBar().showMessage(f"{self.dev.name}: {m.name}", 2500)
        except DeviceError as e:
            self.statusBar().showMessage(f"Lost connection: {e}")
            self._try_reconnect()

    def _mode_params_changed(self):
        if not self.dev:
            return
        try:
            self.client.update_mode(self.dev, self.dev.current_mode)
            self.canvas.set_fallback(device_colors_preview(self.dev))
            self._refresh_tabs()
        except DeviceError as e:
            self.statusBar().showMessage(f"Lost connection: {e}")
            self._try_reconnect()

    def _save_mode_to_device(self):
        if self.dev:
            self.client.update_mode(self.dev, self.dev.current_mode, save=True)
            self.statusBar().showMessage(f"Stored '{self.dev.current_mode.name}' in {self.dev.name}'s memory", 4000)

    def _effect_everywhere(self):
        if not self.dev:
            return
        src = self.dev.current_mode
        for d in self.client.devices:
            if d is self.dev:
                continue
            m = d.mode_named(src.name)
            if not m:
                continue
            if m.has_speed and src.has_speed:
                lo, hi = sorted((m.speed_min, m.speed_max))
                slo, shi = sorted((src.speed_min, src.speed_max))
                m.speed = int(lo + (src.speed - slo) / max(1, shi - slo) * (hi - lo))
            if m.has_brightness and src.has_brightness:
                lo, hi = sorted((m.brightness_min, m.brightness_max))
                slo, shi = sorted((src.brightness_min, src.brightness_max))
                m.brightness = int(lo + (src.brightness - slo) / max(1, shi - slo) * (hi - lo))
            if m.has_mode_colors and src.colors:
                m.colors = [
                    src.colors[i % len(src.colors)]
                    for i in range(max(m.colors_min, min(len(src.colors), m.colors_max)))
                ]
                m.color_mode = src.color_mode if src.color_mode != MODE_COLORS_PER_LED else m.color_mode
            self.client.update_mode(d, m)
        self._refresh_tabs()
        self.statusBar().showMessage(f"'{src.name}' applied to every device that has it", 3000)

    def _everything_off(self):
        for d in self.client.devices:
            off = d.mode_named("Off")
            if off:
                self.client.update_mode(d, off)
            elif d.led_names and self._per_led_mode(d):
                self.client.update_mode(d, self._per_led_mode(d))
                self.client.update_leds(d, [0] * len(d.colors))
        if self.dev:
            self._select_device(self.dev.index)

    # ------------------------------------------------------- app effects
    def _app_effect_picked(self, kind: str):
        if not self.dev or not self.canvas.geoms:
            return
        self._start_app_effect(self.dev, kind, self.effects.app_settings)

    def _start_app_effect(self, d: Device, kind: str, settings: EffectSettings):
        self._stop_app_effect(keep_mode=True)
        if not self._ensure_per_led(d):
            self.statusBar().showMessage("This device has no per-key mode, so typing effects can't run on it.", 5000)
            return
        geoms = build_geometry(d)
        pos = {g.led: (g.x + g.w / 2, g.y + g.h / 2) for g in geoms}
        name_to_led = {(n[5:] if n.startswith("Key: ") else n): i for i, n in enumerate(d.led_names)}
        settings.kind = kind
        settings.fps = int(self.settings.get("fx_fps", settings.fps))
        self.engine = EffectEngine(pos, name_to_led, lambda: self._active_rgb())
        self.engine.settings = settings
        self.engine_dev, self.engine_kind = d, kind
        self._queue_leds(d)  # pushes current colors into engine.background
        paths = find_keyboard_events(d.name)
        self.listener = KeyListener(paths, self.engine.key_pressed)
        self.listener.start()
        QTimer.singleShot(400, self._report_listener)
        self._fx_timer.start(int(1000 / max(10, settings.fps)))
        self.effects.set_app_effect(kind, settings)
        self.tray.setToolTip(f"{APP_NAME} — {kind} running")
        self._refresh_tabs()

    def _report_listener(self):
        if not self.listener:
            return
        if self.listener.ok:
            where = "(global keyboard hook)" if IS_WINDOWS else f"on {len(self.listener.paths)} input device(s)"
            self.effects.listen_lbl.setText(
                f"Listening for key presses {where}. Keep RGB Deck running (it can sit in the tray) for the effect to work."
            )
        else:
            why = "" if IS_WINDOWS else ". Your user needs read access to /dev/input/event* (the 'input' group)."
            self.effects.listen_lbl.setText(
                "Couldn't read key presses: " + (self.listener.error or "no keyboard input device found") + why
            )

    def _stop_app_effect(self, keep_mode: bool = False):
        if self.listener:
            self.listener.stop()
            self.listener = None
        self._fx_timer.stop()
        self._pulse_followers(restore=True)
        if self.engine and self.engine_dev:
            d = self.engine_dev
            base = self.base_colors.get(d.index, list(d.colors))
            self.engine = None
            self.engine_dev = self.engine_kind = None
            try:
                self.client.update_leds(d, base)  # back to the plain colors
            except DeviceError:
                pass
            if d is self.dev:
                self.canvas.set_colors(base)
        self.engine = None
        self.engine_dev = self.engine_kind = None
        self.effects.set_app_effect(None)
        self.tray.setToolTip(APP_NAME)

    def _active_rgb(self):
        c = self.color_panel.color()
        return c.red(), c.green(), c.blue()

    def _app_settings_changed(self):
        if self.engine:
            self.engine.settings = self.effects.app_settings
            self._fx_timer.setInterval(int(1000 / max(10, self.engine.settings.fps)))

    def _fx_test(self):
        if self.engine and self.engine.name_to_led:
            self.engine.key_pressed(random.choice(list(self.engine.name_to_led)))

    def _fx_tick(self):
        if not self.engine or not self.engine_dev or not self.client.connected:
            return
        frame = self.engine.tick()
        if frame is None:
            return
        self._pulse_followers()
        if frame == self._fx_last_frame:
            return
        self._fx_last_frame = frame
        try:
            self.client.update_leds(self.engine_dev, frame)
        except DeviceError as e:
            self.statusBar().showMessage(f"Lost connection: {e}")
            self._stop_app_effect()
            self._try_reconnect()
            return
        if self.engine_dev is self.dev:
            self.canvas.set_colors(frame)

    # ----------------------------------------------- lights that pulse along
    def _shown_base(self, d: Device) -> List[int]:
        """The device's plain colors as they are sent (base colors with its dimming applied)."""
        base = self.base_colors.get(d.index, list(d.colors))
        f = self.dim.get(d.index, 100) / 100.0
        return list(base) if f >= 0.999 else [rgb_to_u32(*(int(x * f + 0.5) for x in u32_to_rgb(c))) for c in base]

    def _pulse_followers(self, restore: bool = False):
        """Room lights have no keys to ripple across, so they flash toward the color of each key press while a
        typing effect runs on the keyboard, and fall back to their own colors as it fades."""
        level, col = (0.0, (0, 0, 0)) if restore or not self.engine else self.engine.activity()
        quiet = level < 0.02
        if quiet and not self._follow_last:
            return
        for d in self.client.devices:
            pulsing = d.index in self._follow_last
            if d is self.engine_dev or not (pulsing or self.settings.get("fx_follow", True)):
                continue
            if not getattr(self.client.driver_for(d), "follows_typing", False):
                continue
            if d.current_mode.color_mode != MODE_COLORS_PER_LED:
                continue  # showing a scene or white: leave it alone
            base = self._shown_base(d)
            if not any(base):
                continue  # the light is off: typing must not switch it on
            if quiet:
                out = base
            else:
                k = min(1.0, level) * 0.9
                out = [rgb_to_u32(*(int(a + (b - a) * k) for a, b in zip(u32_to_rgb(c), col))) for c in base]
            if self._follow_last.get(d.index) == out or (quiet and not pulsing):
                continue
            self._follow_last[d.index] = out
            try:
                self.client.update_leds(d, out)
            except DeviceError:
                continue
            if d is self.dev:
                self.canvas.set_colors(out)
        if quiet:
            self._follow_last.clear()

    # ----------------------------------------------------------- presets
    def _snapshot(self) -> dict:
        return {
            "devices": [
                {
                    "name": d.name,
                    "serial": d.serial,
                    "type": d.type_name,
                    "active_mode": d.active_mode,
                    "modes": {
                        m.name: {
                            "speed": m.speed,
                            "brightness": m.brightness,
                            "direction": m.direction,
                            "color_mode": m.color_mode,
                            "colors": m.colors,
                        }
                        for m in d.modes
                    },
                    "colors": self.base_colors.get(d.index, list(d.colors)),
                    "dim": self.dim.get(d.index, 100),
                }
                for d in self.client.devices
            ],
            "app_effect": (
                {
                    "device": self.engine_dev.name,
                    "active_color": qcolor_to_u32(self.color_panel.color()),
                    **self.engine.settings.to_dict(),
                }
                if self.engine and self.engine_dev
                else None
            ),
        }

    def _refresh_presets(self):
        while self.presets_lay.count():
            w = self.presets_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        auto = self.settings.get("autostart_preset") if autostart_enabled("apply") else None
        for name in sorted(self.presets):
            c = PresetCard(name, self.presets[name], name == auto)
            c.clicked.connect(lambda _c=False, n=name: self._apply_preset(n))
            c.setContextMenuPolicy(Qt.CustomContextMenu)
            c.customContextMenuRequested.connect(lambda pos, n=name, w=c: self._preset_menu(n, w.mapToGlobal(pos)))
            self.presets_lay.addWidget(c)
        if not self.presets:
            lbl = QLabel("No presets yet — set things up how you like, then “Save current look”.")
            lbl.setObjectName("hint")
            self.presets_lay.addWidget(lbl)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Name this look:")
        if ok and name.strip():
            self.presets[name.strip()] = self._snapshot()
            save_json(PRESETS_FILE, self.presets)
            self._refresh_presets()
            self.statusBar().showMessage(f"Saved preset '{name.strip()}'", 3000)

    def _apply_preset(self, name):
        if name in self.presets:
            snap = self.presets[name]
            self._stop_app_effect()
            try:
                apply_snapshot(self.client, snap, self.base_colors, self.dim)
            except DeviceError as e:
                self.statusBar().showMessage(f"Lost connection: {e}")
                return
            fx = snap.get("app_effect")
            if fx:
                d = next((x for x in self.client.devices if x.name == fx.get("device")), None)
                if d:
                    self._start_app_effect(d, fx.get("kind", "ripple"), EffectSettings.from_dict(fx))
            if self.dev:
                self._select_device(self.dev.index)
            self.statusBar().showMessage(f"Preset '{name}' applied", 3000)

    def _preset_menu(self, name, pos):
        menu = QMenu(self)
        a_apply = menu.addAction("Apply")
        auto_on = self.settings.get("autostart_preset") == name and autostart_enabled("apply")
        a_auto = menu.addAction("Don't apply at login" if auto_on else "★ Apply at login")
        a_over = menu.addAction("Overwrite with current look")
        a_ren = menu.addAction("Rename…")
        menu.addSeparator()
        a_del = menu.addAction("Delete")
        act = menu.exec(pos)
        if act == a_apply:
            self._apply_preset(name)
        elif act == a_auto:
            self._set_autostart(None if auto_on else name)
        elif act == a_over:
            self.presets[name] = self._snapshot()
            save_json(PRESETS_FILE, self.presets)
            self._refresh_presets()
        elif act == a_ren:
            new, ok = QInputDialog.getText(self, "Rename preset", "New name:", text=name)
            if ok and new.strip() and new.strip() != name:
                self.presets[new.strip()] = self.presets.pop(name)
                if self.settings.get("autostart_preset") == name:
                    self._set_autostart(new.strip())
                save_json(PRESETS_FILE, self.presets)
                self._refresh_presets()
        elif act == a_del:
            if QMessageBox.question(self, "Delete preset", f"Delete '{name}'?") == QMessageBox.Yes:
                self.presets.pop(name, None)
                save_json(PRESETS_FILE, self.presets)
                if self.settings.get("autostart_preset") == name:
                    self._set_autostart(None)
                self._refresh_presets()

    def _set_autostart(self, name: Optional[str]):
        if name:
            self.settings["autostart_preset"] = name
        else:
            self.settings.pop("autostart_preset", None)
        try:
            set_autostart("apply", ["--apply", name] if name else None)
        except OSError as e:
            log.error("could not update the login entry: %s", e)
        save_json(SETTINGS_FILE, self.settings)
        self._refresh_presets()

    # ------------------------------------------------------------ connection
    def _rescan(self):
        self.start_scan()

    def start_scan(self):
        """Probe devices on a background thread so the window stays responsive (some probes take seconds)."""
        if self._scanning:
            return
        self._scanning = True
        self._scan_error = ""
        if self.engine and self.engine_dev:
            self._scan_resume_fx = (self.engine_dev.name, self.engine_kind, self.engine.settings)
            self._stop_app_effect()
        self.statusBar().showMessage("Scanning for devices…")
        threading.Thread(target=self._scan_worker, name="rgbdeck-scan", daemon=True).start()

    def _scan_worker(self):
        try:
            self.client.refresh()
        except Exception as e:  # noqa: BLE001 - report, never crash the UI
            log.exception("device scan failed")
            self._scan_error = str(e)
        self.scanFinished.emit()

    def _scan_done(self):
        self._scanning = False
        self._follow_last.clear()
        self.base_colors = {d.index: list(d.colors) for d in self.client.devices}
        self.dim = {d.index: self.dim.get(d.index, 100) for d in self.client.devices}
        self._populate_devices()
        fx, self._scan_resume_fx = self._scan_resume_fx, None
        if fx:
            d = next((x for x in self.client.devices if x.name == fx[0]), None)
            if d:
                self._start_app_effect(d, fx[1], fx[2])
        err = self._scan_error or self.client.last_error
        if not self.client.devices:
            if "can't open" in err or "Permission" in err:
                self.statusBar().showMessage(
                    "Found devices but can't access them — close the vendor software (see Help → Troubleshooting)."
                    if IS_WINDOWS
                    else "Found devices but can't access them — install the udev rule (see Help → Troubleshooting)."
                )
                self.hint.setText("Permission problem: " + err)
            else:
                self.statusBar().showMessage(
                    "No supported devices connected — plug one in, it will be picked up automatically."
                )
        elif err:
            self.statusBar().showMessage(f"{len(self.client.devices)} device(s) · some failed: {err}", 8000)

    def _poll_server(self):
        if self._scanning:
            return
        err = self.client.take_error()
        if err:
            self.statusBar().showMessage(f"Device error: {err}")
            if "gone" in err or "did not answer" in err:
                self._try_reconnect()
            return
        try:
            if self.client.poll_notifications():
                self._rescan()
        except DeviceError:
            self._try_reconnect()

    def _try_reconnect(self):
        self.statusBar().showMessage("A device stopped answering — looking again…")
        self.dev = None
        self.start_scan()

    def _open_lights(self):
        dlg = LightsDialog(self)
        dlg.exec()
        if dlg.changed:
            self.start_scan()

    def _open_settings(self):
        dlg = SettingsDialog(dict(self.settings), self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.settings.update(dlg.apply())
        save_json(SETTINGS_FILE, self.settings)
        self.sync_chk.setChecked(self.settings.get("sync", True))
        if self.engine:
            self.engine.settings.fps = int(self.settings.get("fx_fps", 24))
            self._fx_timer.setInterval(int(1000 / max(10, self.engine.settings.fps)))
        if dlg.network_changed:
            self.start_scan()

    def _show_troubleshooting(self):
        rules = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "70-rgbdeck.rules")
        access = (
            "<b>Device found but can't be accessed</b><br>Another program is holding it. Quit the vendor software "
            "(Logitech G HUB, ASUS Armoury Crate / Aura, Glorious Core, Corsair iCUE, Razer Synapse, SteelSeries GG, "
            "HyperX NGENUITY) including its tray icon, then <b>File → Rescan devices</b>.<br><br>"
            if IS_WINDOWS
            else "<b>Device found but can't be accessed</b><br>Install the udev rule once, then unplug/replug:<br>"
            f"<code>sudo install -m644 {rules} /etc/udev/rules.d/ &amp;&amp; sudo udevadm control --reload "
            "&amp;&amp; sudo udevadm trigger</code><br><br>"
        )
        typing = (
            "<b>Typing effects don't react</b><br>They use a global keyboard hook; some anti-cheat or security "
            "software blocks those while a game runs.<br><br>"
            if IS_WINDOWS
            else "<b>Typing effects don't react</b><br>They read /dev/input; your user needs access (logind grants it to "
            "the active session). Check the hint under the effect chips.<br><br>"
        )
        QMessageBox.information(
            self,
            "Troubleshooting",
            access + "<b>Govee light not found</b><br>Switch on <b>LAN Control</b> for the light in the Govee Home "
            "app (open the light, settings gear, LAN Control). Keep it on the same network as this PC and let "
            "incoming UDP port 4002 through your firewall. Settings → Network lights can also add it by address."
            "<br><br><b>Device not listed</b><br>Only families with a driver are detected (see the README table). "
            "Another program talking to the device (vendor software, OpenRGB…) must be closed first.<br><br>"
            "<b>Colors don't change on a Glorious mouse</b><br>Its lighting is stored per onboard profile; "
            "RGB Deck writes all three, so this should not happen — if it does, press the profile button once.<br><br>"
            + typing
            + f"<b>Logs</b><br>{LOG_FILE} (run <code>rgbdeck --verbose</code> for more detail).",
        )

    def _show_about(self):
        QMessageBox.about(
            self,
            "About RGB Deck",
            f"<h3>RGB Deck {__version__}</h3>"
            "<p>Standalone RGB lighting control — talks to your devices directly, no vendor software.</p>"
            "<p><a href='https://github.com/dancingduck425/rgbdeck'>github.com/dancingduck425/rgbdeck</a></p>"
            "<p style='color:#9aa0b4'>GPL-2.0. Protocols learned from GPL-2.0 driver sources; RGB Deck does not use "
            "or depend on them at runtime.</p>",
        )

    def _show_help(self):
        QMessageBox.information(
            self,
            "How to use RGB Deck",
            "<b>Quick color</b> — with <i>Whole device</i> selected (the default), click any palette swatch or "
            "drag the wheel and the device changes instantly. Turn on <i>Sync all devices</i> to recolor the "
            "keyboard and mouse together.<br><br>"
            "<b>Paint keys</b> — click/drag over keys to paint them. Right-click a key to grab its color.<br>"
            "<b>Select keys</b> — click, Ctrl-click or drag a box; the wheel/palette then recolors just those keys. "
            "Rainbow, Gradient, Random and Black respect the selection too.<br><br>"
            "<b>Effects</b> — chips on the right; speed, brightness, direction and the effect's colors appear when "
            "available. <i>Keep on device</i> writes it into the hardware's own memory.<br><br>"
            "<b>Presets</b> — save the whole look; right-click a preset for ★ Apply at login.<br><br>"
            "Keys: 1/2/3 switch modes · F fill · Ctrl+A select all · Esc clear · Ctrl+I invert",
        )

    def _start_control_socket(self):
        try:
            self._ctl = control_listen()
        except OSError as e:
            log.warning("control channel unavailable: %s", e)
            self._ctl = None
        if self._ctl is None:
            return
        self._ctl_notifier = QSocketNotifier(self._ctl.fileno(), QSocketNotifier.Read, self)
        self._ctl_notifier.activated.connect(self._control_request)

    def _control_request(self):
        try:
            conn, _ = self._ctl.accept()
        except OSError:
            return
        try:
            conn.settimeout(1.0)
            data = b""
            while b"\n" not in data and len(data) < 4096:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.decode(errors="replace").strip()
            if TCP_CONTROL:
                token, _, line = line.partition(" ")
                if token != CONTROL_TOKEN:
                    conn.sendall(b"denied")
                    return
            cmd, _, arg = line.partition(" ")
            if cmd == "apply":
                if arg in self.presets:
                    self._apply_preset(arg)
                    reply = f"applied {arg}"
                else:
                    reply = f"no preset named {arg!r}"
            elif cmd == "show":
                self._show_from_tray()
                reply = "ok"
            elif cmd == "ping":
                reply = "pong"
            else:
                reply = "unknown command"
            conn.sendall(reply.encode())
        except OSError:
            pass
        finally:
            conn.close()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon() if not self.windowIcon().isNull() else QIcon(), self)
        menu = QMenu()
        a = menu.addAction("Show RGB Deck")
        a.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        a = menu.addAction("Quit (effects stop, colors stay)")
        a.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(APP_NAME)
        self.tray.activated.connect(lambda r: self._toggle_from_tray() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def _toggle_from_tray(self):
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            self.hide()
        else:
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._really_quit = True
        self.close()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh_presets()

    def closeEvent(self, e):
        self.settings["palette"] = self.color_panel.palette_colors
        save_json(SETTINGS_FILE, self.settings)
        if self.engine and not getattr(self, "_really_quit", False) and self.tray.isVisible():
            # a typing effect is running: keep it alive in the tray
            e.ignore()
            self.hide()
            if not self.settings.get("tray_hint_shown"):
                self.tray.showMessage(
                    APP_NAME,
                    "Still running in the tray so your typing effect keeps working. Right-click the tray icon to quit.",
                    QSystemTrayIcon.Information,
                    6000,
                )
                self.settings["tray_hint_shown"] = True
                save_json(SETTINGS_FILE, self.settings)
            return
        self._stop_app_effect()
        self.tray.hide()
        self._keep_look_on_close()
        self.client.wait_idle(4.0)
        super().closeEvent(e)

    def _keep_look_on_close(self):
        """Make the current look outlive the app: store it in each device's memory where the hardware can, and
        re-apply what it can't (multi-color key layouts) at login through the 'Last look' preset."""
        if not self.settings.get("keep_on_close", True) or not self.client.devices:
            return
        needs_login = False
        for d in self.client.devices:
            if getattr(self.client.driver_for(d), "keeps_state", False):
                continue  # e.g. a network light: it remembers its own colour / scene / power
            mode = d.current_mode
            try:
                if mode.has_per_led:
                    keys = [c for c, n in zip(d.colors, d.led_names) if "Indicator" not in n] or list(d.colors)
                    static = d.mode_named("Static")
                    if keys and len(set(keys)) == 1 and static is not None and static.has_mode_colors:
                        static.colors = [keys[0]]
                        self.client.update_mode(d, static, save=True)  # one color: the device can remember it
                        log.info("kept %s as static %06x in device memory", d.name, keys[0])
                    else:
                        needs_login = True  # per-key layout: only the app can restore it
                else:
                    self.client.update_mode(d, mode, save=True)  # onboard effect: just ask the device to keep it
                    log.info("kept %s effect '%s' in device memory", d.name, mode.name)
            except DeviceError as e:
                log.warning("could not store the look in %s: %s", d.name, e)
        self.presets[LAST_LOOK] = self._snapshot()
        save_json(PRESETS_FILE, self.presets)
        if needs_login and not self.settings.get("autostart_preset"):
            self._set_autostart(LAST_LOOK)
            log.info("per-key layout: '%s' will be re-applied at login", LAST_LOOK)


# =============================================================================
# preset application (shared with --apply)
# =============================================================================
def apply_snapshot(client: LocalBackend, snap: dict, base_colors: Optional[dict] = None, dim: Optional[dict] = None):
    for entry in snap.get("devices", []):
        dev = next(
            (
                d
                for d in client.devices
                if d.name == entry["name"] and (not entry.get("serial") or d.serial == entry["serial"])
            ),
            None,
        ) or next((d for d in client.devices if d.name == entry["name"]), None)
        if not dev:
            continue
        for m in dev.modes:
            saved = entry.get("modes", {}).get(m.name)
            if saved:
                m.speed, m.brightness = saved.get("speed", m.speed), saved.get("brightness", m.brightness)
                m.direction, m.color_mode = saved.get("direction", m.direction), saved.get("color_mode", m.color_mode)
                if saved.get("colors") is not None:
                    m.colors = list(saved["colors"])
        am = entry.get("active_mode", dev.active_mode)
        if 0 <= am < len(dev.modes):
            client.update_mode(dev, dev.modes[am])
            if dev.modes[am].color_mode == MODE_COLORS_PER_LED and entry.get("colors"):
                cols = list(entry["colors"])[: len(dev.colors)]
                cols += [0] * (len(dev.colors) - len(cols))
                f = entry.get("dim", 100) / 100.0
                if base_colors is not None:
                    base_colors[dev.index] = cols
                if dim is not None:
                    dim[dev.index] = entry.get("dim", 100)
                out = cols if f >= 0.999 else [rgb_to_u32(*(int(x * f + 0.5) for x in u32_to_rgb(c))) for c in cols]
                client.update_leds(dev, out)


def run_headless_effect(client: LocalBackend, snap: dict) -> int:
    """Keep a typing effect running without a window (used by 'apply at login')."""
    import time as _time

    fx = snap["app_effect"]
    dev = next((d for d in client.devices if d.name == fx.get("device")), None)
    if not dev:
        return 0
    entry = next((e for e in snap["devices"] if e["name"] == dev.name), None)
    base = list(entry["colors"]) if entry and entry.get("colors") else list(dev.colors)
    geoms = build_geometry(dev)
    pos = {g.led: (g.x + g.w / 2, g.y + g.h / 2) for g in geoms}
    name_to_led = {(n[5:] if n.startswith("Key: ") else n): i for i, n in enumerate(dev.led_names)}
    settings = EffectSettings.from_dict(fx)
    r, g, b = u32_to_rgb(int(fx.get("active_color", 0x00B4FF)))
    engine = EffectEngine(pos, name_to_led, lambda: (r, g, b))
    engine.settings = settings
    engine.background = base
    listener = KeyListener(find_keyboard_events(dev.name), engine.key_pressed)
    listener.start()
    last = None
    period = 1.0 / max(10, settings.fps)
    try:
        while True:
            t = _time.monotonic()
            frame = engine.tick(t)
            if frame is not None and frame != last:
                last = frame
                client.update_leds(dev, frame)
            _time.sleep(max(0.0, period - (_time.monotonic() - t)))
    except (KeyboardInterrupt, DeviceError):
        listener.stop()
        return 0


def apply_theme(app: QApplication):
    """Dark look everywhere. The stylesheet only paints the main window; dialogs, menus and tooltips take
    their background from the palette, which on a light desktop theme (Windows' default) would leave the
    stylesheet's light text on a white dialog."""
    app.setStyle("Fusion")
    pal = QPalette()
    text, field, card = QColor("#e8e9ee"), QColor("#262a35"), QColor("#1b1d24")
    for role, col in (
        (QPalette.Window, card),
        (QPalette.WindowText, text),
        (QPalette.Base, field),
        (QPalette.AlternateBase, card),
        (QPalette.Text, text),
        (QPalette.Button, field),
        (QPalette.ButtonText, text),
        (QPalette.ToolTipBase, field),
        (QPalette.ToolTipText, text),
        (QPalette.Highlight, QColor(ACCENT)),
        (QPalette.HighlightedText, QColor("#ffffff")),
        (QPalette.PlaceholderText, QColor("#7d8398")),
        (QPalette.Link, QColor("#9385ff")),
    ):
        pal.setColor(role, col)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#666c7d"))
    app.setPalette(pal)
    app.setStyleSheet(QSS)


def app_icon_path() -> Optional[Path]:
    data = Path(__file__).resolve().parent / "data"
    for name in ("rgbdeck.ico", "rgbdeck.svg") if IS_WINDOWS else ("rgbdeck.svg", "rgbdeck.ico"):
        if (data / name).exists():
            return data / name
    return None


def selftest() -> int:
    """Exit 0 when this installation is complete: every driver imports, Qt starts, the icon is bundled."""
    setup_logging(True)
    from rgbdeck.backend import discover_drivers

    mods = discover_drivers()
    log.info("selftest: %d drivers: %s", len(mods), [m.__name__.rsplit(".", 1)[-1] for m in mods])
    # the HID library must really be in the build (PyInstaller can't see hidapi_backend's dynamic import)
    from rgbdeck.hw import hidraw

    try:
        found = hidraw.enumerate_hid(with_descriptors=False)
        hid_ok = True
        log.info("selftest: HID backend %s lists %d interfaces", hidraw.enumerate_hid.__module__, len(found))
    except DeviceError as e:
        hid_ok = False
        log.error("selftest: HID backend unusable: %s", e)
    icon = app_icon_path()
    log.info("selftest: icon %s", icon)
    app = QApplication.instance() or QApplication([])
    pix_ok = icon is not None and not QIcon(str(icon)).pixmap(32, 32).isNull()
    log.info("selftest: icon renders: %s · platform %s · Qt %s", pix_ok, app.platformName(), QT_VERSION_STR)
    ok = len(mods) >= 10 and pix_ok and hid_ok
    log.info("selftest: %s", "OK" if ok else "FAILED")
    print("selftest", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: List[str]) -> int:
    if "--apply" in argv:
        name = argv[argv.index("--apply") + 1]
        presets = load_json(PRESETS_FILE, {})
        if name not in presets:
            print(f"no preset named {name!r}", file=sys.stderr)
            return 1
        handed = send_control(f"apply {name}")
        if handed is not None:  # the GUI is running: let it do it (one controller per device)
            print(handed)
            return 0 if handed.startswith("applied") else 1
        c = LocalBackend()
        c.connect()
        snap = presets[name]
        apply_snapshot(c, snap)
        fx = snap.get("app_effect")
        if fx:
            return run_headless_effect(c, snap)
        c.close()
        return 0
    if "--list" in argv:
        for n in sorted(load_json(PRESETS_FILE, {})):
            print(n)
        return 0
    if "--version" in argv:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "--selftest" in argv:  # CI check of a bundled build: drivers discoverable, Qt + icon load, nothing else
        return selftest()

    if send_control("show") == "ok":  # already running: just raise that window
        return 0
    setup_logging("--verbose" in argv or "-v" in argv)
    log.info("RGB Deck starting")
    app = QApplication([a for a in argv if a not in ("--tray", "--verbose", "-v")])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("rgbdeck")
    apply_theme(app)
    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    client = LocalBackend()
    w = MainWindow(client)
    if "--tray" in argv and w.tray.isVisible():
        w.hide()
    else:
        w.show()
    w.start_scan()
    rc = app.exec()
    client.shutdown()
    if w._ctl is not None:
        forget_control_file()
    return rc


def main_entry():
    sys.exit(main(sys.argv))


if __name__ == "__main__":
    main_entry()
