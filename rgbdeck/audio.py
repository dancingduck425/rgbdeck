# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
What the PC is playing, as eight frequency bands and a beat, for the music animations.

The sound is taken from the output side (what goes to the speakers), never from a microphone:

    Linux     parec / pw-record reading the default sink's monitor (PulseAudio or PipeWire)
    Windows   WASAPI loopback through the optional `soundcard` package

Analysis is cheap on purpose: one Goertzel filter per band over the last 512 samples (23 ms at 22.05 kHz),
with numpy's FFT used instead when numpy happens to be installed. Levels are normalised against a slowly
falling peak, so quiet and loud music both fill the range.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import List, Optional, Tuple

log = logging.getLogger("rgbdeck.audio")

RATE = 22050
WINDOW = 512
BANDS = (60.0, 130.0, 260.0, 520.0, 1000.0, 2000.0, 4000.0, 8000.0)  # centre frequencies, Hz
TILT = (1.0, 1.0, 1.1, 1.3, 1.6, 2.0, 2.6, 3.4)
SILENCE = 60.0  # mean magnitude (of 32768) below which nothing is playing

try:  # optional: a real FFT when numpy is around
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None


class Levels:
    """bands: eight values 0..1 (bass first); bass: 0..1; beat: 1.0 on a beat, falling to 0; active: sound now."""

    __slots__ = ("bands", "bass", "beat", "active")

    def __init__(self, bands=None, bass=0.0, beat=0.0, active=False):
        self.bands: List[float] = list(bands) if bands is not None else [0.0] * len(BANDS)
        self.bass, self.beat, self.active = bass, beat, active


class Analyzer:
    """Turns a stream of signed 16-bit mono samples into Levels. Feed it WINDOW samples at a time."""

    def __init__(self, rate: int = RATE):
        self.rate = rate
        self._coeff = [2.0 * math.cos(2.0 * math.pi * f / rate) for f in BANDS]
        self._peak = 1.0
        self._hann: List[float] = []
        self._smooth = [0.0] * len(BANDS)
        self._bass_avg = 0.0
        self._beat = 0.0
        self._beat_at = 0.0
        self._lock = threading.Lock()
        self._levels = Levels()
        self._last_feed = 0.0

    def _magnitudes(self, samples: Tuple[int, ...]) -> List[float]:
        n = len(samples)
        if _np is not None:
            spec = _np.abs(_np.fft.rfft(_np.asarray(samples, dtype=_np.float32) * _np.hanning(n))) / n
            out = []
            for f in BANDS:
                lo, hi = int(f / 1.41 * n / self.rate), int(f * 1.41 * n / self.rate) + 1
                out.append(float(spec[max(1, lo) : max(2, hi)].max()) * 2.0)
            return out
        if len(self._hann) != n:
            self._hann = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]
        windowed = [x * h for x, h in zip(samples, self._hann)]
        out = []
        for c in self._coeff:  # Goertzel: the energy at one frequency
            s1 = s2 = 0.0
            for x in windowed:
                s1, s2 = x + c * s1 - s2, s1
            power = s1 * s1 + s2 * s2 - c * s1 * s2
            out.append(math.sqrt(max(0.0, power)) / n * 2.0)
        return out

    def feed(self, raw: bytes, now: Optional[float] = None):
        n = len(raw) // 2
        if n < 64:
            return
        now = time.monotonic() if now is None else now
        samples = struct.unpack(f"<{n}h", raw[: n * 2])
        loud = sum(abs(x) for x in samples[::8]) / (n / 8)
        # music carries far less energy up high: tilt the bands so treble gets to move too
        mags = [m * tilt for m, tilt in zip(self._magnitudes(samples), TILT)]
        self._peak = max(max(mags), self._peak * 0.995, 150.0)  # one slowly falling reference for all bands
        bands = []
        for i, m in enumerate(mags):
            level = min(1.0, m / self._peak) if loud >= SILENCE else 0.0
            # rise at once, fall gently: bars that drop instead of flickering
            self._smooth[i] = level if level > self._smooth[i] else self._smooth[i] * 0.80 + level * 0.20
            bands.append(self._smooth[i])
        bass_now = max(mags[0], mags[1])
        if loud >= SILENCE and bass_now > self._bass_avg * 1.5 and bass_now > 120.0 and now - self._beat_at > 0.18:
            self._beat, self._beat_at = 1.0, now
        else:
            self._beat *= 0.82
        self._bass_avg = self._bass_avg * 0.93 + bass_now * 0.07
        with self._lock:
            self._levels = Levels(bands, max(bands[0], bands[1]), self._beat, loud >= SILENCE)
            self._last_feed = now

    def levels(self, now: Optional[float] = None) -> Levels:
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._last_feed > 0.5:  # the capture stalled: nothing to show
                return Levels()
            return self._levels


def _linux_command() -> Optional[List[str]]:
    if shutil.which("parec"):
        return [
            "parec",
            "--device=" + (os.environ.get("RGBDECK_AUDIO_DEVICE") or "@DEFAULT_MONITOR@"),
            "--format=s16le",
            f"--rate={RATE}",
            "--channels=1",
            "--latency-msec=30",
        ]
    if shutil.which("pw-record"):
        return ["pw-record", "--target=@DEFAULT_AUDIO_SINK@", "-P", "stream.capture.sink=true", "--format=s16",
                f"--rate={RATE}", "--channels=1", "-"]  # fmt: skip
    return None


def available() -> Tuple[bool, str]:
    """Can the PC's own sound be captured here? (ok, why not)."""
    if sys.platform == "win32":
        try:
            import soundcard  # noqa: F401
        except Exception:  # noqa: BLE001
            return False, "music animations on Windows need the 'soundcard' package (pip install soundcard)"
        return True, ""
    if _linux_command() is None:
        return False, "music animations need 'parec' (pulseaudio-utils / libpulse) or 'pw-record' (PipeWire)"
    return True, ""


class Capture(threading.Thread):
    def __init__(self, analyzer: Analyzer):
        super().__init__(name="rgbdeck-audio", daemon=True)
        self.analyzer = analyzer
        self._stop_flag = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.error = ""

    def run(self):
        try:
            if sys.platform == "win32":
                self._run_windows()
            else:
                self._run_linux()
        except Exception as e:  # noqa: BLE001 - no sound capture must never take the app down
            self.error = str(e)
            log.warning("audio capture stopped: %s", e)

    def _run_linux(self):
        cmd = _linux_command()
        if cmd is None:
            self.error = available()[1]
            return
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while not self._stop_flag.is_set():
                raw = self._proc.stdout.read(WINDOW * 2)
                if not raw:
                    self.error = "the sound server closed the stream"
                    return
                self.analyzer.feed(raw)
        finally:
            self._proc.terminate()

    def _run_windows(self):
        import soundcard

        speaker = soundcard.default_speaker()
        mic = soundcard.get_microphone(str(speaker.name), include_loopback=True)
        with mic.recorder(samplerate=RATE, channels=1, blocksize=WINDOW) as rec:
            while not self._stop_flag.is_set():
                data = rec.record(numframes=WINDOW)
                raw = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, float(x[0]))) * 32767)) for x in data)
                self.analyzer.feed(raw)

    def stop(self):
        self._stop_flag.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass


_capture: Optional[Capture] = None
_analyzer = Analyzer()


def start() -> Tuple[bool, str]:
    """Begin listening (idempotent). (ok, why not)."""
    global _capture, _analyzer
    ok, why = available()
    if not ok:
        return False, why
    if _capture is None or not _capture.is_alive():
        _analyzer = Analyzer()
        _capture = Capture(_analyzer)
        _capture.start()
    return True, ""


def stop():
    global _capture
    if _capture is not None:
        _capture.stop()
        _capture = None


def levels() -> Levels:
    return _analyzer.levels()
