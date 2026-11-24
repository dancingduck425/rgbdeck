# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""A simulated ASUS ROG mouse on its SpeedNova 8K receiver (or its cable) for the tests: it answers the way the
real Harpe II Ace was captured answering (see rgbdeck/hw/asus_mouse.py for the protocol)."""

import time

PACKET = 64


class FakeMouse:
    """The receiver (or the cable) with the mouse behind it. Reports are 64 bytes with the report id, as the
    system hands them over; with report id 0 the id byte is left out of what is read, as hidraw does."""

    def __init__(self, rid=3, effect=0x04, pct=50, rgb=(255, 0, 0), battery=80, charging=False):
        self.rid = rid
        self.on = True  # False: asleep, switched off or out of range
        self.silent = False  # asleep and the receiver says nothing at all
        self.battery, self.charging = battery, charging
        self.light = (effect, pct, *rgb)
        self.stored = self.light
        self.lighting = True  # False: a mouse that refuses the lighting commands
        self.refuse_saves = 0  # how many saves to refuse first (the Harpe II Ace does one after sitting unused)
        self.refuse_then_echo = False  # refuse the next change, then confirm it all the same
        self.noise = []  # reports that arrive ahead of the next answer
        self.sent, self.queue = [], []
        self.closed = False

    # -- HidDevice surface
    def drain(self):
        self.queue.clear()

    def close(self):
        self.closed = True

    def read(self, size=64, timeout=0.5):
        if self.queue:
            return self.queue.pop(0)
        time.sleep(min(timeout, 0.002))
        return None

    def write(self, data):
        data = bytes(data)
        assert len(data) == PACKET, f"{len(data)} bytes instead of {PACKET}"
        assert data[0] == self.rid, f"report id {data[0]:02x} instead of {self.rid:02x}"
        self.sent.append(data)
        self.queue += self.noise
        self.noise = []
        if self.silent:
            return len(data)
        if not self.on:
            self._reply(0xFF, 0xAA)
            return len(data)
        cmd = data[1:3]
        if cmd == bytes([0x12, 0x07]):
            self._reply(0x12, 0x07, 0, 0, self.battery, 0x02, 0x14, 0xD8, 0x0F, 1 if self.charging else 0, 0, 1)
        elif cmd == bytes([0x12, 0x03]):
            if self.lighting:
                self._reply(0x12, 0x03, 0, 0, *self.light)  # byte 3 is 0 whatever zone is asked for
            else:
                self._reply(0xFF, 0xAA)
        elif cmd == bytes([0x51, 0x28]):
            if not self.lighting:
                self._reply(0xFF, 0xAA)
                return len(data)
            if self.refuse_then_echo:
                self.refuse_then_echo = False
                self._reply(0xFF, 0xAA)
            self.light = tuple(data[5:10])
            self._reply(*data[1:10])
        elif cmd == bytes([0x50, 0x03]):
            if self.refuse_saves:
                self.refuse_saves -= 1
                self._reply(0xFF, 0xAA)
            else:
                self.stored = self.light
                self._reply(0x50, 0x03)
        else:
            self._reply(0xFF, 0xAA)
        return len(data)

    def _reply(self, *body):
        report = bytes([self.rid, *body]) + bytes(PACKET - 1 - len(body))
        self.queue.append(report[1:] if self.rid == 0 else report)

    # -- for the checks
    def take(self):
        """What was sent since the last look, as (command bytes ...) without the padding."""
        out = [p.rstrip(b"\x00") or b"\x00" for p in self.sent]
        self.sent = []
        return out
