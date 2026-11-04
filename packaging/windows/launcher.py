# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""PyInstaller entry point for the Windows build (see .github/workflows/ci.yml)."""

from rgbdeck.app import main_entry

if __name__ == "__main__":
    main_entry()
