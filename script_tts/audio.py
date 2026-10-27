"""One output stream, with clip boundaries measured at the device's DAC clock."""
from __future__ import annotations

import threading

from .config import ROOT


class AudioPlayer:
    def __init__(self, *, sd=None):
        if sd is None:
            import sounddevice as sd
        self.sd = sd
        self.rate = 24000
        self.stream = None
        self.lock = threading.Lock()
        self.samples = None
        self.position = 0
        self.start_dac = None
        self.end_dac = None
        self.cues = {}

    def _callback(self, outdata, frames, timing, status):
        outdata.fill(0)
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.samples is None or self.position >= len(self.samples):
                return
            count = min(frames, len(self.samples) - self.position)
            outdata[:count, 0] = self.samples[self.position:self.position + count]
            if self.start_dac is None:
                self.start_dac = float(timing.outputBufferDacTime)
            self.position += count
            if self.position == len(self.samples):
                self.end_dac = float(timing.outputBufferDacTime) + count / self.rate
        finally:
            self.lock.release()

    def _ensure_stream(self):
        if self.stream is None:
            self.stream = self.sd.OutputStream(samplerate=self.rate, channels=1, dtype="float32", blocksize=0, latency="low", callback=self._callback)
            self.stream.start()

    def initialize(self):
        self._ensure_stream()
        for name in ("Starting.wav", "Stopped.wav"):
            self._cue_samples(name)

    def _resample(self, audio, rate):
        import numpy as np
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = audio.ravel()
        if not len(audio) or not np.isfinite(audio).all():
            raise ValueError("Cannot play empty or invalid audio.")
        if rate != self.rate:
            count = max(1, round(len(audio) * self.rate / rate))
            audio = np.interp(np.arange(count) * rate / self.rate, np.arange(len(audio)), audio).astype(np.float32)
        return np.ascontiguousarray(audio)

    def play(self, audio, rate):
        samples = self._resample(audio, rate)
        self._ensure_stream()
        with self.lock:
            self.samples = samples
            self.position = 0
            self.start_dac = None
            self.end_dac = None

    def _cue_samples(self, name):
        if name not in self.cues:
            path = ROOT / name
            if not path.is_file():
                return None
            import soundfile as sf
            audio, rate = sf.read(str(path), dtype="float32")
            self.cues[name] = self._resample(audio, rate)
        return self.cues[name]

    def cue(self, name):
        samples = self._cue_samples(name)
        if samples is not None:
            self.play(samples, self.rate)

    def started(self):
        with self.lock:
            start = self.start_dac
        return self.stream is not None and start is not None and self.stream.time >= start

    def active(self):
        with self.lock:
            samples, end = self.samples, self.end_dac
        if samples is None:
            return False
        if self.stream is None or not self.stream.active:
            raise RuntimeError("The audio output stopped. Check your playback device and start again.")
        return end is None or self.stream.time < end

    def stop(self):
        with self.lock:
            self.samples = None
            self.start_dac = self.end_dac = None
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.abort()
            stream.close()

    def close(self):
        self.stop()
