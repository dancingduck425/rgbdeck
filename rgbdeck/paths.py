# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""Where RGB Deck keeps its files (shared by the UI and the drivers that remember something)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def config_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def config_dir() -> Path:
    return config_root() / "rgbdeck"
