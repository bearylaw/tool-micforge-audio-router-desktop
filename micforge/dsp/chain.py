"""The voice chain: an ordered, reorderable stack of stages.

``STAGE_META`` doubles as UI metadata -- the Voice tab builds its controls from
it, so adding a new parameter to an effect means adding one line here rather
than hand-wiring another slider.
"""
from __future__ import annotations

import threading

import numpy as np

from . import Stage, clampf
from .biquad import Equalizer, HighPass, LowPass
from .dynamics import Compressor, Gain, Limiter, NoiseGate
from .effects import BitCrush, Chorus, Delay, Distortion, RingMod, Tremolo, Vibrato
from .pitch import PitchShifter, Robotize
from .reverb import Reverb

STAGE_FACTORIES = {
    "gate": NoiseGate,
    "highpass": HighPass,
    "lowpass": LowPass,
    "eq": Equalizer,
    "pitch": PitchShifter,
    "robot": Robotize,
    "ringmod": RingMod,
    "bitcrush": BitCrush,
    "distortion": Distortion,
    "chorus": Chorus,
    "vibrato": Vibrato,
    "tremolo": Tremolo,
    "delay": Delay,
    "reverb": Reverb,
    "compressor": Compressor,
    "makeup": Gain,
}

# kind -> (label, blurb, [(param, label, widget, min, max, step, suffix)])
# widget is one of: db, float, int, pct, choice:<a|b|c>, bands
STAGE_META: dict[str, tuple[str, str, list]] = {
    "gate": ("Noise gate", "Mutes the mic between words. Turn this on before you crank the gain.", [
        ("threshold_db", "Threshold", "db", -90, 0, 0.5, "dB"),
        ("attack_ms", "Attack", "float", 0.1, 50, 0.1, "ms"),
        ("hold_ms", "Hold", "float", 0, 1000, 5, "ms"),
        ("release_ms", "Release", "float", 5, 2000, 5, "ms"),
        ("range_db", "Closed level", "db", -90, 0, 1, "dB"),
    ]),
    "highpass": ("High-pass", "Removes rumble, desk thumps and plosives.", [
        ("freq", "Frequency", "float", 20, 800, 1, "Hz"),
        ("q", "Resonance", "float", 0.3, 4.0, 0.01, ""),
    ]),
    "lowpass": ("Low-pass", "Takes the top off. Pair with a high-pass for a radio sound.", [
        ("freq", "Frequency", "float", 500, 20000, 10, "Hz"),
        ("q", "Resonance", "float", 0.3, 4.0, 0.01, ""),
    ]),
    "eq": ("Equaliser", "Five bands. Cut around 300 Hz for clarity, lift 3 kHz for presence.", [
        ("bands", "Bands", "bands", 0, 0, 0, ""),
    ]),
    "pitch": ("Pitch & formant", "Pitch moves the note; formant moves the body of the voice. "
                                 "Move both together for a chipmunk, pitch alone to stay natural.", [
        ("semitones", "Pitch", "float", -24, 24, 0.1, "st"),
        ("formant_semitones", "Formant", "float", -24, 24, 0.1, "st"),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
        ("quality", "Quality", "choice:low|high|ultra", 0, 0, 0, ""),
    ]),
    "robot": ("Robot", "Flattens the pitch to a monotone by resetting phase every frame.", [
        ("pitch_hz", "Robot pitch", "float", 30, 600, 1, "Hz"),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
    ]),
    "ringmod": ("Ring modulator", "Classic Dalek. Low rates buzz, high rates go metallic.", [
        ("freq", "Carrier", "float", 0.5, 4000, 0.5, "Hz"),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
        ("waveform", "Shape", "choice:sine|triangle|square|saw", 0, 0, 0, ""),
    ]),
    "bitcrush": ("Bitcrusher", "Quantise and decimate for a lo-fi, broken-radio texture.", [
        ("bits", "Bit depth", "float", 1, 16, 0.5, "bit"),
        ("downsample", "Decimate", "float", 1, 32, 1, "x"),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
    ]),
    "distortion": ("Distortion", "Saturation, from warm to destroyed.", [
        ("drive_db", "Drive", "db", 0, 48, 0.5, "dB"),
        ("tone", "Tone", "pct", 0, 1, 0.01, ""),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
        ("kind", "Shape", "choice:tanh|hard|fold|fuzz|sine", 0, 0, 0, ""),
    ]),
    "chorus": ("Chorus", "Detuned copies. Two or three voices at once, or a demon chorus.", [
        ("rate_hz", "Rate", "float", 0.05, 10, 0.05, "Hz"),
        ("depth_ms", "Depth", "float", 0.1, 25, 0.1, "ms"),
        ("voices", "Voices", "int", 1, 4, 1, ""),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
        ("spread", "Spread", "pct", 0, 1, 0.01, ""),
    ]),
    "vibrato": ("Vibrato", "Pitch wobble. Slow and deep sounds seasick.", [
        ("rate_hz", "Rate", "float", 0.05, 20, 0.05, "Hz"),
        ("depth_ms", "Depth", "float", 0, 15, 0.1, "ms"),
    ]),
    "tremolo": ("Tremolo", "Volume wobble. Above ~15 Hz it becomes a stutter.", [
        ("rate_hz", "Rate", "float", 0.05, 40, 0.05, "Hz"),
        ("depth", "Depth", "pct", 0, 1, 0.01, ""),
        ("waveform", "Shape", "choice:sine|triangle|square|saw|random", 0, 0, 0, ""),
    ]),
    "delay": ("Delay", "Echo. Short times with high feedback make a metal tube.", [
        ("time_ms", "Time", "float", 5, 2000, 5, "ms"),
        ("feedback", "Feedback", "pct", 0, 0.95, 0.01, ""),
        ("damping", "Damping", "pct", 0, 0.95, 0.01, ""),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
    ]),
    "reverb": ("Reverb", "Room. Large and wet sounds like a cave or a stadium PA.", [
        ("size", "Size", "pct", 0, 1, 0.01, ""),
        ("damping", "Damping", "pct", 0, 1, 0.01, ""),
        ("width", "Width", "pct", 0, 1, 0.01, ""),
        ("predelay_ms", "Pre-delay", "float", 0, 200, 1, "ms"),
        ("mix", "Mix", "pct", 0, 1, 0.01, ""),
    ]),
    "compressor": ("Compressor", "Evens out your level so quiet speech still carries.", [
        ("threshold_db", "Threshold", "db", -60, 0, 0.5, "dB"),
        ("ratio", "Ratio", "float", 1, 20, 0.1, ":1"),
        ("attack_ms", "Attack", "float", 0.2, 100, 0.2, "ms"),
        ("release_ms", "Release", "float", 10, 1000, 5, "ms"),
        ("knee_db", "Knee", "db", 0, 24, 0.5, "dB"),
        ("makeup_db", "Make-up", "db", -12, 24, 0.5, "dB"),
    ]),
    "makeup": ("Output gain", "Final trim on the voice chain.", [
        ("gain_db", "Gain", "db", -24, 36, 0.5, "dB"),
    ]),
}

ALL_KINDS = list(STAGE_FACTORIES)


class VoiceChain:
    """Ordered stack of stages with a latency-compensated global dry/wet."""

    def __init__(self, samplerate: int):
        self.sr = int(samplerate)
        self._lock = threading.Lock()
        self._stages: list[tuple[str, Stage]] = []
        self._order: list[str] = []
        self._enabled = True
        self._dry_wet = 1.0
        self._dry_delay = np.zeros(0, dtype=np.float32)
        self._latency = 0
        self.bypassed = False

    # ------------------------------------------------------------- assembly
    def configure(self, voice, fx_lookup) -> None:
        """Rebuild from a ``VoiceSettings``-shaped object.

        ``fx_lookup(kind) -> dict`` supplies the parameter dict for one stage
        (``Config.fx`` does exactly this, filling in missing keys).
        """
        order = [k for k in (voice.order or []) if k in STAGE_FACTORIES]
        for k in ALL_KINDS:
            if k not in order:
                order.append(k)

        with self._lock:
            existing = {k: s for k, s in self._stages}
            stages: list[tuple[str, Stage]] = []
            for kind in order:
                stage = existing.get(kind)
                if stage is None:
                    stage = STAGE_FACTORIES[kind](self.sr)
                try:
                    stage.set_params(fx_lookup(kind))
                except Exception:
                    pass
                stages.append((kind, stage))
            self._stages = stages
            self._order = order
            self._enabled = bool(voice.enabled)
            self._dry_wet = clampf(float(getattr(voice, "dry_wet", 1.0)), 0.0, 1.0)
            self._latency = sum(s.latency for _, s in stages if s.enabled)
            if self._dry_delay.size != self._latency:
                self._dry_delay = np.zeros(self._latency, dtype=np.float32)

    def reset(self) -> None:
        with self._lock:
            for _, s in self._stages:
                try:
                    s.reset()
                except Exception:
                    pass
            self._dry_delay = np.zeros(self._latency, dtype=np.float32)

    @property
    def latency(self) -> int:
        return self._latency

    @property
    def order(self) -> list[str]:
        return list(self._order)

    def stage(self, kind: str) -> Stage | None:
        for k, s in self._stages:
            if k == kind:
                return s
        return None

    def active_kinds(self) -> list[str]:
        with self._lock:
            return [k for k, s in self._stages if s.enabled]

    # -------------------------------------------------------------- process
    def process(self, x: np.ndarray) -> np.ndarray:
        if self.bypassed:
            return x
        with self._lock:
            if not self._enabled or not self._stages:
                return x
            dry = None
            if self._dry_wet < 0.999 and self._latency:
                buf = np.concatenate((self._dry_delay, x))
                self._dry_delay = buf[x.size:x.size + self._latency].copy()
                dry = buf[:x.size]
            elif self._dry_wet < 0.999:
                dry = x.copy()

            y = x
            for _kind, stage in self._stages:
                if not stage.enabled:
                    continue
                try:
                    y = stage.process(y)
                except Exception:
                    # A broken stage must never take the whole audio thread down.
                    continue
            if dry is not None:
                w = self._dry_wet
                y = (dry * (1.0 - w) + y * w).astype(np.float32)
            return y

    # --------------------------------------------------------------- meters
    def meters(self) -> dict:
        out = {}
        for kind, stage in self._stages:
            gr = getattr(stage, "gain_reduction_db", None)
            if gr is not None and stage.enabled:
                out[kind] = float(gr)
        return out


def make_limiter(samplerate: int) -> Limiter:
    return Limiter(samplerate)
