"""Signal processing blocks for the voice chain.

Everything in here follows the same contract:

* mono ``float32`` numpy arrays in and out, same length;
* state lives on the object, so consecutive blocks join seamlessly;
* :meth:`reset` clears that state;
* :meth:`set_params` is cheap and safe to call from the UI thread between blocks.
"""
from __future__ import annotations

import numpy as np

EPS = 1e-20


def db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def lin_to_db(lin: float) -> float:
    return float(20.0 * np.log10(max(abs(lin), EPS)))


def clampf(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


class Stage:
    """Base class for a chain stage."""

    kind = "stage"

    def __init__(self, samplerate: int):
        self.sr = int(samplerate)
        self.enabled = False

    def set_params(self, params: dict) -> None:  # pragma: no cover - trivial
        self.enabled = bool(params.get("enabled", False))

    def reset(self) -> None:  # pragma: no cover - trivial
        pass

    def process(self, x: np.ndarray) -> np.ndarray:
        return x

    @property
    def latency(self) -> int:
        """Algorithmic latency in frames, so the engine can report honest numbers."""
        return 0
