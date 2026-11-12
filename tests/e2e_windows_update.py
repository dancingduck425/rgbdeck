"""The update window on a real Windows machine, start to finish (run by the Windows build, see ci.yml):
install the freshly built setup, let the program believe it is an old version, have the update window press
its own buttons, and check what is left running afterwards.

    python tests/e2e_windows_update.py <RGBDeck-Setup-x.exe> <RGBDeck.exe>

Needs the network: "Update now" downloads the newest published release from GitHub."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ["APPDATA"]) / "rgbdeck"
LOG = CONFIG / "rgbdeck.log"
ROOT = Path(os.environ.get("RUNNER_TEMP") or os.environ["TEMP"]) / "rgb deck test"
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def running():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq RGBDeck.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True
    ).stdout
    return [line.split(",")[1].strip('"') for line in out.splitlines() if line.lower().startswith('"rgbdeck.exe"')]


def log_text():
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def clean():
    subprocess.run(["taskkill", "/IM", "RGBDeck.exe", "/F", "/T"], capture_output=True)
    deadline = time.monotonic() + 15
    while running() and time.monotonic() < deadline:
        time.sleep(0.5)
    time.sleep(1)
    shutil.rmtree(CONFIG, ignore_errors=True)


def start(exe, choice, *args):
    env = dict(os.environ, RGBDECK_PRETEND_VERSION="1.0.0", RGBDECK_UPDATE_TEST=choice)
    subprocess.Popen([str(exe), *args], env=env, close_fds=True)


def wait_for(what, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if what():
            return True
        time.sleep(1)
    return False


def show_log(title):
    print(f"--- log after {title}\n{log_text()}\n---", flush=True)


def decline(exe, choice, *args):
    title = f"{choice} {' '.join(args)}".strip()
    clean()
    start(exe, choice, *args)
    offered = wait_for(lambda: "update available" in log_text(), 90)
    check(offered, f"[{title}] the update is offered")
    time.sleep(10)  # the button is pressed two seconds after the window opens
    alive = running()
    check(bool(alive), f"[{title}] RGB Deck is still running after the button was pressed")
    check("Traceback" not in log_text(), f"[{title}] nothing went wrong in the log")
    if not (offered and alive):
        show_log(title)


def update(exe, title):
    clean()
    start(exe, "update")
    check(wait_for(lambda: "update available" in log_text(), 90), f"[{title}] the update is offered")
    left = wait_for(lambda: "restarting after the update" in log_text(), 300)
    check(left, f"[{title}] the new version was downloaded and the old program closed to make room")
    back = wait_for(lambda: log_text().count("RGB Deck starting") >= 2 and bool(running()), 240)
    check(back, f"[{title}] the new version started by itself")
    time.sleep(8)
    check(bool(running()), f"[{title}] and it stays running")
    show_log(title)


def main():
    setup, portable = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    shutil.rmtree(ROOT, ignore_errors=True)
    installed = ROOT / "RGB Deck"
    subprocess.run(
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", f"/DIR={installed}"],
        check=True,
        timeout=300,
    )
    exe = installed / "RGBDeck.exe"
    check(exe.exists() and any(installed.glob("unins*.exe")), "the setup installed the program")

    for args in ((), ("--tray",)):
        for choice in ("later", "skip"):
            decline(exe, choice, *args)
    update(exe, "installed with the setup")

    lone = ROOT / "portable" / "RGBDeck.exe"
    lone.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(portable, lone)
    update(lone, "single exe")
    check(not lone.with_name("RGBDeck.exe.new").exists(), "[single exe] no half-finished download is left behind")
    clean()
    print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
