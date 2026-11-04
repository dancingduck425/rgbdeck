"""
Corsair Lighting Node PRO / CORE, Commander PRO (LED channels), LS100, LT100, Obsidian 1000D and
SPEC OMEGA RGB case controllers over /dev/hidraw.

EXPERIMENTAL: ported from OpenRGB sources, not yet verified on hardware.

Ported from OpenRGB Controllers/CorsairLightingNodeController/CorsairLightingNodeController.cpp
(packets), RGBController_CorsairLightingNode.cpp (zones and modes) and
CorsairLightingNodeControllerDetect.cpp (product ids).  Every packet is a 64-byte output report
(sent with a leading 0x00 report id) answered by a 16-byte reply; OpenRGB waits up to 15 ms for it.

Two "Corsair RGB Header" zones (OpenRGB uses two channels for every model, including the
single-channel Node CORE / LS100).  Both start at 0 LEDs: call ``resize_zone(idx, n)`` or set
``RGBDECK_CORSAIR_NODE_LEDS=<ch1>,<ch2>`` in the environment before starting RGB Deck.  Max 204
LEDs per channel.  Direct mode streams per-LED colors (0x38 port state, 0x32 colour planes in
blocks of 50, 0x33 commit); the onboard effects use 0x37 reset / 0x34 begin / 0x38 port state /
0x35 effect config / 0x33 commit with 0x39 brightness, exactly as upstream.  The device drops
back to its rainbow after ~20 s without traffic, so ``keepalive()`` re-sends a commit every 5 s.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Tuple

from rgbdeck.models import (
    Device,
    DeviceError,
    Mode,
    Zone,
    MODE_COLORS_MODE_SPECIFIC,
    MODE_COLORS_NONE,
    MODE_COLORS_PER_LED,
    MODE_COLORS_RANDOM,
    MODE_FLAG_AUTOMATIC_SAVE,
    MODE_FLAG_HAS_BRIGHTNESS,
    MODE_FLAG_HAS_DIRECTION_LR,
    MODE_FLAG_HAS_MODE_SPECIFIC_COLOR,
    MODE_FLAG_HAS_PER_LED_COLOR,
    MODE_FLAG_HAS_RANDOM_COLOR,
    MODE_FLAG_HAS_SPEED,
    u32_to_rgb,
)
from rgbdeck.hw.hidraw import HidDevice, enumerate_hid

log = logging.getLogger("rgbdeck.corsair_node")

VID = 0x1B1C
PIDS = {
    0x0C1A: "Corsair Lighting Node Core",
    0x0C0B: "Corsair Lighting Node Pro",
    0x0C10: "Corsair Commander Pro",
    0x0C1E: "Corsair LS100 Lighting Kit",
    0x1D00: "Corsair 1000D Obsidian",
    0x1D04: "Corsair SPEC OMEGA RGB",
    0x0C23: "Corsair LT100",
}
USB_IDS: List[Tuple[int, int]] = [(VID, p) for p in sorted(PIDS)]

WRITE_PKT, READ_PKT, READ_TIMEOUT = 65, 17, 0.015
NUM_CHANNELS, LEDS_MAX = 2, 204
KEEPALIVE_S = 5.0
ENV_LEDS = "RGBDECK_CORSAIR_NODE_LEDS"

PKT_FIRMWARE, PKT_DIRECT, PKT_COMMIT, PKT_BEGIN, PKT_EFFECT_CONFIG = 0x02, 0x32, 0x33, 0x34, 0x35
PKT_RESET, PKT_PORT_STATE, PKT_BRIGHTNESS = 0x37, 0x38, 0x39
CH_RED, CH_GREEN, CH_BLUE = 0x00, 0x01, 0x02
PORT_HARDWARE, PORT_SOFTWARE = 0x01, 0x02
SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW = 0x00, 0x01, 0x02
MODE_RAINBOW_WAVE, MODE_COLOR_SHIFT, MODE_COLOR_PULSE, MODE_COLOR_WAVE, MODE_STATIC, MODE_TEMPERATURE = range(6)
MODE_VISOR, MODE_MARQUEE, MODE_BLINK, MODE_SEQUENTIAL, MODE_RAINBOW = range(6, 11)
MODE_DIRECT = 0xFFFF
ZONE_LINEAR = 1


def _mode(idx: int, name: str, value: int, flags: int, ncolors: int, color_mode: int, direction: int = 0) -> Mode:
    speedy = bool(flags & MODE_FLAG_HAS_SPEED)
    return Mode(
        idx,
        name,
        value,
        flags | MODE_FLAG_HAS_BRIGHTNESS | MODE_FLAG_AUTOMATIC_SAVE,
        SPEED_SLOW if speedy else 0,
        SPEED_FAST if speedy else 0,
        0,
        100,
        ncolors,
        ncolors,
        SPEED_MEDIUM if speedy else 0,
        100,
        direction,
        color_mode,
        [0] * ncolors,
    )


def build_modes() -> List[Mode]:
    """RGBController_CorsairLightingNode::SetupModes() (speed range is inverted upstream: 2 = slow .. 0 = fast)."""
    S, LR = MODE_FLAG_HAS_SPEED, MODE_FLAG_HAS_DIRECTION_LR
    C = MODE_FLAG_HAS_MODE_SPECIFIC_COLOR
    CR = C | MODE_FLAG_HAS_RANDOM_COLOR
    MS, RIGHT = MODE_COLORS_MODE_SPECIFIC, 1
    return [
        Mode(0, "Direct", MODE_DIRECT, MODE_FLAG_HAS_PER_LED_COLOR, color_mode=MODE_COLORS_PER_LED),
        _mode(1, "Rainbow Wave", MODE_RAINBOW_WAVE, S | LR, 0, MODE_COLORS_NONE, RIGHT),
        _mode(2, "Color Shift", MODE_COLOR_SHIFT, S | CR, 2, MS),
        _mode(3, "Color Pulse", MODE_COLOR_PULSE, S | CR, 2, MS),
        _mode(4, "Color Wave", MODE_COLOR_WAVE, S | LR | CR, 2, MS, RIGHT),
        _mode(5, "Static", MODE_STATIC, C, 1, MS),
        _mode(6, "Temperature", MODE_TEMPERATURE, C, 3, MS),
        _mode(7, "Visor", MODE_VISOR, S | CR, 2, MS),
        _mode(8, "Marquee", MODE_MARQUEE, S | LR | CR, 1, MS, RIGHT),
        _mode(9, "Blink", MODE_BLINK, S | CR, 2, MS),
        _mode(10, "Sequential", MODE_SEQUENTIAL, S | LR | CR, 1, MS, RIGHT),
        _mode(11, "Rainbow", MODE_RAINBOW, S, 0, MODE_COLORS_NONE),
    ]


def _default_led_counts() -> List[int]:
    counts = [0] * NUM_CHANNELS
    raw = os.environ.get(ENV_LEDS, "")
    for i, part in enumerate(raw.split(",")[:NUM_CHANNELS]):
        try:
            counts[i] = max(0, min(LEDS_MAX, int(part)))
        except ValueError:
            pass
    return counts


class CorsairLightingNode:
    def __init__(
        self, path: str, pid: int, name: str, hid: Optional[HidDevice] = None, led_counts: Optional[List[int]] = None
    ):
        self.hid = hid or HidDevice(path)
        self.name = name
        self.path = path
        self.last_commit = time.monotonic()
        self.version = ""
        try:
            self._read_firmware()
        except DeviceError:
            self.hid.close()
            raise
        self.counts = list(led_counts) if led_counts is not None else _default_led_counts()
        self.counts = (self.counts + [0] * NUM_CHANNELS)[:NUM_CHANNELS]
        self.device = Device(
            0,
            "LED Strip",
            name,
            "Corsair",
            "Corsair Lighting Node Device (direct hidraw)",
            self.version,
            "",
            "HID: " + path,
            0,
            build_modes(),
            [],
            [],
            [],
        )
        self._rebuild_zones(keep_colors=[])

    # ---- transport ---------------------------------------------------------------------------
    def _write_read(self, buf: bytearray) -> Optional[bytes]:
        """WriteAndRead(): 64-byte report, bounded 15 ms wait for the 16-byte reply."""
        self.hid.write(bytes(buf))
        return self.hid.read(READ_PKT, READ_TIMEOUT)

    @staticmethod
    def _pkt(*head: int) -> bytearray:
        buf = bytearray(WRITE_PKT)
        buf[1 : 1 + len(head)] = bytes(head)
        return buf

    def _read_firmware(self):
        r = self._write_read(self._pkt(PKT_FIRMWARE))
        if r and len(r) >= 4:
            self.version = f"{r[1]}.{r[2]}.{r[3]}"

    def _send_direct(self, channel: int, start: int, data: bytes, color_channel: int):
        buf = self._pkt(PKT_DIRECT, channel, start, len(data), color_channel)
        buf[6 : 6 + len(data)] = data
        self._write_read(buf)

    def _send_commit(self):
        self.last_commit = time.monotonic()
        self._write_read(self._pkt(PKT_COMMIT, 0xFF))

    def _send_begin(self, channel: int):
        self._write_read(self._pkt(PKT_BEGIN, channel))

    def _send_reset(self, channel: int):
        self._write_read(self._pkt(PKT_RESET, channel))

    def _send_port_state(self, channel: int, state: int):
        self._write_read(self._pkt(PKT_PORT_STATE, channel, state))

    def _send_brightness(self, channel: int, brightness: int):
        self._write_read(self._pkt(PKT_BRIGHTNESS, channel, min(100, max(0, brightness))))

    def _send_effect_config(
        self,
        channel: int,
        count: int,
        led_type: int,
        mode: int,
        speed: int,
        direction: int,
        change_style: int,
        colors: List[Tuple[int, int, int]],
    ):
        buf = self._pkt(PKT_EFFECT_CONFIG, channel, count, led_type, mode, speed, direction, change_style, 0)
        for i, (r, g, b) in enumerate(colors[:3]):
            buf[0x0A + 3 * i : 0x0D + 3 * i] = bytes([r, g, b])
        # bytes 0x13..0x18: three big-endian temperatures, always 0 upstream
        self._write_read(buf)

    # ---- zones ------------------------------------------------------------------------------
    def _rebuild_zones(self, keep_colors: List[int]):
        zones, names, start = [], [], 0
        for ch in range(NUM_CHANNELS):
            n = self.counts[ch]
            zname = f"Corsair RGB Header {ch + 1}"
            zones.append(Zone(ch, zname, ZONE_LINEAR, 0, LEDS_MAX, n, None, start))
            names += [f"{zname}, LED {i + 1}" for i in range(n)]
            start += n
        colors = (list(keep_colors) + [0] * start)[:start]
        self.device.zones, self.device.led_names, self.device.colors = zones, names, colors

    def resize_zone(self, zone_idx: int, count: int):
        """Set the number of LEDs on a channel (0..204); LED names/colors are rebuilt."""
        if not 0 <= zone_idx < NUM_CHANNELS:
            raise ValueError(f"zone {zone_idx} does not exist")
        count = max(0, min(LEDS_MAX, int(count)))
        old = [self.device.colors[z.start_idx : z.start_idx + z.leds_count] for z in self.device.zones]
        self.counts[zone_idx] = count
        old[zone_idx] = (old[zone_idx] + [0] * count)[:count]
        self._rebuild_zones([c for chunk in old for c in chunk])

    # ---- colors -----------------------------------------------------------------------------
    def set_colors(self, colors: List[int]):
        colors = (list(colors) + [0] * len(self.device.colors))[: len(self.device.colors)]
        for z in self.device.zones:
            self._set_channel_leds(z.index, colors[z.start_idx : z.start_idx + z.leds_count])
        self.device.colors = colors

    def _set_channel_leds(self, channel: int, colors: List[int]):
        """SetChannelLEDs(): software port state, 50-LED colour planes (R, G, B), commit."""
        self._send_port_state(channel, PORT_SOFTWARE)
        for start in range(0, len(colors), 50):
            chunk = [u32_to_rgb(c) for c in colors[start : start + 50]]
            for color_channel in (CH_RED, CH_GREEN, CH_BLUE):
                self._send_direct(channel, start, bytes(p[color_channel] for p in chunk), color_channel)
        self._send_commit()

    # ---- modes ------------------------------------------------------------------------------
    def set_mode(self, mode: Mode, save: bool = False):
        self.device.active_mode = mode.index
        if mode.value == MODE_DIRECT:
            self.set_colors(self.device.colors)
        else:
            for z in self.device.zones:
                self._set_channel_effect(z.index, z.leds_count, mode)
        brightness = mode.brightness if mode.flags & MODE_FLAG_HAS_BRIGHTNESS else 100
        for ch in range(NUM_CHANNELS):
            self._send_brightness(ch, brightness)

    def _set_channel_effect(self, channel: int, num_leds: int, mode: Mode):
        """DeviceUpdateZoneMode() + SetChannelEffect(): reset, begin, hardware port state, config, commit."""
        random = mode.color_mode == MODE_COLORS_RANDOM
        direction = 1 if mode.direction == 1 else 0
        colors = [u32_to_rgb(c) for c in mode.colors[:3]] if mode.color_mode == MODE_COLORS_MODE_SPECIFIC else []
        self._send_reset(channel)
        self._send_begin(channel)
        self._send_port_state(channel, PORT_HARDWARE)
        # upstream passes (channel, 0, num_leds, ...) so the LED count lands in the "led type" byte
        self._send_effect_config(
            channel, 0, num_leds & 0xFF, mode.value & 0xFF, mode.speed & 0xFF, direction, 1 if random else 0, colors
        )
        self._send_commit()

    def keepalive(self):
        if time.monotonic() - self.last_commit > KEEPALIVE_S:
            self._send_commit()

    def close(self):
        self.hid.close()


def probe() -> List[CorsairLightingNode]:
    found = []
    for info in enumerate_hid(VID):
        if info.pid not in PIDS:
            continue
        try:
            found.append(CorsairLightingNode(info.path, info.pid, PIDS[info.pid]))
        except DeviceError as e:
            log.info("%s at %s skipped: %s", PIDS[info.pid], info.path, e)
        except Exception as e:  # noqa: BLE001
            log.info("%s at %s skipped: %s: %s", PIDS[info.pid], info.path, type(e).__name__, e)
    return found


__all__ = ["USB_IDS", "probe", "CorsairLightingNode", "build_modes"]
