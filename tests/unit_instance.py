# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""One RGB Deck per user: the lock that says who drives the devices, and replacing a copy that is stuck."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = tempfile.mkdtemp(prefix="rgbdeck-unit-")
os.environ["XDG_CONFIG_HOME"] = TMP
os.environ["APPDATA"] = TMP
os.environ["HOME"] = TMP
os.environ["RGBDECK_NO_BLUETOOTH"] = "1"
os.environ["RGBDECK_NO_UPDATE_CHECK"] = "1"
os.environ["RGBDECK_NO_DESKTOP_HOOKS"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rgbdeck import instance  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


LOCK = Path(TMP) / "rgbdeck" / "instance.lock"
# a stand-in for another copy: takes the lock (or reports that it could not) and then just sits there, the
# way a copy does whose window thread hangs. "rgbdeck" is in its command line, as it is in the real one's.
HOLDER = (
    "import sys, time; sys.path.insert(0, sys.argv[1]); from rgbdeck import instance; "
    "lock = instance.InstanceLock(sys.argv[2]); got = lock.acquire(sys.argv[3]); "
    "print('held' if got else 'taken', flush=True); time.sleep(float(sys.argv[4]) if got else 0)"
)


def holder(mode="window", seconds=60.0):
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER, ROOT, str(LOCK), mode, str(seconds), "rgbdeck-test-holder"],
        stdout=subprocess.PIPE,
        text=True,
    )
    return p, p.stdout.readline().strip()


mine = instance.InstanceLock(LOCK)
check(mine.acquire("window") and mine.held, "the first process gets the lock")
info = mine.owner()
check(info.get("pid") == os.getpid() and info.get("mode") == "window", f"and says who it is ({info})")
check(not instance.InstanceLock(LOCK).acquire(), "a second claim in the same process is turned down")
p, said = holder()
p.wait(10)
check(said == "taken", f"another process is turned down ({said})")
mine.release()
check(not mine.held, "released")

p, said = holder("apply")
check(said == "held", "once it is released, another process gets it")
check(not mine.acquire(), "and this one is turned down in turn")
info = mine.owner()
check(info.get("pid") == p.pid and info.get("mode") == "apply", f"the file names the holder ({info})")
check(instance.alive(p.pid) and instance.is_rgbdeck(p.pid), "the holder is recognised as a copy of RGB Deck")
check(not instance.is_rgbdeck(os.getpid()), "this process is never its own 'other copy'")
other = subprocess.Popen(["cmd.exe", "/c", "ping -n 30 127.0.0.1 >nul"] if sys.platform == "win32" else ["sleep", "30"])
check(not instance.is_rgbdeck(other.pid), "some other program is not")
other.kill()
other.wait(10)
check(not instance.alive(other.pid), "a process that has ended is not alive")
check(instance.end_process(p.pid) and not instance.alive(p.pid), "a copy can be ended")
p.wait(10)
deadline = time.monotonic() + 5
while not mine.acquire() and time.monotonic() < deadline:
    time.sleep(0.05)
check(mine.held, "the lock of a process that died is free again")
mine.release()

if sys.platform != "win32":
    import signal

    p, said = holder()
    os.kill(p.pid, signal.SIGSTOP)  # frozen solid: does not even react to a polite request to end
    t0 = time.monotonic()
    check(instance.end_process(p.pid, wait=5.0), f"a frozen copy is ended too ({time.monotonic() - t0:.1f} s)")
    p.wait(10)

# ---- the start of the program: hand over to a copy that answers, replace one that does not -------------------
import rgbdeck.app as app_mod  # noqa: E402

app_mod.LOCK_FILE = LOCK
app_mod.START_GRACE = 1.5
app_mod.APPLY_GRACE = 10.0
app_mod.EFFECT_GRACE = 0.3

lock = instance.InstanceLock(LOCK)
check(app_mod.claim_devices(lock, "window", "show") is None and lock.held, "nobody else: the devices are ours")
lock.release()

p, said = holder()
asked = []
real_send = app_mod.send_control


def answering(cmd):
    asked.append(cmd)
    return "ok" if len(asked) >= 3 else None  # the other copy is still starting: it answers a moment later


app_mod.send_control = answering
lock = instance.InstanceLock(LOCK)
t0 = time.monotonic()
reply = app_mod.claim_devices(lock, "window", "show")
check(
    reply == "ok" and not lock.held and instance.alive(p.pid),
    f"a copy that is still starting is waited for and then asked ({reply}, {len(asked)} tries)",
)
app_mod.send_control = lambda cmd: None  # ... and one that never answers
t0 = time.monotonic()
reply = app_mod.claim_devices(lock, "window", "show")
took = time.monotonic() - t0
check(
    reply is None and lock.held and not instance.alive(p.pid),
    f"a copy that holds the devices without answering is replaced ({took:.1f} s)",
)
check(took >= 1.4, "but only after it has had its time")
p.wait(10)
lock.release()

p, said = holder("effect")
t0 = time.monotonic()
reply = app_mod.claim_devices(lock, "window", "show")
took = time.monotonic() - t0
check(
    reply is None and lock.held and took < 1.4 and not instance.alive(p.pid),
    f"a copy that only plays an effect without a window makes way at once ({took:.1f} s)",
)
p.wait(10)
lock.release()

p, said = holder("apply", 1.0)  # setting a preset without a window: done in a moment, must not be cut short
t0 = time.monotonic()
reply = app_mod.claim_devices(lock, "window", "show")
took = time.monotonic() - t0
check(
    reply is None and lock.held and p.wait(10) == 0,
    f"a copy that is applying a preset is left to finish ({took:.1f} s, it ended with {p.returncode})",
)
lock.release()
app_mod.send_control = real_send

# a program that has decided to end, ends: whatever still hangs in it is cut off after a while
GUARD = (
    "import sys, time; sys.path.insert(0, sys.argv[1]); import rgbdeck.app as a; "
    "a.setup_logging(False); a.EXIT_GUARD = True; a.arm_exit_guard(limit=1.0, code=7); time.sleep(60)"
)
t0 = time.monotonic()
rc = subprocess.run([sys.executable, "-c", GUARD, ROOT], timeout=50).returncode
check(rc == 7 and time.monotonic() - t0 < 30, f"something that will not let go cannot keep the process ({rc})")
check(
    app_mod.LOG_FILE.exists() and "ending the process the hard way" in app_mod.LOG_FILE.read_text(errors="replace"),
    "... and the log says that it came to that",
)

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
