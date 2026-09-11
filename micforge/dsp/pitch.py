"""Phase-vocoder pitch shifting with independent formant control.

Why bother with a phase vocoder when a modulated delay line is ten lines of
code? Because *formants* are what actually make a voice read as male or female.
Naive pitch shifting moves the vocal tract resonances along with the pitch and
you get a chipmunk. Splitting the spectrum into a smooth envelope (the vocal
tract) and a flattened residual (the vocal folds) lets the two be moved
independently:

* pitch +5, formant 0   -> higher pitch, same body. Natural, still recognisably you.
* pitch +5, formant +5  -> the classic chipmunk (the envelope rides along).
* pitch +4, formant +3  -> convincingly feminine.
* pitch -5, formant -2  -> big villain voice.

Latency is one FFT frame (about 21 ms at 48 kHz with the default 1024-point
window), which is fine for voice chat.
"""
from __future__ import annotations

import numpy as np

from . import EPS, Stage, clampf

TWO_PI = 2.0 * np.pi


def _hann(n: int) -> np.ndarray:
    """Periodic Hann - the one that satisfies COLA for hop = n/4."""
    return (0.5 - 0.5 * np.cos(TWO_PI * np.arange(n) / n)).astype(np.float64)


def _wrap_phase(p: np.ndarray) -> np.ndarray:
    return p - TWO_PI * np.round(p / TWO_PI)


class _StftStream:
    """Overlap-add STFT front end: feed samples, get samples back.

    Subclasses implement :meth:`transform`, which rewrites one frame's spectrum.
    """

    def __init__(self, sr: int, fft_size: int = 1024, overlap: int = 4):
        self.sr = int(sr)
        self.n = int(fft_size)
        self.hop = max(1, self.n // int(overlap))
        self.win = _hann(self.n)
        # Sum of win^2 over all overlapping frames, for OLA normalisation.
        norm = np.sum(self.win ** 2) / self.hop
        self.win_norm = float(norm) if norm > 0 else 1.0
        self.nbins = self.n // 2 + 1
        self._in = np.zeros(0, dtype=np.float64)
        self._ola = np.zeros(self.n, dtype=np.float64)
        self._out = np.zeros(0, dtype=np.float64)
        self._primed = False
        self.reset()

    @property
    def latency(self) -> int:
        return self.n

    def reset(self) -> None:
        self._in = np.zeros(0, dtype=np.float64)
        self._ola = np.zeros(self.n, dtype=np.float64)
        # Pre-load the output with one frame of silence: that is the algorithmic
        # latency, and it keeps process() length-preserving from the first call.
        self._out = np.zeros(self.n, dtype=np.float64)
        self._primed = True
        self.reset_state()

    def reset_state(self) -> None:
        pass

    def transform(self, spec: np.ndarray) -> np.ndarray:  # pragma: no cover - abstract
        return spec

    def process(self, x: np.ndarray) -> np.ndarray:
        n_want = x.size
        if n_want == 0:
            return x
        self._in = np.concatenate((self._in, x.astype(np.float64)))

        produced = []
        while self._in.size >= self.n:
            frame = self._in[:self.n] * self.win
            spec = np.fft.rfft(frame)
            spec = self.transform(spec)
            out_frame = np.fft.irfft(spec, n=self.n) * self.win
            self._ola += out_frame
            produced.append(self._ola[:self.hop] / self.win_norm)
            self._ola = np.concatenate((self._ola[self.hop:],
                                        np.zeros(self.hop, dtype=np.float64)))
            self._in = self._in[self.hop:]

        if produced:
            self._out = np.concatenate([self._out] + produced)

        if self._out.size >= n_want:
            y = self._out[:n_want]
            self._out = self._out[n_want:]
        else:
            y = np.concatenate((self._out,
                                np.zeros(n_want - self._out.size, dtype=np.float64)))
            self._out = np.zeros(0, dtype=np.float64)
        return y.astype(np.float32)


class _PitchCore(_StftStream):
    def __init__(self, sr: int, fft_size: int = 1024, overlap: int = 4):
        self.ratio = 1.0
        self.formant_ratio = 1.0
        self.use_envelope = True
        self.lifter = 42
        super().__init__(sr, fft_size, overlap)
        k = np.arange(self.nbins, dtype=np.float64)
        self._expected = TWO_PI * self.hop * k / self.n
        self._bin_freq = TWO_PI * k / self.n

    def reset_state(self) -> None:
        nbins = getattr(self, "nbins", 1)
        self._last_phase = np.zeros(nbins, dtype=np.float64)
        self._sum_phase = np.zeros(nbins, dtype=np.float64)

    def _envelope(self, mag: np.ndarray) -> np.ndarray:
        """Cepstrally smoothed spectral envelope (the vocal tract shape)."""
        log_mag = np.log(mag + 1e-9)
        ceps = np.fft.irfft(log_mag, n=self.n)
        q = min(self.lifter, self.n // 2 - 1)
        lift = np.zeros(self.n, dtype=np.float64)
        lift[:q] = ceps[:q]
        lift[-q + 1:] = ceps[-q + 1:]
        env = np.exp(np.fft.rfft(lift, n=self.n).real)
        return np.maximum(env, 1e-9)

    def transform(self, spec: np.ndarray) -> np.ndarray:
        mag = np.abs(spec)
        phase = np.angle(spec)

        # --- analysis: recover each bin's true frequency from phase advance
        delta = phase - self._last_phase - self._expected
        self._last_phase = phase
        delta = _wrap_phase(delta)
        true_freq = self._bin_freq + delta / self.hop

        # --- split source from filter
        if self.use_envelope:
            env = self._envelope(mag)
            flat = mag / env
        else:
            env = None
            flat = mag

        # --- move the source spectrum by the pitch ratio
        r = self.ratio
        if abs(r - 1.0) < 1e-6:
            syn_flat = flat
            syn_freq = true_freq
        else:
            idx = np.arange(self.nbins, dtype=np.float64)
            target = np.rint(idx * r).astype(np.int64)
            keep = (target >= 0) & (target < self.nbins)
            syn_flat = np.zeros(self.nbins, dtype=np.float64)
            syn_freq = np.zeros(self.nbins, dtype=np.float64)
            np.add.at(syn_flat, target[keep], flat[keep])
            # Later (higher) bins overwrite earlier ones, which is what we want:
            # the loudest partial in a collision usually comes last.
            syn_freq[target[keep]] = true_freq[keep] * r

        # --- move the filter by the formant ratio, independently
        if env is not None:
            f = self.formant_ratio
            if abs(f - 1.0) < 1e-6:
                syn_env = env
            else:
                src = np.arange(self.nbins, dtype=np.float64)
                syn_env = np.interp(src / f, src, env, left=env[0], right=env[-1])
            syn_mag = syn_flat * syn_env
        else:
            syn_mag = syn_flat

        # --- synthesis: integrate the shifted frequencies into a phase
        self._sum_phase = self._sum_phase + syn_freq * self.hop
        self._sum_phase = _wrap_phase(self._sum_phase)
        return syn_mag * np.exp(1j * self._sum_phase)


class PitchShifter(Stage):
    """Config-facing pitch/formant stage with a dry/wet blend."""

    kind = "pitch"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.semitones = 0.0
        self.formant_semitones = 0.0
        self.mix = 1.0
        self.quality = "high"
        self._core = _PitchCore(sr, 1024, 4)
        self._dry = np.zeros(self._core.latency, dtype=np.float32)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self.semitones = clampf(float(params.get("semitones", 0.0)), -24.0, 24.0)
        self.formant_semitones = clampf(
            float(params.get("formant_semitones", 0.0)), -24.0, 24.0)
        self.mix = clampf(float(params.get("mix", 1.0)), 0.0, 1.0)
        quality = str(params.get("quality", "high"))
        if quality != self.quality:
            self.quality = quality
            self._rebuild()
        self._core.ratio = 2.0 ** (self.semitones / 12.0)
        self._core.formant_ratio = 2.0 ** (self.formant_semitones / 12.0)
        self._core.use_envelope = quality != "low"

    def _rebuild(self) -> None:
        size = {"low": 512, "high": 1024, "ultra": 2048}.get(self.quality, 1024)
        overlap = 4 if self.quality != "ultra" else 8
        self._core = _PitchCore(self.sr, size, overlap)
        self._core.ratio = 2.0 ** (self.semitones / 12.0)
        self._core.formant_ratio = 2.0 ** (self.formant_semitones / 12.0)
        self._core.use_envelope = self.quality != "low"
        self._dry = np.zeros(self._core.latency, dtype=np.float32)

    @property
    def latency(self) -> int:
        return self._core.latency

    def reset(self) -> None:
        self._core.reset()
        self._dry = np.zeros(self._core.latency, dtype=np.float32)

    def is_identity(self) -> bool:
        return abs(self.semitones) < 0.01 and abs(self.formant_semitones) < 0.01

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.is_identity() and self.mix >= 0.999:
            # Still push through the delay so toggling does not jump in time.
            return self._delay_dry(x)
        wet = self._core.process(x)
        if self.mix >= 0.999:
            self._delay_dry(x)
            return wet
        dry = self._delay_dry(x)
        return (dry * (1.0 - self.mix) + wet * self.mix).astype(np.float32)

    def _delay_dry(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self._dry, x.astype(np.float32)))
        n = x.size
        self._dry = buf[n:n + self._core.latency].copy()
        return buf[:n]


class Robotize(Stage):
    """Monotone robot voice: reset the synthesis phase every frame.

    Forcing every bin to zero phase at the frame rate makes all partials line up
    periodically, so the whole signal takes on the pitch of the frame rate --
    a flat, metallic monotone. Frame rate is derived from the requested pitch.
    """

    kind = "robot"

    def __init__(self, sr: int):
        super().__init__(sr)
        self.pitch_hz = 120.0
        self.mix = 1.0
        self._core: _RobotCore | None = None
        self._build()

    def _build(self) -> None:
        hop = int(round(self.sr / max(self.pitch_hz, 20.0)))
        hop = int(np.clip(hop, 32, 2048))
        size = 1
        while size < hop * 4:
            size *= 2
        size = min(size, 8192)
        self._core = _RobotCore(self.sr, size, max(2, size // hop))
        self._dry = np.zeros(self._core.latency, dtype=np.float32)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        pitch = clampf(float(params.get("pitch_hz", 120.0)), 30.0, 600.0)
        self.mix = clampf(float(params.get("mix", 1.0)), 0.0, 1.0)
        if abs(pitch - self.pitch_hz) > 0.5:
            self.pitch_hz = pitch
            self._build()

    @property
    def latency(self) -> int:
        return self._core.latency if self._core else 0

    def reset(self) -> None:
        if self._core:
            self._core.reset()
            self._dry = np.zeros(self._core.latency, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        assert self._core is not None
        wet = self._core.process(x)
        buf = np.concatenate((self._dry, x.astype(np.float32)))
        self._dry = buf[x.size:x.size + self._core.latency].copy()
        if self.mix >= 0.999:
            return wet
        dry = buf[:x.size]
        return (dry * (1.0 - self.mix) + wet * self.mix).astype(np.float32)


class _RobotCore(_StftStream):
    def transform(self, spec: np.ndarray) -> np.ndarray:
        return np.abs(spec).astype(np.complex128)


def semitones_to_ratio(semitones: float) -> float:
    return float(2.0 ** (semitones / 12.0))


def ratio_to_semitones(ratio: float) -> float:
    return float(12.0 * np.log2(max(ratio, EPS)))
