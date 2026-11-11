# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""Entry point of the Bluetooth helper process (see hw/govee_ble.py). Imports nothing of the UI."""

import sys

from rgbdeck.hw import govee_ble

if __name__ == "__main__":
    sys.exit(govee_ble.helper_main())
