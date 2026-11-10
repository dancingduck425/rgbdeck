"""The phone page's server on loopback with a stand-in for the app: who gets in, what may be asked."""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rgbdeck import remote  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


calls = []
STATE = {
    "master": 80,
    "dark": False,
    "presets": ["Night"],
    "preset": None,
    "animation": None,
    "animations": [],
    "devices": [],
}


def call(text):
    calls.append(text)
    if text == "state":
        return "ok " + json.dumps(STATE)
    return f"ok {text}"


r = remote.Remote(call, "s3cret-token", port=0, host="127.0.0.1")
check(r.start() and r.running and r.port > 0, "server starts on a free port")
base = f"http://127.0.0.1:{r.port}"


def get(path, data=None):
    req = urllib.request.Request(base + path, data=data)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


code, body, headers = get("/?k=s3cret-token")
check(
    code == 200 and "<title>RGB Deck</title>" in body and headers.get("Cache-Control") == "no-store",
    "the page is served with the token",
)
check(
    get("/")[0] == 403 and get("/?k=wrong")[0] == 403 and get("/api/state")[0] == 403,
    "no token, wrong token: 403 everywhere",
)
check(get("/api/do?k=nope", b"toggle")[0] == 403 and calls == [], "and nothing reaches the app")
code, body, _ = get("/api/state?k=s3cret-token")
check(code == 200 and json.loads(body)["master"] == 80, "state as JSON")
code, body, _ = get("/api/do?k=s3cret-token", b"brightness 60")
check(
    code == 200 and json.loads(body) == {"reply": "ok brightness 60"} and calls[-1] == "brightness 60",
    "a command is passed on",
)
code, body, _ = get("/api/do?k=s3cret-token", b"preset Night")
check(code == 200 and calls[-1] == "preset Night", "preset by name")
n = len(calls)
refused = get("/api/do?k=s3cret-token", b"flash ffffff")[0] == 400 and get("/api/do?k=s3cret-token", b"state")[0] == 400
check(refused, "verbs outside the page's list are refused")
check(get("/api/do?k=s3cret-token", b"toggle\npreset x")[0] == 400, "one command per request")
check(
    get("/api/do?k=s3cret-token", b"color " + b"f" * 600)[0] == 400 and len(calls) == n, "oversized bodies are refused"
)
check(get("/nothing?k=s3cret-token")[0] == 404, "unknown paths: 404")
check("s3cret-token" in r.url() and str(r.port) in r.url(), "the link carries address, port and token")
for addr, ok in (
    ("192.168.1.20", True),
    ("10.0.0.5", True),
    ("127.0.0.1", True),
    ("fe80::1%eth0", True),
    ("::ffff:192.168.1.9", True),
    ("8.8.8.8", False),
    ("2001:4860:4860::8888", False),
    ("garbage", False),
):
    check(remote.allowed_client(addr) is ok, f"client {addr}: {'allowed' if ok else 'refused'}")
check(len(remote.new_token()) >= 16 and remote.new_token() != remote.new_token(), "tokens are random")
r.stop()
check(not r.running, "stops")
busy = remote.Remote(call, "t", port=0, host="127.0.0.1")
busy.start()
clash = remote.Remote(call, "t", port=busy.port, host="127.0.0.1")
check(not clash.start() and clash.error, "a taken port is reported, not fatal")
busy.stop()

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
