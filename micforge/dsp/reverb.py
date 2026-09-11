"""Freeverb-style reverb (8 damped combs into 4 allpasses).

Processed in chunks no longer than the shortest internal delay, so every
recursion stays block-safe no matter what block size the engine runs at.
"""
from __future__ import annotations

import numpy as np

from . import Stage, clampf
from .effects import DelayLine

try:
    from scipy.signal import lfilter  # type: ignore

    _HAVE_SCIPY = True
except Exception:  # pragma: no cover
    _HAVE_SCIPY = False

# Original Freeverb tunings, in samples at 44100 Hz.
COMB_TUNING = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
ALLPASS_TUNING = (556, 441, 341, 225)
FIXED_GAIN = 0.015
SCALE_DAMP = 0.4
SCALE_ROOM = 0.28
OFFSET_ROOM = 0.7


class _Comb:
    def __init__(self, delay: int):
        self.delay = int(delay)
        self.line = DelayLine(self.delay + 8)
        self.line.buf[:] = 0.0
        self.feedback = 0.5
        self.damp = 0.5
        self._store = 0.0

    def reset(self) -> None:
        self.line.reset()
        self._store = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = self.line.read(float(self.delay), x.size)
        d = self.damp
        if _HAVE_SCIPY:
            filtered, zf = lfilter([1.0 - d], [1.0, -d], out,
                                   zi=[self._store * d])
            self._store = float(filtered[-1]) if filtered.size else self._store
        else:  # pragma: no cover
            filtered = np.empty_like(out)
            acc = self._store
            for i in range(out.size):
                acc = out[i] * (1.0 - d) + acc * d
                filtered[i] = acc
            self._store = float(acc)
        self.line.write(x + filtered * self.feedback)
        return out


class _AllPass:
    def __init__(self, delay: int):
        self.delay = int(delay)
        self.line = DelayLine(self.delay + 8)
        self.gain = 0.5

    def reset(self) -> None:
        self.line.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        bufout = self.line.read(float(self.delay), x.size)
        self.line.write(x + bufout * self.gain)
        return bufout - x


class Reverb(Stage):
    kind = "reverb"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.size = 0.5
        self.damping = 0.5
        self.mix = 0.2
        self.width = 1.0
        self.predelay_ms = 15.0

        scale = sr / 44100.0
        self._combs = [_Comb(max(int(round(t * scale)), 32)) for t in COMB_TUNING]
        self._aps = [_AllPass(max(int(round(t * scale)), 32)) for t in ALLPASS_TUNING]
        self._chunk = max(32, min([c.delay for c in self._combs]
                                  + [a.delay for a in self._aps]) - 1)
        self._pre = DelayLine(int(sr * 0.25) + 64)
        self._apply()

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.size = clampf(float(params.get("size", 0.5)), 0.0, 1.0)
        self.damping = clampf(float(params.get("damping", 0.5)), 0.0, 1.0)
        self.mix = clampf(float(params.get("mix", 0.2)), 0.0, 1.0)
        self.width = clampf(float(params.get("width", 1.0)), 0.0, 1.0)
        self.predelay_ms = clampf(float(params.get("predelay_ms", 15.0)), 0.0, 200.0)
        self._apply()

    def _apply(self) -> None:
        fb = self.size * SCALE_ROOM + OFFSET_ROOM
        damp = self.damping * SCALE_DAMP
        for c in self._combs:
            c.feedback = fb
            c.damp = damp
        for a in self._aps:
            a.gain = 0.5

    def reset(self) -> None:
        for c in self._combs:
            c.reset()
        for a in self._aps:
            a.reset()
        self._pre.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        if n == 0:
            return x
        src = x.astype(np.float64)
        pre = self.predelay_ms * self.sr / 1000.0
        if pre >= 1.0:
            delayed = self._pre.read(max(pre, float(n)), n)
            self._pre.write(src)
            src = delayed

        out = np.empty(n, dtype=np.float64)
        pos = 0
        while pos < n:
            end = min(pos + self._chunk, n)
            block = src[pos:end] * FIXED_GAIN
            acc = np.zeros(end - pos, dtype=np.float64)
            for c in self._combs:
                acc += c.process(block)
            for a in self._aps:
                acc = a.process(acc)
            out[pos:end] = acc
            pos = end

        wet = out * (0.5 + 0.5 * self.width)
        return (x * (1.0 - self.mix) + wet * self.mix).astype(np.float32)
