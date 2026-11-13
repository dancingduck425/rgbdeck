"""Starting and ending the packaged program on a real Windows machine (run by the Windows build, see ci.yml):
one copy at a time however it is started, a copy that hangs is replaced, and nothing is left running after Quit.

    python tests/e2e_windows_start.py <RGBDeck.exe>

There are no lighting devices on the build machine: this is about processes, not about colors."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

CONFIG = Path(os.environ["APPDATA"]) / "rgbdeck"
LOG = CONFIG / "rgbdeck.log"
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def processes():
    """Every RGBDeck.exe process: (pid, parent pid, command line)."""
    out = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='RGBDeck.exe'\" | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    data = [data] if isinstance(data, dict) else data
    return [(p["ProcessId"], p["ParentProcessId"], p.get("CommandLine") or "") for p in data]


def copies():
    """Started copies of the program. (The packaged program is two processes, a small one that unpacks and the
    program itself; the Bluetooth helper is a third. Those belong to their copy and are not counted.)"""
    procs = processes()
    pids = {p[0] for p in procs}
    return [p for p in procs if p[1] not in pids and "--ble-helper" not in p[2]]


def log_text():
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_for(what, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if what():
            return True
        time.sleep(0.5)
    return bool(what())


def clean():
    subprocess.run(["taskkill", "/IM", "RGBDeck.exe", "/F", "/T"], capture_output=True)
    wait_for(lambda: not processes(), 20)
    time.sleep(1)
    shutil.rmtree(CONFIG, ignore_errors=True)


def show_log(title):
    print(f"--- log: {title}", flush=True)
    print(log_text() or "(empty)", flush=True)
    print("--- processes", processes(), flush=True)


def holder_pid():
    try:
        return int(json.loads((CONFIG / "instance.lock").read_bytes()[:400].split(b"\n", 1)[0]).get("pid") or 0)
    except (OSError, ValueError):
        return 0


def freeze(pid):
    """Stop every thread of a process, the way a program looks that hangs for good."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not h:
        return False
    rc = ntdll.NtSuspendProcess(h)
    k32.CloseHandle(h)
    return rc == 0


def quit_program(exe):
    rc = subprocess.run([str(exe), "--quit"], timeout=90).returncode
    gone = wait_for(lambda: not processes(), 40)
    return rc, gone


def main():
    exe = Path(sys.argv[1]).resolve()

    # ---- one start, a second start, Quit
    clean()
    subprocess.Popen([str(exe), "--tray"], close_fds=True)
    up = wait_for(lambda: "devices:" in log_text(), 120)
    check(up and "RGB Deck starting: version" in log_text(), "the program starts and its device scan comes to an end")
    check(len(copies()) == 1, f"one copy is running ({copies()})")
    t0 = time.monotonic()
    rc = subprocess.run([str(exe)], timeout=120).returncode
    check(rc == 0, f"a second start hands over to the first and ends ({rc}, {time.monotonic() - t0:.0f} s)")
    time.sleep(2)
    check(
        len(copies()) == 1 and log_text().count("RGB Deck starting") == 1,
        f"... and there is still one copy ({len(copies())} running, {log_text().count('RGB Deck starting')} started)",
    )
    rc, gone = quit_program(exe)
    check(rc == 0 and gone, f"Quit ends every process ({rc}, left: {processes()})")
    check(
        not (CONFIG / "running.json").exists() and not (CONFIG / "control.json").exists(),
        "... and leaves no trace of a running program behind",
    )
    check("the hard way" not in log_text(), "... without having to be cut off")
    show_log("start, second start, quit")

    # ---- two starts in the same moment (a double click that registered twice; login entry plus the user)
    clean()
    subprocess.Popen([str(exe), "--tray"], close_fds=True)
    subprocess.Popen([str(exe), "--tray"], close_fds=True)
    wait_for(lambda: "devices:" in log_text(), 120)
    time.sleep(20)  # time enough for a second copy to come up, if one were going to
    check(
        len(copies()) == 1 and log_text().count("RGB Deck starting") == 1,
        f"started twice at once: one copy ({len(copies())} running, {log_text().count('RGB Deck starting')} started)",
    )
    rc, gone = quit_program(exe)
    check(rc == 0 and gone, "... which Quit ends")
    show_log("two starts at once")

    # ---- login: the tray copy and "apply this preset" are started side by side
    clean()
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "presets.json").write_text(
        json.dumps({"Last look": {"devices": [], "app_effect": None, "animation": None}})
    )
    subprocess.Popen([str(exe), "--tray"], close_fds=True)
    rc = subprocess.run([str(exe), "--apply", "Last look"], timeout=180).returncode
    wait_for(lambda: "devices:" in log_text(), 120)
    time.sleep(5)
    check(rc == 0, f"the preset is applied, by whichever of the two got to the devices ({rc})")
    check(
        len(copies()) == 1 and "ending it" not in log_text(),
        f"and neither had to end the other ({len(copies())} running)",
    )
    show_log("tray copy and preset at once")

    # ---- a copy that hangs for good is replaced by the next start
    stuck = holder_pid()
    check(stuck > 0 and freeze(stuck), f"the running copy is frozen (process {stuck})")
    t0 = time.monotonic()
    subprocess.Popen([str(exe), "--tray"], close_fds=True)
    replaced = wait_for(lambda: log_text().count("RGB Deck starting") >= 2 and holder_pid() not in (0, stuck), 120)
    took = time.monotonic() - t0
    check(replaced and "not answering: ending it" in log_text(), f"the next start replaces it ({took:.0f} s)")
    wait_for(lambda: len(copies()) == 1, 30)
    check(
        len(copies()) == 1 and stuck not in {p[0] for p in processes()},
        f"... and is the only copy left ({copies()})",
    )
    check("did not end properly" in log_text(), "... and has noted that the run before it did not end properly")
    rc, gone = quit_program(exe)
    check(rc == 0 and gone, "the new copy answers and ends on Quit")
    show_log("frozen copy")

    clean()
    print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
