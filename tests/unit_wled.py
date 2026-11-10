"""WLED driver against a simulated controller on loopback: JSON info/state over HTTP, realtime colours over UDP."""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TMP = tempfile.mkdtemp(prefix="rgbdeck-wled-")
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["APPDATA"] = TMP
os.environ["RGBDECK_NO_BLUETOOTH"] = "1"
os.environ["RGBDECK_NO_DESKTOP_HOOKS"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rgbdeck.backend as backend  # noqa: E402
from rgbdeck.hw import wled  # noqa: E402
from rgbdeck.models import rgb_to_u32  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeWled:
    def __init__(self, leds=60, brand="WLED"):
        self.leds, self.brand = leds, brand
        self.state = {"on": True, "bri": 128}
        self.posts, self.frames = [], []
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/json/info":
                    self._send(
                        {
                            "ver": "0.15.0",
                            "brand": fake.brand,
                            "name": "Desk",
                            "mac": "aabbccddeeff",
                            "udpport": fake.udp.getsockname()[1],
                            "leds": {"count": fake.leds},
                        }
                    )
                else:
                    self._send(fake.state)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                fake.posts.append(body)
                if "on" in body:
                    fake.state["on"] = body["on"]
                self._send(fake.state)

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.alive = True
        threading.Thread(target=self._udp, daemon=True).start()

    def _udp(self):
        while self.alive:
            try:
                self.frames.append(self.udp.recvfrom(4096)[0])
            except socket.timeout:
                pass
            except OSError:
                return

    def stop(self):
        self.alive = False
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()


fake = FakeWled()
wled.HTTP_PORT = fake.http.server_address[1]
wled.discover = lambda wait=0.8: []  # no multicast in tests
backend.discover_drivers = lambda: [wled]
backend.hidraw.enumerate_hid = lambda *a, **k: []

q = wled._mdns_question("_wled._tcp.local")
check(
    q[4:6] == b"\x00\x01" and b"\x05_wled\x04_tcp\x05local\x00" in q and q[-4:] == b"\x00\x0c\x80\x01", "mDNS question"
)
check(
    wled.check("127.0.0.1") is not None and wled.check("127.0.0.1").count == 60, "controller recognised, LED count read"
)
check(wled.add("not an address") is None, "nonsense is not an address")
check(
    wled.add("127.0.0.1") == "Desk" and wled.get_settings()["hosts"] == ["127.0.0.1"], "added by address and remembered"
)

client = backend.LocalBackend()
client.refresh()
check([d.name for d in client.devices] == ["WLED Desk"], f"shows up as a device ({[d.name for d in client.devices]})")
dev = client.devices[0]
check(len(dev.colors) == 30 and dev.zones[0].leds_max == 60, "a long strip is offered as 30 LEDs, resizable to 60")
check(dev.current_mode.name == "WLED's own effect" and dev.experimental, "starts on WLED's own effect; experimental")
check(not client.poll_notifications(), "no rescan while nothing changes")

red, blue = rgb_to_u32(255, 0, 0), rgb_to_u32(0, 0, 255)
client.update_mode(dev, dev.mode_named("Direct"))
client.update_leds(dev, [red] * 15 + [blue] * 15)
client.wait_idle()
time.sleep(0.3)
f = fake.frames[-1]
check(
    f[0] == 2 and f[1] == wled.REALTIME_TIMEOUT and len(f) == 2 + 60 * 3, f"DRGB frame for all 60 LEDs ({len(f)} bytes)"
)
check(
    f[2:5] == b"\xff\x00\x00" and f[2 + 29 * 3 : 5 + 29 * 3] == b"\xff\x00\x00" and f[-3:] == b"\x00\x00\xff",
    "30 shown LEDs spread over 60",
)
check({"on": True} in fake.posts, "the strip is switched on before colours are streamed")
n = len(fake.frames)
time.sleep(2.4)
check(len(fake.frames) >= n + 2, f"frames are repeated so the realtime colours do not expire ({len(fake.frames) - n})")
check(client.resize_zone(dev, 0, 60) and len(dev.colors) == 60, "LED count can be raised to the real one")
client.update_mode(dev, dev.mode_named("Off"))
client.wait_idle()
n = len(fake.frames)
time.sleep(1.3)
check(fake.state["on"] is False and len(fake.frames) == n, "Off: switched off, streaming stops")
client.update_mode(dev, dev.mode_named("Direct"))
client.update_leds(dev, [red] * 60)
client.wait_idle()
fake.posts.clear()
client.close()
check(
    any(p.get("seg", {}).get("i", [None])[:2] == [0, "FF0000"] for p in fake.posts),
    f"closing writes the look into WLED itself ({str(fake.posts)[:80]})",
)

# more than 490 LEDs: DNRGB packets with a start index
big = FakeWled(leds=600)
info = wled.check("127.0.0.1")
wled.HTTP_PORT = big.http.server_address[1]
strip = wled.WledStrip(wled.check("127.0.0.1"), {"on": True})
strip.set_mode(strip.device.mode_named("Direct"))
time.sleep(0.3)
heads = sorted((fr[0], fr[2] << 8 | fr[3], (len(fr) - 4) // 3) for fr in big.frames)
check(heads == [(4, 0, 489), (4, 489, 111)], f"long strips are sent in DNRGB chunks ({heads})")
strip.close()
big.stop()
wled.HTTP_PORT = fake.http.server_address[1]

# something else answering HTTP is not a WLED; a controller that went away is reported, then dropped
other = FakeWled(brand="Shelly")
wled.HTTP_PORT = other.http.server_address[1]
check(wled.check("127.0.0.1") is None and wled.add("127.0.0.1") is None, "another device's web server is not a WLED")
other.stop()
wled.HTTP_PORT = fake.http.server_address[1]
client.refresh()
fake.stop()
wled._refresh(search=False)
check(client.poll_notifications(), "a controller that went away triggers a rescan")
check(
    wled.status_report() == [{"name": "WLED controller", "ip": "127.0.0.1", "state": "silent"}],
    "and is listed as silent",
)
client.refresh()
check(client.devices == [], "gone from the devices")
check(wled.forget("127.0.0.1") and wled.status_report() == [], "forgotten on request")
client.shutdown()

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
