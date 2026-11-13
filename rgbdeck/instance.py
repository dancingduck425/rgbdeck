# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""One RGB Deck per user.

Two copies talking to the same keyboard at once read each other's answers: devices go missing in one of them
and each undoes what the other shows. The control channel (app.py) only exists once a window has been built,
which leaves seconds at login in which a second start sees nobody. So the claim is a lock on a file, taken
first thing: the operating system gives it to exactly one process and takes it back when that process ends,
however it ends. The file also names its holder, so a copy that no longer answers can be ended and replaced.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

_LOCK_AT = 1 << 20  # the locked byte lies far behind the text, so other processes can still read who holds it


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, mode: str = "window") -> bool:
        """Claim the devices for this process. False: another process holds them (see owner())."""
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True  # no place for a lock file: carry on without one rather than not start at all
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, _LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self.note(mode)
        return True

    def note(self, mode: str):
        """Write down what the holder is doing: "window" (the program with its window or tray icon), "apply"
        (setting a preset without a window, done in a moment) or "effect" (playing one without a window, for
        as long as nobody else wants the devices)."""
        if self._fd is None:
            return
        try:
            text = json.dumps({"pid": os.getpid(), "mode": mode, "since": time.time()}).encode()
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, text + b"\n")
            os.ftruncate(self._fd, len(text) + 1)
        except OSError:
            pass  # the lock counts; the note about who holds it is a convenience

    def owner(self) -> dict:
        """What the holder wrote about itself: {"pid": ..., "mode": "window" | "apply", "since": ...}."""
        try:
            with open(self.path, "rb") as f:
                info = json.loads(f.read(400).split(b"\n", 1)[0] or b"{}")
        except (OSError, ValueError):
            return {}
        return info if isinstance(info, dict) else {}

    def release(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)  # closing gives the lock back
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------- other copies
def _win_open(pid: int, access: int):
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32, k32.OpenProcess(access, False, int(pid))


def process_command(pid: int) -> str:
    """The program behind a process id (Windows: its file; elsewhere: its command line). "" if there is none."""
    if not pid or pid <= 0:
        return ""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32, h = _win_open(pid, 0x1000)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            k32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            k32.CloseHandle(h)
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return f.read(4096).replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def is_rgbdeck(pid: int, own: Optional[str] = None) -> bool:
    """Is this process a copy of RGB Deck? (Asked before ending one: a process id alone proves nothing.)"""
    if not pid or pid == os.getpid():
        return False
    cmd = process_command(pid)
    if not cmd:
        return False
    if sys.platform == "win32":
        own = os.path.normcase(os.path.abspath(own or sys.executable))
        theirs = os.path.normcase(os.path.abspath(cmd))
        if theirs == own:
            return True
        # the same Python installation started as python.exe here and pythonw.exe there
        both_python = all(os.path.basename(p).startswith("python") for p in (own, theirs))
        return both_python and os.path.dirname(own) == os.path.dirname(theirs)
    return "rgbdeck" in cmd.lower()


def alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        from ctypes import wintypes

        k32, h = _win_open(pid, 0x00100000)  # SYNCHRONIZE
        if not h:
            return False
        try:
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:  # ended but not collected by its parent yet: gone for every purpose here
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            return f.read(400).rsplit(b")", 1)[-1].split()[:1] != [b"Z"]
    except OSError:
        return True


def end_process(pid: int, wait: float = 5.0) -> bool:
    """End a process that does not react any more. True once it is gone."""
    if sys.platform == "win32":
        from ctypes import wintypes

        k32, h = _win_open(pid, 0x0001 | 0x00100000)  # PROCESS_TERMINATE | SYNCHRONIZE
        if not h:
            return not alive(pid)
        try:
            k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.TerminateProcess(h, 1)
            return k32.WaitForSingleObject(h, int(wait * 1000)) == 0
        finally:
            k32.CloseHandle(h)
    for sig, patience in ((signal.SIGTERM, min(2.0, wait)), (signal.SIGKILL, wait)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            if not alive(pid):
                return True
            time.sleep(0.05)
    return not alive(pid)
