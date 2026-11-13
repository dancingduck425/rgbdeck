# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""What is left behind when a run goes wrong: the marker of a run that did not end, the trace of a crash inside a
library, errors nobody caught, and the report that collects it all."""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="rgbdeck-unit-"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rgbdeck import diagnostics  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


grab = Grab()
logging.getLogger("rgbdeck").addHandler(grab)
logging.getLogger("rgbdeck").setLevel(logging.INFO)

folder = TMP / "a"
check(diagnostics.begin(folder, "9.9.9") == "", "a first start has nothing to say about the run before")
check((folder / "running.json").exists(), "a marker exists while the program runs")
diagnostics.end()
check(not (folder / "running.json").exists(), "and is gone after a proper end")
check(diagnostics.begin(folder, "9.9.9") == "", "after a proper end the next start has nothing to say either")
note = diagnostics.begin(folder, "9.9.10")  # started again without the end in between
check("did not end properly" in note and "9.9.9" in note, f"a run that did not end is noticed ({note})")
diagnostics.end()

# a crash inside a library: the process dies on the spot, the trace is what remains
folder = TMP / "b"
CRASH = (
    "import sys, faulthandler; sys.path.insert(0, sys.argv[1]); from pathlib import Path\n"
    "if sys.platform == 'win32':\n"
    "    import ctypes; ctypes.windll.kernel32.SetErrorMode(0x8003)\n"  # no error dialog on the build machine
    "from rgbdeck import diagnostics\n"
    "diagnostics.begin(Path(sys.argv[2]), '1.2.3')\n"
    "def deep_inside_a_driver():\n"
    "    faulthandler._sigsegv()\n"
    "deep_inside_a_driver()\n"
)
rc = subprocess.run([sys.executable, "-c", CRASH, ROOT, str(folder)], stderr=subprocess.DEVNULL).returncode
check(rc != 0, f"the stand-in crashed ({rc})")
note = diagnostics.begin(folder, "1.2.4")
check(
    "did not end properly" in note and "1.2.3" in note and "deep_inside_a_driver" in note,
    "the next start knows the run crashed, and where: " + note.replace("\n", " | ")[:150],
)
check("deep_inside_a_driver" in (folder / "crash-previous.txt").read_text(), "the trace is kept for the report")
diagnostics.end()
check(diagnostics.begin(folder, "1.2.4") == "", "and is reported once, not at every start")


# errors nobody caught end up in the log, from any thread
def breaks():
    raise ValueError("nobody catches this")


t = threading.Thread(target=breaks, name="some-worker")
t.start()
t.join()
texts = [r.getMessage() for r in grab.records]
check(
    any("some-worker" in m for m in texts)
    and any(r.exc_info and "nobody catches this" in str(r.exc_info[1]) for r in grab.records),
    "an error in a thread is logged with the thread's name",
)
try:
    raise RuntimeError("top level")
except RuntimeError:
    sys.excepthook(*sys.exc_info())
check(any(r.exc_info and "top level" in str(r.exc_info[1]) for r in grab.records), "and so is one on the main thread")
diagnostics.end()

# the report
log_file = folder / "rgbdeck.log"
log_file.write_text("\n".join(f"line {i}" for i in range(1000)) + "\nthe last line\n")
mode = types.SimpleNamespace(name="Direct")
dev = types.SimpleNamespace(
    type_name="Keyboard",
    name="Test board",
    colors=[0] * 104,
    modes=[mode],
    current_mode=mode,
    experimental=True,
    location="usb:1",
)
client = types.SimpleNamespace(devices=[dev], last_error="some_driver did not answer in time")
text = diagnostics.report_text(
    "1.2.4", folder, log_file, client, {"sync": True, "remote": {"on": True, "token": "s3cret", "port": 8765}}
)
check(
    "RGB Deck 1.2.4 report" in text and "Keyboard: Test board · 104 lights" in text,
    "the report names version and devices",
)
check("did not answer in time" in text, "... what the scan complained about")
check("s3cret" not in text and '"port": 8765' in text, "... the settings, without the phone page's secret")
check("deep_inside_a_driver" in text, "... the crash trace")
check(text.rstrip().endswith("the last line") and "line 100\n" not in text, "... and the end of the log, not all of it")
check(diagnostics.report_target().name == "RGB Deck report.txt", "it is saved under a name that says what it is")

print("\nALL PASSED" if not fails else "\nFAILURES: " + str(fails))
sys.exit(1 if fails else 0)
