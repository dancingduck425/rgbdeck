"""Hardware-free checks for the hidapi (Windows) backend: per-interface grouping, report-id routing, merged reads,
and the Windows scan-code mapping. A fake `hid` module imitates Windows: one entry per collection, writes with a
foreign report id rejected."""

import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RGBDECK_HID_BACKEND"] = "hidapi"

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def desc(page, usage, rid=None, size=19):
    d = bytes([0x06, page & 0xFF, page >> 8, 0x0A, usage & 0xFF, usage >> 8, 0xA1, 0x01])
    if rid is not None:
        d += bytes([0x85, rid])
    d += bytes([0x09, 0x01, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 8, 0x95, size, 0x81, 0x00, 0x91, 0x00, 0xC0])
    return d


# Real Windows shape: collection N is "colNN" in the hardware id AND "&000(N-1)" at the end of the instance id
KB = r"\\?\hid#vid_046d&pid_c33c&mi_01&col0%d#7&2b4c1234&0&000%d#{4d1e55b2-f16f-11cf-88cb-001111000030}"
MOUSE = r"\\?\hid#vid_258a&pid_2011&mi_02#7&abcd&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
COLS = {
    KB % (1, 0): dict(page=1, usage=6, rids={1}, desc=desc(1, 6, 1), locked=True),
    KB % (2, 1): dict(page=0xC, usage=1, rids={2}, desc=desc(0xC, 1, 2), locked=True),
    KB % (3, 2): dict(page=0xFF43, usage=0x0602, rids={0x11}, desc=desc(0xFF43, 0x0602, 0x11)),
    KB % (4, 3): dict(page=0xFF43, usage=0x0604, rids={0x12}, desc=desc(0xFF43, 0x0604, 0x12, 63)),
    MOUSE: dict(page=0xFFFF, usage=0, rids=set(), desc=desc(0xFFFF, 0, None, 64)),
}
for c in COLS.values():
    c.update(written=[], feats=[], replies=[])
ENTRIES = []
for path, c in COLS.items():
    vid, pid = (0x046D, 0xC33C) if "046d" in path else (0x258A, 0x2011)
    ENTRIES.append(
        dict(
            path=path.encode(),
            vendor_id=vid,
            product_id=pid,
            product_string="G513" if vid == 0x046D else "Model O",
            interface_number=1 if vid == 0x046D else 2,
            usage_page=c["page"],
            usage=c["usage"],
        )
    )


class FakeDev:
    def __init__(self):
        self.col = None

    def open_path(self, path):
        self.col = COLS[path.decode()]
        if self.col.get("locked"):
            raise OSError("Access is denied")

    def get_report_descriptor(self):
        return list(self.col["desc"])

    def write(self, data):
        if self.col["rids"] and data[0] not in self.col["rids"]:
            raise OSError("The parameter is incorrect")
        self.col["written"].append(bytes(data))
        self.col["replies"].append(bytes([data[0], 0xAA]) + bytes(data[2:]))  # echo as the reply
        return len(data)

    def read(self, n, timeout_ms=0):
        for _ in range(max(1, timeout_ms // 10)):
            if self.col["replies"]:
                return list(self.col["replies"].pop(0))
            time.sleep(0.01)
        return []

    def send_feature_report(self, data):
        if self.col["rids"] and data[0] not in self.col["rids"]:
            raise OSError("The parameter is incorrect")
        self.col["feats"].append(bytes(data))
        return len(data)

    def get_feature_report(self, rid, n):
        return [rid] + [0x77] * (n - 1)

    def close(self):
        pass


fake = types.ModuleType("hid")
fake.enumerate = lambda vid=0, pid=0: [
    e for e in ENTRIES if (not vid or e["vendor_id"] == vid) and (not pid or e["product_id"] == pid)
]
fake.device = FakeDev
fake.__version__ = "0.14.0-fake"
sys.modules["hid"] = fake

from rgbdeck.hw import hidraw  # noqa: E402
from rgbdeck.hw.hidinfo import parse_collections  # noqa: E402
from rgbdeck.effects import win_scancode_to_evdev, EVDEV_CODES  # noqa: E402

check(hidraw.enumerate_hid.__module__ == "rgbdeck.hw.hidapi_backend", "hidraw module dispatches to the hidapi backend")

# --- descriptor parser
cols = parse_collections(desc(0xFF43, 0x0602, 0x11) + desc(1, 6, 1))
check(cols[0][:2] == (0xFF43, 0x0602) and cols[0][2] == {0x11} and cols[1][:2] == (1, 6), f"parse_collections {cols}")

# --- grouping
infos = hidraw.enumerate_hid(0x046D)
check(len(infos) == 1 and infos[0].interface == 1 and infos[0].pid == 0xC33C, f"4 collections -> 1 interface ({infos})")
kb = infos[0]
check(
    b"\x06\x43\xff" in kb.descriptor and b"\x85\x11" in kb.descriptor and b"\x85\x12" in kb.descriptor,
    "descriptor merged",
)
check("&col" not in kb.path.lower(), "interface path has the collection index stripped")
allinfos = hidraw.enumerate_hid()
check([i.pid for i in allinfos] == [0xC33C, 0x2011], "unfiltered enumeration lists every interface once")
check(all(i.descriptor == b"" for i in hidraw.enumerate_hid(with_descriptors=False)), "with_descriptors=False is cheap")

# --- routing
dev = hidraw.HidDevice(kb.path)
check(len(dev._handles) == 2, f"only the two vendor collections were opened ({len(dev._handles)})")
dev.write(bytes([0x11, 0xFF, 1, 2]) + bytes(16))
dev.write(bytes([0x12, 0xFF, 3, 4]) + bytes(60))
check(COLS[KB % (3, 2)]["written"] and COLS[KB % (3, 2)]["written"][0][0] == 0x11, "0x11 went to usage 0x0602")
check(COLS[KB % (4, 3)]["written"] and COLS[KB % (4, 3)]["written"][0][0] == 0x12, "0x12 went to usage 0x0604")
got = {dev.read(64, 0.5)[0] for _ in range(2)}
check(got == {0x11, 0x12}, f"replies from both collections merged into one read queue {got}")
check(dev.read(64, 0.05) is None, "read times out when idle")
dev.write(bytes([0x11]) + bytes(19))
time.sleep(0.1)
dev.drain()
check(dev.read(64, 0.05) is None, "drain empties pending replies")
try:
    dev.write(bytes([0x42]) + bytes(19))
    check(False, "unknown report id raises")
except Exception as e:
    check("failed" in str(e), f"unknown report id raises DeviceError ({e})")
dev.close()
check(all(not t.is_alive() for t in dev._threads), "reader threads stopped on close")

# --- feature reports on a collection without report ids (Glorious)
m = hidraw.enumerate_hid(0x258A)[0]
md = hidraw.HidDevice(m.path)
md.send_feature(bytes([0x00, 0x05, 0x01]) + bytes(62))
fb = md.get_feature(0, 65)
check(COLS[MOUSE]["feats"] and len(fb) == 65 and fb[1] == 0x77, "feature report routed / padded")
md.close()
check(threading.active_count() < 10, "no thread leak")

# --- Windows scan codes -> Linux key codes
check(EVDEV_CODES[win_scancode_to_evdev(0x1E, False, 0x41)] == "KEY_A", "A")
check(EVDEV_CODES[win_scancode_to_evdev(0x1D, True, 0xA3)] == "KEY_RIGHTCTRL", "Right Ctrl (extended)")
check(EVDEV_CODES[win_scancode_to_evdev(0x1D, False, 0xA2)] == "KEY_LEFTCTRL", "Left Ctrl")
check(EVDEV_CODES[win_scancode_to_evdev(0x48, True, 0x26)] == "KEY_UP", "Up arrow")
check(EVDEV_CODES[win_scancode_to_evdev(0x48, False, 0x68)] == "KEY_KP8", "KP 8 (same scan code, not extended)")
check(EVDEV_CODES[win_scancode_to_evdev(0x45, False, 0x13)] == "KEY_PAUSE", "Pause")
check(EVDEV_CODES[win_scancode_to_evdev(0x45, True, 0x90)] == "KEY_NUMLOCK", "Num Lock")
check(EVDEV_CODES[win_scancode_to_evdev(0x1C, True, 0x0D)] == "KEY_KPENTER", "KP Enter")
check(EVDEV_CODES[win_scancode_to_evdev(0x5B, True, 0x5B)] == "KEY_LEFTMETA", "Left Win")
check(win_scancode_to_evdev(0x00, False, 0xFF) == 0, "unknown -> 0")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
