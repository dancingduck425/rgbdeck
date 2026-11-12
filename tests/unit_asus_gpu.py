"""ASUS graphics card lights (hw/asus_gpu.py) against a stand-in for the card's lighting controller:
what is asked before anything is written, what is never touched, and what a color or an effect puts on the line."""

import ctypes
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.hw import asus_gpu as gpu  # noqa: E402
from rgbdeck.keyboard_layout import build_geometry  # noqa: E402
from rgbdeck.models import DeviceError  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeCard(gpu.Bus):
    """The controller as the driver sees it: a register pointer set by a word write to command 0, a read at
    0x81, byte writes at 0x01, block writes at 0x03, and the fixed pattern at 0xA0..0xAF."""

    def __init__(self, ids=(0x10DE, 0x2786, 0x1043, 0x88F3), version="AUMA0-E6K5-0107", leds=22, pattern=True):
        self.key, self.label, self.name, self.ids = f"fake:{id(self)}", "stand-in", "GeForce RTX 4070", ids
        self.mem = {}
        self.pattern = pattern
        for i, ch in enumerate(version.encode().ljust(16, b"\0")):
            self.mem[0x1000 + i] = ch
        self.mem[0x1C00 + 0x03] = leds
        self.pointer = None
        self.reads = self.writes = 0
        self.light_writes = []  # (register, bytes) for everything that is not a pointer move
        self.addresses = set()
        self.dead = False
        self.closed = False

    def _on(self, addr):
        self.addresses.add(addr)
        if self.dead:
            raise DeviceError("no answer")

    def read_byte_data(self, addr, cmd):
        self._on(addr)
        self.reads += 1
        if 0xA0 <= cmd <= 0xAF:
            return cmd - 0xA0 if self.pattern else 0xFF
        assert cmd == 0x81 and self.pointer is not None, "read without a register selected"
        return self.mem.get(self.pointer, 0)

    def write_word_data(self, addr, cmd, value):
        self._on(addr)
        self.writes += 1
        assert cmd == 0x00
        self.pointer = ((value & 0xFF) << 8) | (value >> 8)  # sent high byte first

    def write_byte_data(self, addr, cmd, value):
        self._on(addr)
        self.writes += 1
        assert cmd == 0x01
        self.mem[self.pointer] = value
        self.light_writes.append((self.pointer, bytes([value])))

    def write_block_data(self, addr, cmd, data):
        self._on(addr)
        self.writes += 1
        assert cmd == 0x03 and 1 <= len(data) <= 3
        for i, b in enumerate(data):
            self.mem[self.pointer + i] = b
        self.light_writes.append((self.pointer, bytes(data)))

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------- who is spoken to at all
other = FakeCard(ids=(0x10DE, 0x2488, 0x1462, 0x3909))  # an MSI card
check(gpu.probe([other]) == [] and other.reads == 0 and other.writes == 0, "another maker's card is not touched at all")
amd = FakeCard(ids=(0x1002, 0x73BF, 0x1043, 0x04F2))
check(gpu.probe([amd]) == [] and amd.reads + amd.writes == 0, "nor an ASUS card without an NVIDIA chip")

silent = FakeCard(pattern=False)
check(gpu.probe([silent]) == [], "an ASUS card without the controller's pattern: no device")
check(silent.writes == 0 and silent.reads == 1, "and nothing was written to it, the first wrong read ends it")
check(silent.closed, "its line is closed again")

strange = FakeCard(version="AUMA0-E6K5-9999")
check(gpu.probe([strange]) == [] and strange.light_writes == [], "an unknown controller version is left alone")

none = FakeCard(leds=0)
check(gpu.probe([none]) == [] and none.light_writes == [], "a controller reporting no lights is left alone")

# ---------------------------------------------------------------------------- the card itself
card = FakeCard()
found = gpu.probe([card])
check(len(found) == 1 and card.light_writes == [], "a Strix 4070 is recognised; looking at it changes nothing")
check(card.addresses == {0x67}, "only address 0x67 is ever used")
drv = found[0]
dev = drv.device
check(dev.type_name == "GPU" and dev.name == "ASUS GeForce RTX 4070" and len(dev.colors) == 22, "device: " + dev.name)
check(dev.version == "AUMA0-E6K5-0107" and dev.modes[0].name == "Direct", "version and modes")
check(build_geometry(dev).kind == "gpu", "drawn as a graphics card")

reads = card.reads
again = gpu.probe([card])
check(len(again) == 1 and card.reads == reads, "a rescan does not question the card again")

colors = [0xFF0000] * 22  # the app's color numbers are 0xBBGGRR: this is blue
colors[3] = 0x112233
drv.set_colors(colors)
check(drv.wait(), "colors are written by the card's own thread")
check(card.light_writes[:2] == [(0x8020, b"\x01"), (0x80A0, b"\x01")], "direct control is switched on first")
table = bytes(card.mem.get(0x8100 + i, 0) for i in range(66))
check(table[:3] == b"\x00\xff\x00" and table[9:12] == b"\x33\x11\x22", "colors land as red, blue, green per light")
check(len(card.light_writes) == 2 + 22, "one block per light")

before = card.writes
drv.set_colors(colors)
drv.wait()
check(card.writes == before, "the same colors again cost nothing")
colors[7] = 0x00FF00
drv.set_colors(colors)
drv.wait()
check(
    card.writes == before + 2 and card.light_writes[-1] == (0x8100 + 21, b"\x00\x00\xff"),
    "one changed light: one write",
)

static = dev.mode_named("Static")
static.colors = [0x0A141E]
card.light_writes.clear()
drv.set_mode(static, save=True)
check(
    card.light_writes[0] == (0x8020, b"\x00") and card.light_writes[1] == (0x8021, b"\x01"),
    "effect: direct off, mode 1",
)
check(bytes(card.mem[0x8160 + i] for i in range(6)) == b"\x1e\x0a\x14\x1e\x0a\x14", "its color fills the effect table")
check(card.light_writes[-2:] == [(0x80A0, b"\x01"), (0x80A0, b"\xaa")], "applied, then stored in the card")
card.light_writes.clear()
drv.set_mode(dev.mode_named("Rainbow"))
check(card.light_writes[-1] == (0x80A0, b"\x01") and card.mem[0x8021] == 5, "without save nothing is stored")

card.light_writes.clear()
drv.set_mode(dev.modes[0])
check(card.light_writes[:2] == [(0x8020, b"\x01"), (0x80A0, b"\x01")], "back to direct: switched on again")
check(len(card.light_writes) == 2 + 22, "and every light is written again, whatever was there before")
registers = {reg for reg, _ in card.light_writes} | {0x8021, 0x8160}
check(all(0x8020 <= r <= 0x80A0 or 0x8100 <= r < 0x81C0 for r in registers), "only lighting registers are written")

card.dead = True
drv.set_colors([0x0000FF] * 22)
check(drv.wait() and drv._failing, "a card that stops answering does not take the program down")
try:
    drv.set_mode(static)
    check(False, "changing its effect then says so")
except DeviceError:
    check(True, "changing its effect then says so")
card.dead = False
card.light_writes.clear()
drv.set_colors([0x00FF00] * 22)
drv.wait()
check(card.light_writes[0] == (0x8020, b"\x01"), "once it answers again, direct control is asked for afresh")

# many frames in a row: the newest one is what ends up on the card, and closing waits for it
for i in range(200):
    drv.set_colors([i] * 22)
drv.close()
check(
    bytes(card.mem[0x8100 + i] for i in range(3)) == b"\xc7\x00\x00" and card.closed,
    "close() writes the last colors first",
)

# ---------------------------------------------------------------------------- the Windows driver call
check(
    ctypes.sizeof(gpu._NvI2CInfo) == (64 if ctypes.sizeof(ctypes.c_void_p) == 8 else 44),
    "request layout as the NVIDIA driver expects it",
)


class FakeApi:
    def __init__(self):
        self.calls = []

    def pci_ids(self, handle, dev, sub, rev, ext):
        dev.value, sub.value = 0x278610DE, 0x88F31043
        return 0

    def full_name(self, handle, text):
        text.value = b"NVIDIA GeForce RTX 4070"
        return 0

    def _note(self, kind, info):
        self.calls.append(
            (
                kind,
                info.dev_address,
                info.reg_address[0],
                info.reg_addr_size,
                bytes(info.data[i] for i in range(info.size)),
                info.port_id,
                info.is_port_id_set,
                info.version,
                info.speed,
            )
        )

    def i2c_write(self, handle, info, unknown):
        self._note("w", info)
        return 0

    def i2c_read(self, handle, info, unknown):
        self._note("r", info)
        info.data[0] = 0x5A
        return self.fail if hasattr(self, "fail") else 0


api = FakeApi()
bus = gpu.NvapiBus(api, 1234, 0)
check(
    bus.ids == (0x10DE, 0x2786, 0x1043, 0x88F3) and bus.name == "NVIDIA GeForce RTX 4070",
    "card identified from the driver",
)
gpu.reg_write_block(bus, 0x8103, b"\x01\x02\x03")
version = ctypes.sizeof(gpu._NvI2CInfo) | (3 << 16)
check(
    api.calls[0] == ("w", 0xCE, 0x00, 1, b"\x81\x03", 1, 1, version, 0xFFFF),
    "register pointer: high byte first, port 1",
)
check(api.calls[1][2:5] == (0x03, 1, b"\x03\x01\x02\x03"), "block write carries its length")
check(gpu.reg_read(bus, 0x1000) == 0x5A and api.calls[-1][:4] == ("r", 0xCE, 0x81, 1), "read through command 0x81")
api.fail = -1
try:
    bus.read_byte_data(0x67, 0xA0)
    check(False, "driver error becomes a device error")
except DeviceError:
    check(True, "driver error becomes a device error")

# ---------------------------------------------------------------------------- Linux: which lines are opened
root = Path(tempfile.mkdtemp(prefix="rgbdeck-i2c-"))


def adapter(n, label, ids):
    pci = root / f"pci{n}"
    pci.mkdir()
    for f, v in zip(("vendor", "device", "subsystem_vendor", "subsystem_device"), ids):
        (pci / f).write_text(f"0x{v:04x}\n")
    d = root / "devices" / f"i2c-{n}"
    d.mkdir(parents=True)
    (d / "name").write_text(label + "\n")
    (d / "device").symlink_to(pci)


adapter(3, "NVIDIA i2c adapter 1 at 1:00.0", (0x10DE, 0x2488, 0x1462, 0x3909))
adapter(4, "NVIDIA i2c adapter 3 at 1:00.0", (0x10DE, 0x2786, 0x1043, 0x88F3))
adapter(5, "SMBus PIIX4 adapter port 0 at 0b00", (0x1022, 0x790B, 0x1043, 0x8877))
opened = []
real = gpu.LinuxBus


class Spy(real):
    def __init__(self, number, ids, label):
        opened.append(number)
        self.key, self.label, self.ids = f"i2c-{number}", label, ids

    def close(self):
        pass


gpu.LinuxBus = Spy
check(
    gpu._linux_buses(str(root / "devices")) == [] and opened == [],
    "Linux: other makers, other ports, other buses are not opened",
)
adapter(6, "NVIDIA i2c adapter 1 at 2:00.0", (0x10DE, 0x2786, 0x1043, 0x88F3))
found = gpu._linux_buses(str(root / "devices"))
check(opened == [6] and found[0].ids[3] == 0x88F3, "Linux: the ASUS card's port 1 is the one that is opened")
gpu.LinuxBus = real

os.environ["RGBDECK_NO_GPU"] = "1"
check(gpu.buses() == [], "RGBDECK_NO_GPU switches the search off")
del os.environ["RGBDECK_NO_GPU"]
check(isinstance(gpu.probe(), list), "the real search runs on this machine without complaint")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
