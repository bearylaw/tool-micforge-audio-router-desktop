"""Modulation, saturation and delay effects.

All of these are block-vectorised. The only one with a feedback path (the delay)
requires its delay time to be at least one block long, which is enforced when
the parameter is set -- at the default 5 ms block that means a 6 ms minimum,
which no one is going to notice on an echo effect.
"""
from __future__ import annotations

import numpy as np

from . import Stage, clampf, db_to_lin

TWO_PI = 2.0 * np.pi


class DelayLine:
    """Circular delay line with fractional-index reads.

    Convention, and it matters: :meth:`read` returns the ``n`` samples ending at
    the current write head, so you must **read before you write** within a
    block, and the requested delay must be at least ``n``. Writing first shifts
    the head by a block and silently shortens every delay by that much, which
    makes the effect change character with the buffer size.
    """

    def __init__(self, max_frames: int):
        self.size = int(max(max_frames, 16))
        self.buf = np.zeros(self.size, dtype=np.float64)
        self.w = 0

    def reset(self) -> None:
        self.buf[:] = 0.0
        self.w = 0

    def write(self, data: np.ndarray) -> None:
        n = data.size
        if n == 0:
            return
        if n >= self.size:
            self.buf[:] = data[-self.size:]
            self.w = 0
            return
        idx = (self.w + np.arange(n)) % self.size
        self.buf[idx] = data
        self.w = (self.w + n) % self.size

    def read(self, delay: float, n: int) -> np.ndarray:
        """``n`` samples ending at the current write head, ``delay`` back."""
        pos = (self.w - delay + np.arange(n, dtype=np.float64)) % self.size
        i0 = np.floor(pos).astype(np.int64) % self.size
        i1 = (i0 + 1) % self.size
        frac = pos - np.floor(pos)
        return self.buf[i0] * (1.0 - frac) + self.buf[i1] * frac

    def read_modulated(self, delays: np.ndarray, head_offset: int = 0) -> np.ndarray:
        """Per-sample fractional delay.

        ``head_offset`` is how far the write head has already advanced past the
        start of this block. Pass ``0`` when reading before the write (delays
        must then be at least one block); pass ``n`` when reading after it,
        which is what effects with sub-block delays need.
        """
        n = delays.size
        pos = (self.w - head_offset - delays
               + np.arange(n, dtype=np.float64)) % self.size
        i0 = np.floor(pos).astype(np.int64) % self.size
        i1 = (i0 + 1) % self.size
        frac = pos - np.floor(pos)
        return self.buf[i0] * (1.0 - frac) + self.buf[i1] * frac


class _Lfo:
    """Free-running low-frequency oscillator with persistent phase."""

    def __init__(self, sr: int):
        self.sr = float(sr)
        self.phase = 0.0

    def block(self, n: int, rate_hz: float, waveform: str = "sine",
              offset: float = 0.0) -> np.ndarray:
        inc = TWO_PI * max(rate_hz, 0.0) / self.sr
        ph = self.phase + inc * np.arange(1, n + 1, dtype=np.float64) + offset
        self.phase = float((self.phase + inc * n) % TWO_PI)
        if waveform == "triangle":
            return 2.0 * np.abs(((ph / TWO_PI) % 1.0) * 2.0 - 1.0) - 1.0
        if waveform == "square":
            return np.where((ph % TWO_PI) < np.pi, 1.0, -1.0)
        if waveform == "saw":
            return ((ph / TWO_PI) % 1.0) * 2.0 - 1.0
        if waveform == "random":
            # Sample-and-hold, stepped at the LFO rate.
            steps = np.floor(ph / TWO_PI).astype(np.int64)
            rng = np.random.default_rng(abs(int(steps[0])) + 1)
            uniq, inv = np.unique(steps, return_inverse=True)
            vals = rng.uniform(-1.0, 1.0, size=uniq.size)
            return vals[inv]
        return np.sin(ph)


class RingMod(Stage):
    """Multiply by a carrier. Low frequencies buzz, high ones go full Dalek."""

    kind = "ringmod"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.freq = 60.0
        self.mix = 0.6
        self.waveform = "sine"
        self._lfo = _Lfo(sr)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.freq = clampf(float(params.get("freq", 60.0)), 0.1, 8000.0)
        self.mix = clampf(float(params.get("mix", 0.6)), 0.0, 1.0)
        self.waveform = str(params.get("waveform", "sine"))

    def reset(self) -> None:
        self._lfo.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        carrier = self._lfo.block(x.size, self.freq, self.waveform)
        wet = x * carrier
        return (x * (1.0 - self.mix) + wet * self.mix).astype(np.float32)


class BitCrush(Stage):
    """Quantise and decimate. Cheap, instantly recognisable lo-fi/8-bit."""

    kind = "bitcrush"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.bits = 8.0
        self.downsample = 2.0
        self.mix = 1.0
        self._hold = 0.0
        self._phase = 0.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.bits = clampf(float(params.get("bits", 8.0)), 1.0, 24.0)
        self.downsample = clampf(float(params.get("downsample", 2.0)), 1.0, 64.0)
        self.mix = clampf(float(params.get("mix", 1.0)), 0.0, 1.0)

    def reset(self) -> None:
        self._hold = 0.0
        self._phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        levels = 2.0 ** self.bits
        q = np.round(x.astype(np.float64) * levels) / levels

        step = self.downsample
        if step > 1.001:
            # Sample-and-hold: index of the most recent kept sample per output.
            pos = self._phase + np.arange(n, dtype=np.float64)
            keep_idx = np.floor(pos / step).astype(np.int64)
            src = np.floor(keep_idx * step - self._phase).astype(np.int64)
            before = src < 0
            src = np.clip(src, 0, n - 1)
            held = q[src]
            if np.any(before):
                held[before] = self._hold
            self._phase = float((self._phase + n) % step)
            self._hold = float(q[-1]) if n else self._hold
            q = held

        return (x * (1.0 - self.mix) + q * self.mix).astype(np.float32)


class Distortion(Stage):
    """Waveshaper with a tilt-EQ tone control."""

    kind = "distortion"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.drive_db = 12.0
        self.tone = 0.5
        self.mix = 1.0
        self.shape = "tanh"
        self._lp = 0.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.drive_db = clampf(float(params.get("drive_db", 12.0)), 0.0, 48.0)
        self.tone = clampf(float(params.get("tone", 0.5)), 0.0, 1.0)
        self.mix = clampf(float(params.get("mix", 1.0)), 0.0, 1.0)
        self.shape = str(params.get("kind", "tanh"))

    def reset(self) -> None:
        self._lp = 0.0

    def _shape(self, d: np.ndarray) -> np.ndarray:
        if self.shape == "hard":
            return np.clip(d, -1.0, 1.0)
        if self.shape == "fold":
            # Reflect anything past unity back on itself - very aggressive.
            y = np.mod(d + 1.0, 4.0) - 1.0
            return np.where(y > 1.0, 2.0 - y, y)
        if self.shape == "fuzz":
            return np.sign(d) * (1.0 - np.exp(-np.abs(d)))
        if self.shape == "sine":
            return np.sin(np.clip(d, -np.pi, np.pi) * 0.5 * np.pi) * 0.9
        return np.tanh(d)

    def process(self, x: np.ndarray) -> np.ndarray:
        drive = db_to_lin(self.drive_db)
        d = x.astype(np.float64) * drive
        y = self._shape(d)
        # Compensate roughly for the level the drive added.
        y = y / max(1.0, np.sqrt(drive))

        # Tone: blend a one-pole lowpassed copy with the raw one.
        a = 0.05 + 0.9 * (1.0 - self.tone)
        lp = np.empty_like(y)
        prev = self._lp
        # A one-pole over a short block, done with a cumulative trick to stay
        # out of a Python loop: exact for constant a.
        if a > 0.999:
            lp[:] = y
        else:
            coef = 1.0 - a
            pw = coef ** np.arange(1, y.size + 1)
            scaled = y * a / np.maximum(pw, 1e-300)
            lp = pw * (prev + np.cumsum(scaled))
            if not np.all(np.isfinite(lp)):  # very long blocks can overflow pw
                lp = np.empty_like(y)
                acc = prev
                for i in range(y.size):
                    acc += a * (y[i] - acc)
                    lp[i] = acc
        self._lp = float(lp[-1]) if y.size else prev
        out = lp * (1.0 - self.tone) + y * self.tone

        return (x * (1.0 - self.mix) + out * self.mix).astype(np.float32)


class Chorus(Stage):
    """Multi-voice modulated delay. Thickens a thin voice, or detunes it."""

    kind = "chorus"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.rate_hz = 0.8
        self.depth_ms = 3.0
        self.voices = 2
        self.mix = 0.35
        self.spread = 0.5
        self._line = DelayLine(int(sr * 0.2) + 4096)
        self._lfo = _Lfo(sr)
        self._base_ms = 12.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.rate_hz = clampf(float(params.get("rate_hz", 0.8)), 0.01, 12.0)
        self.depth_ms = clampf(float(params.get("depth_ms", 3.0)), 0.1, 25.0)
        self.voices = int(clampf(float(params.get("voices", 2)), 1, 4))
        self.mix = clampf(float(params.get("mix", 0.35)), 0.0, 1.0)
        self.spread = clampf(float(params.get("spread", 0.5)), 0.0, 1.0)

    def reset(self) -> None:
        self._line.reset()
        self._lfo.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        depth = self.depth_ms * self.sr / 1000.0
        base = max(self._base_ms * self.sr / 1000.0, depth + 2.0)

        # One phase ramp shared by every voice, advanced once at the end -
        # advancing per voice would make the spread depend on the block size.
        inc = TWO_PI * self.rate_hz / self.sr
        ph0 = self._lfo.phase
        ph = ph0 + inc * np.arange(1, n + 1, dtype=np.float64)
        self._lfo.phase = float((ph0 + inc * n) % TWO_PI)

        self._line.write(x.astype(np.float64))
        wet = np.zeros(n, dtype=np.float64)
        for v in range(self.voices):
            offset = TWO_PI * v * (0.25 + 0.5 * self.spread)
            delays = base * (1.0 + 0.3 * v) + depth * np.sin(ph + offset)
            wet += self._line.read_modulated(np.maximum(delays, 1.0), head_offset=n)
        wet /= self.voices
        return (x * (1.0 - self.mix) + wet * self.mix).astype(np.float32)


class Tremolo(Stage):
    """Amplitude modulation. At high rates it turns into a stutter effect."""

    kind = "tremolo"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.rate_hz = 5.0
        self.depth = 0.5
        self.waveform = "sine"
        self._lfo = _Lfo(sr)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.rate_hz = clampf(float(params.get("rate_hz", 5.0)), 0.05, 40.0)
        self.depth = clampf(float(params.get("depth", 0.5)), 0.0, 1.0)
        self.waveform = str(params.get("waveform", "sine"))

    def reset(self) -> None:
        self._lfo.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        lfo = self._lfo.block(x.size, self.rate_hz, self.waveform)
        gain = 1.0 - self.depth * (0.5 - 0.5 * lfo)
        return (x * gain).astype(np.float32)


class Delay(Stage):
    """Feedback echo with a damped repeat tail."""

    kind = "delay"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.time_ms = 180.0
        self.feedback = 0.3
        self.mix = 0.25
        self.damping = 0.3
        self._line = DelayLine(int(sr * 3.0) + 64)
        self._damp_state = 0.0
        self._min_delay = 64

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.time_ms = clampf(float(params.get("time_ms", 180.0)), 1.0, 2500.0)
        self.feedback = clampf(float(params.get("feedback", 0.3)), 0.0, 0.95)
        self.mix = clampf(float(params.get("mix", 0.25)), 0.0, 1.0)
        self.damping = clampf(float(params.get("damping", 0.3)), 0.0, 0.95)

    def reset(self) -> None:
        self._line.reset()
        self._damp_state = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        # The feedback loop is closed once per block, so the delay can never be
        # shorter than the block or we would need the output before we have it.
        delay = max(self.time_ms * self.sr / 1000.0, float(n), self._min_delay)
        echo = self._line.read(delay, n)
        if self.damping > 0.001:
            a = 1.0 - self.damping
            damped = np.empty_like(echo)
            acc = self._damp_state
            for i in range(n):  # short block, and only when damping is on
                acc += a * (echo[i] - acc)
                damped[i] = acc
            self._damp_state = float(acc)
            echo_fb = damped
        else:
            echo_fb = echo
        self._line.write(x.astype(np.float64) + echo_fb * self.feedback)
        return (x * (1.0 - self.mix) + echo * self.mix).astype(np.float32)


class Vibrato(Stage):
    """Pitch wobble via a modulated delay - useful for a shaky/creepy voice."""

    kind = "vibrato"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.rate_hz = 5.0
        self.depth_ms = 2.0
        self._line = DelayLine(int(sr * 0.15) + 4096)
        self._lfo = _Lfo(sr)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.rate_hz = clampf(float(params.get("rate_hz", 5.0)), 0.05, 20.0)
        self.depth_ms = clampf(float(params.get("depth_ms", 2.0)), 0.0, 15.0)

    def reset(self) -> None:
        self._line.reset()
        self._lfo.phase = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        depth = self.depth_ms * self.sr / 1000.0
        base = max(0.02 * self.sr, depth + 2.0)
        lfo = self._lfo.block(n, self.rate_hz, "sine")
        self._line.write(x.astype(np.float64))
        delays = np.maximum(base + depth * lfo, 1.0)
        return self._line.read_modulated(delays, head_offset=n).astype(np.float32)
