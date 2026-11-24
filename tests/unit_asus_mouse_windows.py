# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""The ASUS mouse driver through the Windows HID layer (hidapi_backend.py), with a fake `hid` module shaped like
Windows shows the SpeedNova 8K receiver: one entry per collection, the mouse and keyboard collections held by
the system, a write with a foreign report id rejected. The driver has to end up on collection 3 of interface 2
(what ASUS's own protocol users call "mi_02&col03") without being told so."""

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["RGBDECK_HID_BACKEND"] = "hidapi"

from asus_mouse_sim import FakeMouse  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def desc(page, usage, rid=None, size=63):
    d = bytes([0x06, page & 0xFF, page >> 8, 0x0A, usage & 0xFF, usage >> 8, 0xA1, 0x01])
    if rid is not None:
        d += bytes([0x85, rid])
    d += bytes([0x09, 0x01, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 8, 0x95, size, 0x81, 0x00, 0x91, 0x00, 0xC0])
    return d


GUID = "{4d1e55b2-f16f-11cf-88cb-001111000030}"
RX = r"\\?\hid#vid_0b05&pid_1ad0&mi_%02d%s#8&1f2e3d4c&0&%04d#" + GUID
CABLE = r"\\?\hid#vid_0b05&pid_1c69&mi_00#8&5a6b7c8d&0&0000#" + GUID
AURA = r"\\?\hid#vid_0b05&pid_19af&mi_02#8&99887766&0&0000#" + GUID


def build(mouse, cable=None, descriptors=True):
    """The fake `hid` module's world: path -> collection."""
    cols = {
        RX % (0, "", 0): dict(page=1, usage=2, rids=set(), locked=True, iface=0),  # the pointer
        RX % (1, "&col01", 0): dict(page=1, usage=6, rids={1}, locked=True, iface=1),  # keys for macros
        RX % (1, "&col02", 1): dict(page=0xC, usage=1, rids={2}, iface=1),
        RX % (1, "&col03", 2): dict(page=1, usage=0x80, rids={3}, iface=1),
        RX % (2, "&col01", 0): dict(page=0xFF00, usage=1, rids={1}, iface=2),
        RX % (2, "&col02", 1): dict(page=0xFF01, usage=1, rids={2}, iface=2),
        RX % (2, "&col03", 2): dict(page=0xFF02, usage=1, rids={3}, iface=2, mouse=mouse),
        RX % (3, "&col01", 0): dict(page=0xFF03, usage=1, rids={4}, iface=3),
        RX % (3, "&col02", 1): dict(page=0xFF04, usage=1, rids={5}, iface=3),
        AURA: dict(page=0xFF72, usage=0xA1, rids={0xEC}, iface=2, pid=0x19AF, name="AURA LED Controller"),
    }
    if cable is not None:
        cols[CABLE] = dict(page=0xFF01, usage=1, rids=set(), iface=0, pid=0x1C69, name="ROG HARPE II ACE", mouse=cable)
    for c in cols.values():
        rid = min(c["rids"]) if c["rids"] else None
        c.update(written=[], chatter=[], opened=0, desc=desc(c["page"], c["usage"], rid) if descriptors else None)
    return cols


class FakeDev:
    cols = {}

    def __init__(self):
        self.col = None

    def open_path(self, path):
        self.col = FakeDev.cols[path.decode()]
        self.col["opened"] += 1

    def get_report_descriptor(self):
        if self.col["desc"] is None:
            raise OSError("not supported by this hidapi")
        return list(self.col["desc"])

    def write(self, data):
        data = bytes(data)
        if self.col.get("locked"):
            raise OSError("Access is denied")
        if self.col["rids"] and data[0] not in self.col["rids"]:
            raise OSError("The parameter is incorrect")
        self.col["written"].append(data)
        if self.col.get("mouse") is not None:
            return self.col["mouse"].write(data)
        return len(data)

    def read(self, n, timeout_ms=0):
        if self.col.get("locked"):
            raise OSError("Access is denied")
        end = time.monotonic() + timeout_ms / 1000
        while True:
            if self.col["chatter"]:
                return list(self.col["chatter"].pop(0))
            mouse = self.col.get("mouse")
            if mouse is not None and mouse.queue:
                return list(mouse.queue.pop(0))
            if time.monotonic() >= end:
                return []
            time.sleep(0.002)

    def send_feature_report(self, data):
        return len(data)

    def get_feature_report(self, rid, n):
        return [rid] + [0] * (n - 1)

    def close(self):
        self.col["opened"] -= 1


def entries(vid=0, pid=0):
    out = []
    for path, c in FakeDev.cols.items():
        p = c.get("pid", 0x1AD0)
        if (vid and vid != 0x0B05) or (pid and pid != p):
            continue
        out.append(
            dict(
                path=path.encode(),
                vendor_id=0x0B05,
                product_id=p,
                manufacturer_string="ASUSTeK",
                product_string=c.get("name", "ROG SPEEDNOVA 8K RECEIVER"),
                interface_number=c["iface"],
                usage_page=c["page"],
                usage=c["usage"],
            )
        )
    return out


fake_hid = types.ModuleType("hid")
fake_hid.enumerate = entries
fake_hid.device = FakeDev
fake_hid.__version__ = "0.14.0-fake"
sys.modules["hid"] = fake_hid

from rgbdeck.hw import asus_mouse as am  # noqa: E402
from rgbdeck.hw import hidraw  # noqa: E402
from rgbdeck.models import rgb_to_u32  # noqa: E402

check(am.HidDevice.__module__ == "rgbdeck.hw.hidapi_backend", "the driver talks through the Windows HID layer here")


def wrote(cols, iface, col):
    return cols[RX % (iface, "&col%02d" % col if col else "", max(0, col - 1))]["written"]


# ------------------------------------------------------------------------------------- the receiver
mouse = FakeMouse()
cols = FakeDev.cols = build(mouse)
found = am.probe()
check(len(found) == 1 and found[0].device.name == "ROG Harpe II Ace" and found[0].awake, "the mouse is found")
drv = found[0]
check(
    [bytes(p[:3]).hex(" ") for p in wrote(cols, 2, 3)] == ["03 12 07", "03 12 03"],
    "its questions went to collection 3 of interface 2",
)
check(
    not any(c["written"] for path, c in cols.items() if c.get("mouse") is None),
    "and to no other collection: not the pointer, not the keys, not the mainboard's controller",
)
check(
    all(c["opened"] == 0 for path, c in cols.items() if c["iface"] != 2 or c.get("pid")),
    "only the receiver's interface 2 is held open",
)
check(drv.battery() == (80, False) and drv.device.current_mode.name == "Reactive", "battery and lighting are read")
drv.set_colors([rgb_to_u32(0, 255, 0)])
check(mouse.light == (0, 100, 0, 255, 0), "a color arrives")
check(all(len(p) == 64 and p[0] == 3 for p in wrote(cols, 2, 3)), "every report is 64 bytes on report id 3")

# other collections of the interface talk too (their reports land in the same queue)
drv._sent_at -= 1
cols[RX % (2, "&col01", 0)]["chatter"] += [bytes([1, 0x51, 0x28]) + bytes(61), bytes([1, 0xFF, 0xAA]) + bytes(61)]
cols[RX % (2, "&col02", 1)]["chatter"] += [bytes([2, 0x12, 0x07, 0, 0, 9]) + bytes(58)]
time.sleep(0.15)  # let the reader threads pick those up
drv.hid.drain = lambda: None  # ... and keep them in front of the real answer
drv.set_colors([rgb_to_u32(255, 0, 0)])
check(mouse.light == (0, 100, 255, 0, 0) and drv.awake, "reports of the neighbouring collections are passed over")
del drv.hid.drain

m = drv.device.mode_named("Breathing")
m.colors, m.brightness = [rgb_to_u32(0, 0, 255)], 60
drv._sent_at -= 1
drv.set_mode(m, save=True)
check(mouse.stored == (1, 60, 0, 0, 255), "an effect is set and stored")
mouse.on = False
drv._sent_at -= 1
drv.set_colors([rgb_to_u32(1, 2, 3)])
check(not drv.awake, "the mouse falling asleep is noticed here too")
mouse.on = True
drv.keepalive()
check(drv.awake and mouse.light == (0, 100, 1, 2, 3), "and it gets its color when it is back")
drv.close()
check(all(c["opened"] == 0 for c in cols.values()), "closing lets go of every collection")

# ------------------------------------------------------------------------------------- an older hidapi
mouse = FakeMouse()
cols = FakeDev.cols = build(mouse, descriptors=False)
found = am.probe()
check(len(found) == 1 and found[0].awake, "without readable descriptors the mouse is still found")
found[0].set_colors([rgb_to_u32(9, 8, 7)])
check(
    mouse.light == (0, 100, 9, 8, 7) and not wrote(cols, 2, 1) and not wrote(cols, 2, 2),
    "the collections that reject report 3 are skipped, the right one is remembered",
)
found[0].close()

# ------------------------------------------------------------------------------------- receiver and cable
asleep, cable = FakeMouse(), FakeMouse(rid=0, effect=0, pct=100, rgb=(0, 0, 255), battery=47, charging=True)
asleep.on = False
cols = FakeDev.cols = build(asleep, cable=cable)
infos = [i for i in hidraw.enumerate_hid(0x0B05) if i.pid == 0x1C69]
check(len(infos) == 1 and infos[0].interface == 0, "the cable is one interface with one collection")
found = am.probe()
check(
    len(found) == 1 and not found[0].wireless and found[0].battery() == (47, True),
    "mouse set to its cable: found there, charging at 47 %",
)
found[0].set_colors([rgb_to_u32(255, 255, 0)])
check(
    cable.light == (0, 100, 255, 255, 0) and cols[CABLE]["written"][-1][:3] == bytes([0x00, 0x51, 0x28]),
    "over the cable reports carry no id: a leading 00 and then the command",
)
check(cols[RX % (2, "&col03", 2)]["opened"] == 0, "the receiver, reporting the mouse asleep, is let go")
found[0].close()

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
