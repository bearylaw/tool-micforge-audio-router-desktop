"""RBJ cookbook biquads and a multi-band EQ built from them."""
from __future__ import annotations

import numpy as np

from . import Stage, clampf

try:  # scipy gives us a C-speed IIR; the fallback is correct but slower
    from scipy.signal import lfilter  # type: ignore

    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - exercised only without scipy
    _HAVE_SCIPY = False


def design(kind: str, sr: float, freq: float, q: float, gain_db: float = 0.0):
    """Return ``(b, a)`` normalised so ``a[0] == 1``."""
    freq = clampf(float(freq), 10.0, sr * 0.49)
    q = max(float(q), 0.05)
    a_amp = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * freq / sr
    cw = np.cos(w0)
    sw = np.sin(w0)
    alpha = sw / (2.0 * q)

    if kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "bandpass":
        b = [alpha, 0.0, -alpha]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "notch":
        b = [1.0, -2 * cw, 1.0]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peaking":
        b = [1 + alpha * a_amp, -2 * cw, 1 - alpha * a_amp]
        a = [1 + alpha / a_amp, -2 * cw, 1 - alpha / a_amp]
    elif kind == "lowshelf":
        sq = 2.0 * np.sqrt(a_amp) * alpha
        b = [a_amp * ((a_amp + 1) - (a_amp - 1) * cw + sq),
             2 * a_amp * ((a_amp - 1) - (a_amp + 1) * cw),
             a_amp * ((a_amp + 1) - (a_amp - 1) * cw - sq)]
        a = [(a_amp + 1) + (a_amp - 1) * cw + sq,
             -2 * ((a_amp - 1) + (a_amp + 1) * cw),
             (a_amp + 1) + (a_amp - 1) * cw - sq]
    elif kind == "highshelf":
        sq = 2.0 * np.sqrt(a_amp) * alpha
        b = [a_amp * ((a_amp + 1) + (a_amp - 1) * cw + sq),
             -2 * a_amp * ((a_amp - 1) + (a_amp + 1) * cw),
             a_amp * ((a_amp + 1) + (a_amp - 1) * cw - sq)]
        a = [(a_amp + 1) - (a_amp - 1) * cw + sq,
             2 * ((a_amp - 1) - (a_amp + 1) * cw),
             (a_amp + 1) - (a_amp - 1) * cw - sq]
    else:  # allpass / unknown -> passthrough
        b = [1.0, 0.0, 0.0]
        a = [1.0, 0.0, 0.0]

    a0 = a[0] if a[0] != 0 else 1.0
    b = np.asarray(b, dtype=np.float64) / a0
    a = np.asarray(a, dtype=np.float64) / a0
    return b, a


class Biquad:
    """One stateful second-order section."""

    def __init__(self, sr: float, kind: str = "peaking", freq: float = 1000.0,
                 q: float = 0.707, gain_db: float = 0.0):
        self.sr = float(sr)
        self._zi = np.zeros(2, dtype=np.float64)
        self.b = np.array([1.0, 0.0, 0.0])
        self.a = np.array([1.0, 0.0, 0.0])
        self.configure(kind, freq, q, gain_db)

    def configure(self, kind: str, freq: float, q: float, gain_db: float = 0.0) -> None:
        key = (kind, round(float(freq), 4), round(float(q), 4), round(float(gain_db), 4))
        if getattr(self, "_key", None) == key:
            return
        self._key = key
        self.b, self.a = design(kind, self.sr, freq, q, gain_db)

    def reset(self) -> None:
        self._zi[:] = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        if _HAVE_SCIPY:
            y, self._zi = lfilter(self.b, self.a, x.astype(np.float64), zi=self._zi)
            return np.asarray(y, dtype=np.float32)
        return self._process_py(x)

    def _process_py(self, x: np.ndarray) -> np.ndarray:  # pragma: no cover
        b0, b1, b2 = self.b
        a1, a2 = self.a[1], self.a[2]
        z1, z2 = self._zi
        out = np.empty_like(x, dtype=np.float32)
        for i, xv in enumerate(x):
            y = b0 * xv + z1
            z1 = b1 * xv - a1 * y + z2
            z2 = b2 * xv - a2 * y
            out[i] = y
        self._zi[0], self._zi[1] = z1, z2
        return out

    def response_db(self, freqs: np.ndarray) -> np.ndarray:
        """Magnitude response in dB, for drawing the EQ curve."""
        w = 2.0 * np.pi * np.asarray(freqs, dtype=np.float64) / self.sr
        z = np.exp(-1j * w)
        num = self.b[0] + self.b[1] * z + self.b[2] * z * z
        den = self.a[0] + self.a[1] * z + self.a[2] * z * z
        with np.errstate(divide="ignore", invalid="ignore"):
            h = np.abs(num / den)
        return 20.0 * np.log10(np.maximum(h, 1e-9))


class HighPass(Stage):
    kind = "highpass"
    _filter_kind = "highpass"

    def __init__(self, sr: int):
        super().__init__(sr)
        self._bq = Biquad(sr, self._filter_kind, 90.0, 0.707)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self._bq.configure(self._filter_kind,
                           float(params.get("freq", 90.0)),
                           float(params.get("q", 0.707)))

    def reset(self) -> None:
        self._bq.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        return self._bq.process(x)


class LowPass(HighPass):
    kind = "lowpass"
    _filter_kind = "lowpass"

    def __init__(self, sr: int):
        Stage.__init__(self, sr)
        self._bq = Biquad(sr, "lowpass", 12000.0, 0.707)

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        self._bq.configure("lowpass",
                           float(params.get("freq", 12000.0)),
                           float(params.get("q", 0.707)))


class Equalizer(Stage):
    """N cascaded bands. Bands come straight from the config dicts."""

    kind = "eq"

    def __init__(self, sr: int):
        super().__init__(sr)
        self._bands: list[Biquad] = []
        self._specs: list[dict] = []

    def set_params(self, params: dict) -> None:
        self.enabled = bool(params.get("enabled", False))
        specs = params.get("bands") or []
        if len(specs) != len(self._bands):
            self._bands = [Biquad(self.sr) for _ in specs]
        for bq, spec in zip(self._bands, specs, strict=False):
            bq.configure(str(spec.get("type", "peaking")),
                         float(spec.get("freq", 1000.0)),
                         float(spec.get("q", 1.0)),
                         float(spec.get("gain_db", 0.0)))
        self._specs = list(specs)

    def reset(self) -> None:
        for bq in self._bands:
            bq.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        for bq in self._bands:
            # Skip flat peaking bands entirely - most of the time most bands are flat.
            if bq._key[0] in ("peaking", "lowshelf", "highshelf") and abs(bq._key[3]) < 0.01:
                continue
            x = bq.process(x)
        return x

    def curve_db(self, freqs: np.ndarray) -> np.ndarray:
        total = np.zeros(len(freqs), dtype=np.float64)
        for bq in self._bands:
            total += bq.response_db(freqs)
        return total
