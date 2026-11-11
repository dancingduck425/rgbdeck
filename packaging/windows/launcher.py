# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""PyInstaller entry point for the Windows build (see .github/workflows/ci.yml)."""

import sys

if __name__ == "__main__":
    if "--ble-helper" in sys.argv[1:]:  # the Bluetooth helper process: no window, none of the UI
        from rgbdeck.hw.govee_ble import helper_main

        sys.exit(helper_main())
    from rgbdeck.app import main_entry

    main_entry()
