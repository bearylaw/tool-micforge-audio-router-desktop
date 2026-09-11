"""Lock-protected ring buffer and a streaming resampler.

Every source (mic, loopback capture, soundboard) runs on its own clock. Each
one writes into a :class:`RingBuffer`; the output callback pulls from them.
Because the clocks drift, the reader also has to cope with the buffer slowly
filling up or running dry -- :meth:`RingBuffer.read_drift_corrected` does that
by dropping or repeating a handful of frames once the fill level strays too far
from target.
"""
from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    """Single-producer / single-consumer float32 ring.

    Overflow drops the *oldest* audio (we would rather hear the present than a
    backlog), underflow pads with silence.
    """

    def __init__(self, capacity_frames: int, channels: int = 1):
        self.capacity = int(max(capacity_frames, 64))
        self.channels = int(max(channels, 1))
        self._buf = np.zeros((self.capacity, self.channels), dtype=np.float32)
        self._w = 0
        self._r = 0
        self._filled = 0
        self._lock = threading.Lock()
        self.overflows = 0
        self.underflows = 0

    # ------------------------------------------------------------------ info
    def __len__(self) -> int:
        with self._lock:
            return self._filled

    @property
    def available(self) -> int:
        with self._lock:
            return self._filled

    @property
    def free(self) -> int:
        with self._lock:
            return self.capacity - self._filled

    def clear(self) -> None:
        with self._lock:
            self._w = self._r = self._filled = 0
            self._buf[:] = 0.0

    def reset_stats(self) -> None:
        self.overflows = 0
        self.underflows = 0

    # ----------------------------------------------------------------- write
    def write(self, data: np.ndarray) -> int:
        """Append frames. ``data`` is (frames,) or (frames, channels)."""
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.shape[1] != self.channels:
            data = _fit_channels(data, self.channels)
        if data.dtype != np.float32:
            data = data.astype(np.float32, copy=False)

        n = data.shape[0]
        if n == 0:
            return 0
        if n > self.capacity:
            data = data[-self.capacity:]
            n = self.capacity

        with self._lock:
            overflow = max(0, self._filled + n - self.capacity)
            if overflow:
                self.overflows += 1
                self._r = (self._r + overflow) % self.capacity
                self._filled -= overflow

            first = min(n, self.capacity - self._w)
            self._buf[self._w:self._w + first] = data[:first]
            rest = n - first
            if rest:
                self._buf[:rest] = data[first:]
            self._w = (self._w + n) % self.capacity
            self._filled += n
        return n

    # ------------------------------------------------------------------ read
    def read(self, frames: int, out: np.ndarray | None = None) -> np.ndarray:
        """Pop ``frames``; missing frames come back as silence."""
        frames = int(frames)
        if out is None:
            out = np.zeros((frames, self.channels), dtype=np.float32)
        else:
            out[:] = 0.0

        with self._lock:
            n = min(frames, self._filled)
            if n < frames:
                self.underflows += 1
            if n:
                first = min(n, self.capacity - self._r)
                out[:first] = self._buf[self._r:self._r + first]
                rest = n - first
                if rest:
                    out[first:n] = self._buf[:rest]
                self._r = (self._r + n) % self.capacity
                self._filled -= n
        return out

    def peek_level(self) -> float:
        """Peak of the most recent ~20 ms, for meters, without consuming."""
        with self._lock:
            if self._filled == 0:
                return 0.0
            n = min(self._filled, 1024)
            start = (self._w - n) % self.capacity
            if start + n <= self.capacity:
                chunk = self._buf[start:start + n]
            else:
                head = self.capacity - start
                chunk = np.concatenate((self._buf[start:], self._buf[:n - head]))
        return float(np.max(np.abs(chunk))) if chunk.size else 0.0

    def discard(self, frames: int) -> None:
        with self._lock:
            n = min(int(frames), self._filled)
            self._r = (self._r + n) % self.capacity
            self._filled -= n

    # ----------------------------------------------------- drift correction
    def read_drift_corrected(self, frames: int, target_fill: int,
                             max_correction: int = 8) -> np.ndarray:
        """Read ``frames`` while nudging the fill level toward ``target_fill``.

        Sources clocked by a different crystal than the output device drift by
        a few hundred ppm. Left alone the buffer eventually overflows or starves
        and you get a click every few minutes. Correcting a handful of frames at
        a time is inaudible on speech and game audio.
        """
        fill = self.available
        if fill > target_fill * 2 and fill > frames * 2:
            self.discard(min(max_correction, fill - target_fill))
        return self.read(frames)


def _fit_channels(data: np.ndarray, channels: int) -> np.ndarray:
    """Up/down-mix so ``data`` has exactly ``channels`` columns."""
    have = data.shape[1]
    if have == channels:
        return data
    if have > channels:
        if channels == 1:
            return data.mean(axis=1, keepdims=True).astype(np.float32)
        return data[:, :channels]
    if have == 1:
        return np.repeat(data, channels, axis=1)
    pad = np.zeros((data.shape[0], channels - have), dtype=np.float32)
    return np.concatenate((data, pad), axis=1)


def to_mono(data: np.ndarray, mode: str = "downmix") -> np.ndarray:
    """(frames, channels) -> (frames,) float32."""
    if data.ndim == 1:
        return data.astype(np.float32, copy=False)
    if data.shape[1] == 1:
        return data[:, 0].astype(np.float32, copy=False)
    if mode == "left":
        return data[:, 0].astype(np.float32, copy=False)
    if mode == "right":
        return data[:, 1].astype(np.float32, copy=False)
    return data.mean(axis=1).astype(np.float32)


class StreamResampler:
    """Linear-interpolating resampler that keeps phase across calls.

    Not the prettiest filter in the world, but it is cheap, allocation-light and
    runs inside a capture callback without drama. Sources are usually already at
    the engine rate, in which case this is a no-op passthrough.
    """

    def __init__(self, src_rate: float, dst_rate: float, channels: int = 1):
        self.src_rate = float(src_rate)
        self.dst_rate = float(dst_rate)
        self.channels = int(channels)
        self.ratio = self.src_rate / self.dst_rate
        self._pos = 0.0
        self._tail = np.zeros((1, self.channels), dtype=np.float32)
        self._has_tail = False

    @property
    def passthrough(self) -> bool:
        return abs(self.src_rate - self.dst_rate) < 0.5

    def reset(self) -> None:
        self._pos = 0.0
        self._has_tail = False
        self._tail[:] = 0.0

    def process(self, data: np.ndarray) -> np.ndarray:
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if self.passthrough:
            return data.astype(np.float32, copy=False)
        if data.shape[0] == 0:
            return np.zeros((0, data.shape[1]), dtype=np.float32)

        if self._has_tail:
            src = np.concatenate((self._tail, data)).astype(np.float32, copy=False)
        else:
            src = data.astype(np.float32, copy=False)
            self._pos = 0.0

        n_src = src.shape[0]
        # Output positions available before we run past the last full sample.
        max_out = int(np.floor((n_src - 1 - self._pos) / self.ratio)) + 1
        if max_out <= 0:
            self._tail = src[-1:].copy()
            self._has_tail = True
            return np.zeros((0, src.shape[1]), dtype=np.float32)

        idx = self._pos + self.ratio * np.arange(max_out, dtype=np.float64)
        i0 = np.floor(idx).astype(np.int64)
        frac = (idx - i0).astype(np.float32)[:, None]
        i1 = np.minimum(i0 + 1, n_src - 1)
        out = src[i0] * (1.0 - frac) + src[i1] * frac

        consumed = float(idx[-1] + self.ratio)
        keep = int(np.floor(consumed))
        self._pos = consumed - keep
        if keep >= n_src:
            self._tail = src[-1:].copy()
            self._pos = max(0.0, consumed - (n_src - 1))
        else:
            self._tail = src[keep:keep + 1].copy()
        self._has_tail = True
        return out.astype(np.float32, copy=False)


def resample_offline(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """High quality one-shot resample, used when decoding soundboard files."""
    if src_rate == dst_rate or data.size == 0:
        return data.astype(np.float32, copy=False)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    try:
        from math import gcd

        from scipy.signal import resample_poly  # type: ignore

        g = gcd(int(src_rate), int(dst_rate))
        up, down = int(dst_rate // g), int(src_rate // g)
        return resample_poly(data, up, down, axis=0).astype(np.float32)
    except Exception:
        n_out = int(round(data.shape[0] * dst_rate / src_rate))
        idx = np.linspace(0, data.shape[0] - 1, n_out)
        i0 = np.floor(idx).astype(np.int64)
        i1 = np.minimum(i0 + 1, data.shape[0] - 1)
        frac = (idx - i0).astype(np.float32)[:, None]
        return (data[i0] * (1 - frac) + data[i1] * frac).astype(np.float32)
