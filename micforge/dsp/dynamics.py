"""Gate, compressor and limiter.

Envelope followers are inherently sequential, so instead of looping over every
sample in Python these run their detector at *control rate*: the block is
chopped into short sub-blocks (default 8 frames = 0.17 ms at 48 kHz), one gain
value is computed per sub-block, and the resulting gain curve is interpolated
back up to sample rate. That is how most hardware compressors work anyway, it
is inaudible for attack times above a few hundred microseconds, and it turns a
48000-iteration-per-second Python loop into a 6000-iteration one.
"""
from __future__ import annotations

import numpy as np

from . import EPS, Stage, db_to_lin

CTRL_HOP = 8


def _subblock_peaks(x: np.ndarray, hop: int) -> np.ndarray:
    """Max |x| per sub-block, padding the tail."""
    n = x.size
    n_ctrl = (n + hop - 1) // hop
    pad = n_ctrl * hop - n
    if pad:
        x = np.concatenate((x, np.zeros(pad, dtype=x.dtype)))
    return np.max(np.abs(x.reshape(n_ctrl, hop)), axis=1)


def _subblock_rms(x: np.ndarray, hop: int) -> np.ndarray:
    n = x.size
    n_ctrl = (n + hop - 1) // hop
    pad = n_ctrl * hop - n
    if pad:
        x = np.concatenate((x, np.zeros(pad, dtype=x.dtype)))
    blocks = x.reshape(n_ctrl, hop).astype(np.float64)
    return np.sqrt(np.mean(blocks * blocks, axis=1))


def _interp_gain(gain_ctrl: np.ndarray, prev: float, n: int, hop: int) -> np.ndarray:
    """Turn one gain-per-sub-block into a smooth per-sample gain curve."""
    pts = np.empty(gain_ctrl.size + 1, dtype=np.float64)
    pts[0] = prev
    pts[1:] = gain_ctrl
    pos = np.arange(n, dtype=np.float64) / hop
    return np.interp(pos, np.arange(pts.size, dtype=np.float64), pts)


def _coef(time_ms: float, sr: float, hop: int) -> float:
    """One-pole coefficient for a time constant expressed in ms."""
    t = max(float(time_ms), 0.05) * 0.001
    ctrl_sr = sr / hop
    return float(np.exp(-1.0 / max(t * ctrl_sr, 1e-6)))


class NoiseGate(Stage):
    """Downward expander with hysteresis and a hold time.

    Kills keyboard clatter and fan noise between words, which matters a lot once
    you turn the mic gain up.
    """

    kind = "gate"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.threshold_db = -45.0
        self.attack_ms = 2.0
        self.hold_ms = 80.0
        self.release_ms = 150.0
        self.range_db = -60.0
        self._gain = 0.0
        self._open = False
        self._hold_left = 0.0
        self.gain_reduction_db = 0.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.threshold_db = float(params.get("threshold_db", -45.0))
        self.attack_ms = float(params.get("attack_ms", 2.0))
        self.hold_ms = float(params.get("hold_ms", 80.0))
        self.release_ms = float(params.get("release_ms", 150.0))
        self.range_db = float(params.get("range_db", -60.0))

    def reset(self) -> None:
        self._gain = 0.0
        self._open = False
        self._hold_left = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        if n == 0:
            return x
        hop = CTRL_HOP
        peaks = _subblock_peaks(x, hop)
        thr_open = db_to_lin(self.threshold_db)
        thr_close = db_to_lin(self.threshold_db - 6.0)  # 6 dB of hysteresis
        floor = db_to_lin(self.range_db)
        a_att = _coef(self.attack_ms, self.sr, hop)
        a_rel = _coef(self.release_ms, self.sr, hop)
        hold_blocks = self.hold_ms * 0.001 * self.sr / hop

        g = self._gain
        prev_gain = g  # where the last block left off, so the ramp stays continuous
        out = np.empty(peaks.size, dtype=np.float64)
        for i in range(peaks.size):
            p = peaks[i]
            if p > thr_open:
                self._open = True
                self._hold_left = hold_blocks
            elif self._open and p < thr_close:
                if self._hold_left > 0:
                    self._hold_left -= 1.0
                else:
                    self._open = False
            target = 1.0 if self._open else floor
            coef = a_att if target > g else a_rel
            g = target + (g - target) * coef
            out[i] = g
        self._gain = g
        self.gain_reduction_db = 20.0 * np.log10(max(g, EPS))

        curve = _interp_gain(out, prev_gain, n, hop)
        return (x * curve).astype(np.float32)


class Compressor(Stage):
    """Feed-forward compressor with a soft knee and RMS detection."""

    kind = "compressor"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.threshold_db = -18.0
        self.ratio = 3.0
        self.attack_ms = 8.0
        self.release_ms = 120.0
        self.knee_db = 6.0
        self.makeup_db = 3.0
        self._env = 0.0
        self._gain = 1.0
        self.gain_reduction_db = 0.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.threshold_db = float(params.get("threshold_db", -18.0))
        self.ratio = max(float(params.get("ratio", 3.0)), 1.0)
        self.attack_ms = float(params.get("attack_ms", 8.0))
        self.release_ms = float(params.get("release_ms", 120.0))
        self.knee_db = max(float(params.get("knee_db", 6.0)), 0.0)
        self.makeup_db = float(params.get("makeup_db", 0.0))

    def reset(self) -> None:
        self._env = 0.0
        self._gain = 1.0

    def _curve_db(self, level_db: np.ndarray) -> np.ndarray:
        """Static input->output characteristic, dB in, dB out."""
        thr = self.threshold_db
        knee = self.knee_db
        over = level_db - thr
        out = np.copy(level_db)
        if knee > 0:
            lower = over < -knee / 2
            upper = over > knee / 2
            mid = ~lower & ~upper
            out[upper] = thr + over[upper] / self.ratio
            if np.any(mid):
                d = over[mid] + knee / 2
                out[mid] = level_db[mid] + (1.0 / self.ratio - 1.0) * d * d / (2 * knee)
        else:
            above = over > 0
            out[above] = thr + over[above] / self.ratio
        return out

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        if n == 0:
            return x
        hop = CTRL_HOP
        det = _subblock_rms(x, hop)
        a_att = _coef(self.attack_ms, self.sr, hop)
        a_rel = _coef(self.release_ms, self.sr, hop)

        env = np.empty(det.size, dtype=np.float64)
        e = self._env
        for i in range(det.size):
            d = det[i]
            coef = a_att if d > e else a_rel
            e = d + (e - d) * coef
            env[i] = e
        self._env = e

        level_db = 20.0 * np.log10(np.maximum(env, EPS))
        target_db = self._curve_db(level_db)
        reduction_db = target_db - level_db
        gain_ctrl = 10.0 ** ((reduction_db + self.makeup_db) / 20.0)
        self.gain_reduction_db = float(np.min(reduction_db)) if reduction_db.size else 0.0

        curve = _interp_gain(gain_ctrl, self._gain, n, hop)
        self._gain = float(gain_ctrl[-1]) if gain_ctrl.size else self._gain
        return (x * curve).astype(np.float32)


class Limiter(Stage):
    """Brickwall safety limiter with one sub-block of lookahead.

    This is the last thing before the virtual cable. Clipping into Discord
    sounds far worse than a few dB of gain reduction, and the whole point of the
    app is that people crank the gain.
    """

    kind = "limiter"

    def __init__(self, sr: int, lookahead_frames: int = CTRL_HOP * 4):
        super().__init__(sr)
        self.enabled = True
        self.ceiling_db = -1.0
        self.release_ms = 120.0
        self.lookahead = int(lookahead_frames)
        self._delay = np.zeros(self.lookahead, dtype=np.float32)
        self._gain = 1.0
        self.gain_reduction_db = 0.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", True))
        self.ceiling_db = float(params.get("ceiling_db", -1.0))
        self.release_ms = float(params.get("release_ms", 120.0))

    def reset(self) -> None:
        self._delay[:] = 0.0
        self._gain = 1.0

    @property
    def latency(self) -> int:
        return self.lookahead

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        if n == 0:
            return x
        hop = CTRL_HOP
        ceiling = db_to_lin(self.ceiling_db)
        a_rel = _coef(self.release_ms, self.sr, hop)

        # Detector looks at the *undelayed* signal, so the gain is already down
        # by the time the peak reaches the output.
        peaks = _subblock_peaks(x, hop)
        need = np.minimum(1.0, ceiling / np.maximum(peaks, EPS))

        gain_ctrl = np.empty(need.size, dtype=np.float64)
        g = self._gain
        prev_gain = g
        for i in range(need.size):
            t = need[i]
            g = t if t < g else t + (g - t) * a_rel
            gain_ctrl[i] = g
        self._gain = g
        self.gain_reduction_db = 20.0 * np.log10(max(float(np.min(gain_ctrl)), EPS))

        delayed = np.concatenate((self._delay, x))
        self._delay = delayed[n:n + self.lookahead].copy()
        y = delayed[:n]

        curve = _interp_gain(gain_ctrl, prev_gain, n, hop)
        out = y * curve
        # Hard safety net for anything that slipped between sub-blocks.
        np.clip(out, -ceiling, ceiling, out=out)
        return out.astype(np.float32)


class Gain(Stage):
    """Plain make-up gain with a short ramp so slider moves do not click."""

    kind = "makeup"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.enabled = True
        self.gain_db = 0.0
        self._current = 1.0

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", True))
        self.gain_db = float(params.get("gain_db", 0.0))

    def reset(self) -> None:
        self._current = db_to_lin(self.gain_db)

    def process(self, x: np.ndarray) -> np.ndarray:
        target = db_to_lin(self.gain_db)
        if abs(target - self._current) < 1e-6:
            self._current = target
            return (x * target).astype(np.float32) if target != 1.0 else x
        ramp = np.linspace(self._current, target, x.size, dtype=np.float32)
        self._current = target
        return (x * ramp).astype(np.float32)
