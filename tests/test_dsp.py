"""DSP correctness and stability checks.

These run without any audio hardware, which is the point: the whole signal path
can be verified offline before it is ever pointed at a real device.
"""
from __future__ import annotations

import numpy as np
import pytest

from micforge import config
from micforge.dsp import chain as chain_mod
from micforge.dsp import presets
from micforge.dsp.biquad import Biquad, Equalizer
from micforge.dsp.dynamics import Compressor, Gain, Limiter, NoiseGate
from micforge.dsp.pitch import PitchShifter
from micforge.dsp.reverb import Reverb

SR = 48000
BLOCK = 240


def sine(freq: float, n: int, sr: int = SR, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def blocks(x: np.ndarray, n: int = BLOCK):
    for i in range(0, len(x) - n + 1, n):
        yield x[i:i + n]


def run_blocks(stage, x: np.ndarray, n: int = BLOCK) -> np.ndarray:
    return np.concatenate([stage.process(b) for b in blocks(x, n)])


def dominant_freq(x: np.ndarray, sr: int = SR) -> float:
    """Peak of the magnitude spectrum, ignoring DC."""
    win = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * win))
    spec[: max(2, int(20 * len(x) / sr))] = 0.0
    return float(np.argmax(spec) * sr / len(x))


# ------------------------------------------------------------------ stages
@pytest.mark.parametrize("kind", list(chain_mod.STAGE_FACTORIES))
def test_every_stage_is_length_preserving_and_finite(kind):
    """A stage that changes block length or emits NaN would corrupt the stream."""
    stage = chain_mod.STAGE_FACTORIES[kind](SR)
    params = config._fx_defaults().get(kind, {})
    params = dict(params)
    params["enabled"] = True
    stage.set_params(params)

    x = sine(220.0, SR // 4) + 0.05 * np.random.default_rng(1).standard_normal(SR // 4).astype(np.float32)
    out = run_blocks(stage, x.astype(np.float32))

    assert out.shape == x[: len(out)].shape
    assert len(out) == (len(x) // BLOCK) * BLOCK
    assert np.all(np.isfinite(out)), f"{kind} produced non-finite samples"
    assert np.max(np.abs(out)) < 20.0, f"{kind} blew up"


@pytest.mark.parametrize("kind", list(chain_mod.STAGE_FACTORIES))
def test_block_size_does_not_change_output(kind):
    """Same audio, different block sizes -> the same result.

    This is the property that breaks first when a stage keeps state wrong, and
    it is exactly what happens in the field when someone changes the buffer
    size in settings.
    """
    if kind in ("bitcrush",):
        pytest.skip("sample-and-hold phase is intentionally block-anchored")
    params = dict(config._fx_defaults().get(kind, {}))
    params["enabled"] = True
    x = sine(330.0, 48000)

    a = chain_mod.STAGE_FACTORIES[kind](SR)
    a.set_params(params)
    out_a = run_blocks(a, x, 240)

    b = chain_mod.STAGE_FACTORIES[kind](SR)
    b.set_params(params)
    out_b = run_blocks(b, x, 480)

    n = min(len(out_a), len(out_b))
    assert np.allclose(out_a[:n], out_b[:n], atol=2e-3), f"{kind} is block-size dependent"


# ------------------------------------------------------------------- pitch
@pytest.mark.parametrize("semitones,expected_ratio", [(12.0, 2.0), (7.0, 1.4983), (-12.0, 0.5)])
def test_pitch_shifter_moves_pitch_by_the_right_ratio(semitones, expected_ratio):
    ps = PitchShifter(SR)
    ps.set_params({"enabled": True, "semitones": semitones,
                   "formant_semitones": 0.0, "mix": 1.0, "quality": "high"})
    x = sine(440.0, SR)
    out = run_blocks(ps, x)
    # Skip the first frames: the vocoder needs to fill before its output is real.
    tail = out[SR // 4:]
    got = dominant_freq(tail)
    assert got == pytest.approx(440.0 * expected_ratio, rel=0.04), (
        f"{semitones} st -> {got:.1f} Hz, wanted {440.0 * expected_ratio:.1f} Hz")


def test_pitch_shifter_is_transparent_at_zero():
    ps = PitchShifter(SR)
    ps.set_params({"enabled": True, "semitones": 0.0, "formant_semitones": 0.0,
                   "mix": 1.0, "quality": "high"})
    x = sine(440.0, SR // 2)
    out = run_blocks(ps, x)
    tail = out[SR // 8:]
    assert dominant_freq(tail) == pytest.approx(440.0, rel=0.02)


def test_formant_shift_moves_the_envelope_not_the_pitch():
    """The whole point of the formant control: pitch stays, timbre changes."""
    ps = PitchShifter(SR)
    ps.set_params({"enabled": True, "semitones": 0.0, "formant_semitones": 7.0,
                   "mix": 1.0, "quality": "high"})
    # A buzzy source has a clear fundamental plus many harmonics to reshape.
    t = np.arange(SR, dtype=np.float64) / SR
    x = np.zeros(SR)
    for h in range(1, 25):
        x += np.sin(2 * np.pi * 150.0 * h * t) / h
    x = (0.3 * x / np.max(np.abs(x))).astype(np.float32)

    out = run_blocks(ps, x)[SR // 4:]
    assert dominant_freq(out) == pytest.approx(150.0, rel=0.06)

    def centroid(sig):
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        freqs = np.fft.rfftfreq(len(sig), 1 / SR)
        return float(np.sum(spec * freqs) / max(np.sum(spec), 1e-9))

    assert centroid(out) > centroid(x[SR // 4:]) * 1.1


# ---------------------------------------------------------------- dynamics
def test_limiter_never_exceeds_its_ceiling():
    lim = Limiter(SR)
    lim.set_params({"enabled": True, "ceiling_db": -1.0, "release_ms": 100.0})
    loud = (sine(200.0, SR, amp=4.0)).astype(np.float32)
    out = run_blocks(lim, loud)
    ceiling = 10 ** (-1.0 / 20.0)
    assert np.max(np.abs(out)) <= ceiling + 1e-6


def test_limiter_leaves_quiet_audio_alone():
    lim = Limiter(SR)
    lim.set_params({"enabled": True, "ceiling_db": -1.0, "release_ms": 100.0})
    quiet = sine(200.0, SR, amp=0.1)
    out = run_blocks(lim, quiet)
    assert np.max(np.abs(out)) == pytest.approx(0.1, rel=0.05)


def test_compressor_reduces_dynamic_range():
    comp = Compressor(SR)
    comp.set_params({"enabled": True, "threshold_db": -20.0, "ratio": 4.0,
                     "attack_ms": 5.0, "release_ms": 100.0, "knee_db": 6.0,
                     "makeup_db": 0.0})
    loud = sine(300.0, SR // 2, amp=0.8)
    out = run_blocks(comp, loud)
    steady = out[SR // 8:]
    assert np.max(np.abs(steady)) < 0.8 * 0.85


def test_noise_gate_closes_on_silence_and_opens_on_speech():
    gate = NoiseGate(SR)
    gate.set_params({"enabled": True, "threshold_db": -40.0, "attack_ms": 1.0,
                     "hold_ms": 20.0, "release_ms": 50.0, "range_db": -60.0})
    quiet = (sine(300.0, SR // 2, amp=0.0005)).astype(np.float32)
    loud = sine(300.0, SR // 2, amp=0.4)
    out = run_blocks(gate, np.concatenate([quiet, loud]))
    first = out[SR // 8: SR // 4]
    last = out[-SR // 8:]
    assert np.max(np.abs(first)) < 1e-4
    assert np.max(np.abs(last)) > 0.3


def test_gain_ramps_without_clicking():
    g = Gain(SR)
    g.set_params({"enabled": True, "gain_db": 0.0})
    g.reset()
    first = g.process(sine(100.0, BLOCK))
    g.set_params({"enabled": True, "gain_db": 20.0})
    second = g.process(sine(100.0, BLOCK))
    # A jump would show up as a big first-sample discontinuity.
    step = abs(second[0] - first[-1])
    assert step < 0.3


# ------------------------------------------------------------------- misc
def test_biquad_lowpass_actually_attenuates_highs():
    bq = Biquad(SR, "lowpass", 1000.0, 0.707)
    low = run_blocks(bq, sine(100.0, SR // 4))
    bq.reset()
    high = run_blocks(bq, sine(10000.0, SR // 4))
    assert np.max(np.abs(low[1000:])) > 0.4
    assert np.max(np.abs(high[1000:])) < 0.05


def test_eq_curve_matches_requested_boost():
    eq = Equalizer(SR)
    eq.set_params({"enabled": True, "bands": [
        {"type": "peaking", "freq": 1000.0, "gain_db": 6.0, "q": 1.0}]})
    curve = eq.curve_db(np.array([1000.0]))
    assert curve[0] == pytest.approx(6.0, abs=0.2)


def test_reverb_produces_a_tail_after_the_input_stops():
    rv = Reverb(SR)
    rv.set_params({"enabled": True, "size": 0.8, "damping": 0.3, "width": 1.0,
                   "mix": 1.0, "predelay_ms": 0.0})
    impulse = np.zeros(SR, dtype=np.float32)
    impulse[:64] = 1.0
    out = run_blocks(rv, impulse)
    assert np.max(np.abs(out[SR // 2:])) > 1e-4


# ------------------------------------------------------------------ chain
def test_chain_runs_every_preset_without_exploding():
    cfg = config.Config()
    x = sine(220.0, SR // 4) * 0.5
    for name in presets.BUILTIN:
        assert presets.apply_preset(cfg, name)
        ch = chain_mod.VoiceChain(SR)
        ch.configure(cfg.voice, cfg.fx)
        out = run_blocks(ch, x.astype(np.float32))
        assert np.all(np.isfinite(out)), f"preset {name} produced non-finite audio"
        assert np.max(np.abs(out)) < 12.0, f"preset {name} is wildly loud"


def test_chain_bypass_is_bit_exact():
    cfg = config.Config()
    ch = chain_mod.VoiceChain(SR)
    ch.configure(cfg.voice, cfg.fx)
    ch.bypassed = True
    x = sine(440.0, BLOCK)
    assert np.array_equal(ch.process(x), x)


def test_preset_switch_does_not_leak_previous_stages():
    cfg = config.Config()
    presets.apply_preset(cfg, "Cave")
    assert cfg.fx("reverb")["enabled"]
    presets.apply_preset(cfg, "Clean")
    assert not cfg.fx("reverb")["enabled"]
    assert not cfg.fx("delay")["enabled"]
