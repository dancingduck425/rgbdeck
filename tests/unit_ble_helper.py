"""The Bluetooth helper process (hw/govee_ble.py): the main program's side of the conversation against a
scripted helper, then a real helper process without any Bluetooth hardware: it has to report the end of a scan
one way or another, and the main program has to survive its death."""

import io
import json
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="rgbdeck-blehelper-")
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["APPDATA"] = TMP
os.environ.pop("RGBDECK_NO_BLUETOOTH", None)
os.environ["RGBDECK_BLE_HELPER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck.hw import govee_ble as ble  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ---------------------------------------------------------------------------- a scripted helper
class FakeProc:
    """Stands in for the helper process: records what it is asked, never answers by itself."""

    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = []
        self.code = None

    def asked(self):
        out = [json.loads(line) for line in self.stdin.getvalue().splitlines()]
        self.stdin.seek(0)
        self.stdin.truncate()
        return out

    def poll(self):
        return self.code

    def wait(self, timeout=None):  # noqa: ARG002
        return self.code if self.code is not None else 0

    def kill(self):
        self.code = -9


real_end_process = ble._end_process
ble._end_process = lambda proc: proc.kill()  # the scripted helper has no process to end
check(ble._helper_mode(), "helper mode can be forced on any system")
check(ble.available(), "with the helper, Bluetooth counts as available without Qt in this process")

proc = FakeProc()
radio = ble._RemoteRadio(proc)
ble._radio = radio
ble._want = lambda advert: advert.sku == "H618F"

heard = {"ev": "heard", "key": "AA:BB", "name": "Govee_H618F_2A24", "sku": "H618F", "suffix": "2A24", "rssi": -60}
radio.handle(heard)
radio.handle({"ev": "heard", "key": "CC:DD", "name": "Govee_H6008_0001", "sku": "H6008", "suffix": "0001", "rssi": -70})
check({a.key for a in ble.adverts()} == {"AA:BB", "CC:DD"}, "what the helper hears shows up as adverts")
check(proc.asked() == [{"op": "connect", "key": "AA:BB"}], "only the wanted light gets a connection")
link = ble.links()[0]
check(link.state == "connecting" and link.sku == "H618F", "its link exists and is connecting")
radio.handle(heard)
check(proc.asked() == [], "hearing it again does not ask twice")
check(link.send([ble.checksum(b"\x33\x01\x01")]) is False, "nothing is sent while connecting")

radio.handle({"ev": "link", "key": "AA:BB", "state": "ready"})
check(ble.links("ready") == [link], "ready when the helper says so")

result = {}
packets = [ble.checksum(b"\x33\x01\x01"), ble.checksum(b"\x33\x04\x32")]
t = threading.Thread(target=lambda: result.update(sent=link.send(packets)))
t.start()
deadline = time.monotonic() + 2
while not proc.stdin.getvalue() and time.monotonic() < deadline:
    time.sleep(0.01)
check(
    proc.asked() == [{"op": "send", "key": "AA:BB", "packets": [p.hex() for p in packets]}], "packets go over in order"
)
radio.handle({"ev": "done", "key": "AA:BB"})
t.join(0.2)
check(t.is_alive(), "the sender waits until every packet was answered")
radio.handle({"ev": "done", "key": "AA:BB"})
t.join(2)
check(result.get("sent") is True, "and returns once the helper reported both")

query = ble.checksum(b"\xaa\x01")
t = threading.Thread(target=lambda: result.update(reply=link.ask(query)))
t.start()
deadline = time.monotonic() + 2
while not proc.stdin.getvalue() and time.monotonic() < deadline:
    time.sleep(0.01)
answer = ble.checksum(b"\xaa\x01\x01")
radio.handle({"ev": "rx", "key": "AA:BB", "data": answer.hex()})
radio.handle({"ev": "done", "key": "AA:BB"})
t.join(2)
check(result.get("reply") == answer and link.power is True, "a query returns the light's answer; power is noted")
proc.asked()

t = threading.Thread(target=lambda: result.update(lost=link.send([ble.checksum(b"\x33\x01\x00")])))
t.start()
time.sleep(0.1)
radio.handle({"ev": "link", "key": "AA:BB", "state": "connecting"})
t.join(2)
check(not t.is_alive() and "lost" in result, "a lost connection releases whoever was waiting")
radio.handle({"ev": "link", "key": "AA:BB", "state": "gone"})
check(ble.links() == [] and link.state == "gone", "gone for good: the link is forgotten")
proc.asked()
radio.handle({"ev": "link", "key": "EE:FF", "state": "ready"})
check(proc.asked() == [{"op": "drop", "key": "EE:FF"}], "a connection nobody asked for is hung up")

radio.handle({"ev": "scan", "running": True, "error": ""})
check(ble.scanning() and ble.scan_age() == 0.0, "scan running")
radio.handle({"ev": "scan", "running": False, "error": "Bluetooth adapter powered off."})
check(not ble.scanning() and ble.scan_error() == "Bluetooth adapter powered off.", "scan end and its error arrive")

# a scan whose end is never reported
ble.scan()
check(proc.asked() == [{"op": "scan"}] and ble.scanning(), "scan requested")
with ble._cv:
    ble._scan["started"] -= ble.SCAN_MS / 1000 + ble.SCAN_GRACE + 1
check(not ble.scanning() and ble.scan_error() == "Bluetooth did not answer", "an unreported scan is over anyway")

# the helper dies under a connected light
radio.handle({"ev": "link", "key": "AA:BB", "state": "gone"})
radio.handle(heard)
radio.handle({"ev": "link", "key": "AA:BB", "state": "ready"})
link = ble.links("ready")[0]
proc.code = -11 if sys.platform != "win32" else 0xC0000409
radio._read()  # the helper's output ended
check(ble._radio is None and link.state == "gone" and ble.links() == [], "a dead helper takes its links with it")
check("stopped unexpectedly" in ble.scan_error(), "the reason is there for the Lights panel")
check(ble._helper["crashes"] == 1 and ble._helper["retry_at"] > time.monotonic(), "no new helper right away")
check(ble.start(lambda a: False) is False, "start() respects the pause")

# three in a row: Bluetooth is left alone
for _ in range(2):
    p2 = FakeProc()
    r2 = ble._RemoteRadio(p2)
    ble._radio = r2
    p2.code = 1
    r2._read()
check(bool(ble._helper["disabled"]) and not ble.available(), "after three losses Bluetooth is left alone")
check("keeps stopping" in ble.scan_error() or "stopped unexpectedly" in ble.scan_error(), "and says so")

# a helper that stops answering is ended (counted in the watcher's own steps)
ble._helper.update(crashes=0, retry_at=0.0, disabled="")
p3 = FakeProc()
r3 = ble._RemoteRadio(p3)
r3.rx_count = 5
old = ble.HELPER_SILENCE
ble.HELPER_SILENCE = 0.15
r3._watch(step=0.05)
ble.HELPER_SILENCE = old
check(p3.code == -9 and "stopped answering" in r3.why, "a silent helper is ended")

# a helper being shut down on purpose is not a crash
ble._helper.update(crashes=0, retry_at=0.0, disabled="")
p4 = FakeProc()
r4 = ble._RemoteRadio(p4)
ble._radio = r4
ble.stop()
check([m["op"] for m in p4.asked()] == ["stop"] if not p4.stdin.closed else True, "stop is sent")
r4._read()
check(ble._helper["crashes"] == 0 and ble.scan_error() == "", "an asked-for end is not counted as a loss")

# ---------------------------------------------------------------------------- a real helper, no hardware needed
ble._end_process = real_end_process
ble._helper.update(crashes=0, retry_at=0.0, disabled="")
check(ble.start(lambda advert: False), "a real helper process starts")
radio = ble._radio
helper = radio.proc
check(ble.scan(), "scan request accepted")
ended = ble.wait(time.monotonic() + ble.SCAN_MS / 1000 + ble.SCAN_GRACE + 8, lambda: not ble._scan["running"])
print("     scan ended:", ended, "| error:", repr(ble.scan_error()), "| heard:", [a.name for a in ble.adverts()])
check(ended, "the scan comes to an end (found something, found nothing, or said why it could not look)")
check(helper.poll() is None and radio.rx_count > 0, "the helper is alive and talking")
ble.stop()
check(helper.poll() is not None, "stop() ends the helper process")
check(ble._helper["crashes"] == 0, "which is not counted as a loss")

# the helper dies the way Qt's Windows backend does when a system call fails: the main program lives on
os.environ["RGBDECK_BLE_HELPER_FAULT"] = "scan"
check(ble.start(lambda advert: False), "helper started for the crash drill")
helper = ble._radio.proc
ble.scan()
gone = ble.wait(time.monotonic() + 20, lambda: ble._radio is None)
check(gone and helper.poll() not in (None, 0), "the helper died in the middle of a scan")
check(not ble.scanning() and "stopped unexpectedly" in ble.scan_error(), "this process is still here and knows why")
check(ble._helper["crashes"] == 1, "counted as one loss")
del os.environ["RGBDECK_BLE_HELPER_FAULT"]
ble._helper.update(retry_at=0.0)
check(ble.start(lambda advert: False), "and Bluetooth can be tried again later")
ble.stop()

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
