"""Ring buffers, resampling and mono folding - the glue every stream depends on."""
from __future__ import annotations

import numpy as np
import pytest

from micforge.audio.ring import RingBuffer, StreamResampler, resample_offline, to_mono


def test_write_then_read_returns_the_same_samples():
    ring = RingBuffer(1024)
    data = np.arange(100, dtype=np.float32)
    ring.write(data)
    out = ring.read(100)[:, 0]
    assert np.array_equal(out, data)
    assert ring.available == 0


def test_reading_more_than_available_pads_with_silence():
    ring = RingBuffer(1024)
    ring.write(np.ones(10, dtype=np.float32))
    out = ring.read(50)[:, 0]
    assert np.array_equal(out[:10], np.ones(10, dtype=np.float32))
    assert np.all(out[10:] == 0.0)
    assert ring.underflows == 1


def test_overflow_drops_the_oldest_audio_not_the_newest():
    """Live audio should stay current; dropping the backlog is the right trade."""
    ring = RingBuffer(128)
    ring.write(np.arange(100, dtype=np.float32))
    ring.write(np.arange(100, 180, dtype=np.float32))
    out = ring.read(128)[:, 0]
    assert out[-1] == 179.0
    assert ring.overflows == 1


def test_write_larger_than_capacity_keeps_the_tail():
    ring = RingBuffer(64)
    ring.write(np.arange(500, dtype=np.float32))
    out = ring.read(64)[:, 0]
    assert out[-1] == 499.0


def test_drift_correction_trims_a_buffer_that_keeps_growing():
    ring = RingBuffer(4800)
    ring.write(np.ones(2000, dtype=np.float32))
    before = ring.available
    ring.read_drift_corrected(240, target_fill=480)
    after = ring.available
    assert after < before - 240, "an over-full ring should shed extra frames"


def test_drift_correction_leaves_a_healthy_buffer_alone():
    ring = RingBuffer(4800)
    ring.write(np.ones(500, dtype=np.float32))
    ring.read_drift_corrected(240, target_fill=480)
    assert ring.available == 260


def test_channel_count_is_respected():
    ring = RingBuffer(256, channels=2)
    stereo = np.tile(np.arange(50, dtype=np.float32)[:, None], (1, 2))
    ring.write(stereo)
    out = ring.read(50)
    assert out.shape == (50, 2)
    assert np.array_equal(out, stereo)


def test_mono_input_fans_out_to_a_stereo_ring():
    ring = RingBuffer(256, channels=2)
    ring.write(np.arange(10, dtype=np.float32))
    out = ring.read(10)
    assert np.array_equal(out[:, 0], out[:, 1])


def test_clear_resets_everything():
    ring = RingBuffer(256)
    ring.write(np.ones(100, dtype=np.float32))
    ring.clear()
    assert ring.available == 0
    assert np.all(ring.read(10) == 0.0)


# ------------------------------------------------------------------ mono fold
def test_to_mono_downmix_averages_channels():
    data = np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32)
    assert np.allclose(to_mono(data), [0.5, 0.5])


def test_to_mono_can_pick_one_side():
    data = np.array([[1.0, -1.0]], dtype=np.float32)
    assert to_mono(data, "left")[0] == 1.0
    assert to_mono(data, "right")[0] == -1.0


# ----------------------------------------------------------------- resampling
@pytest.mark.parametrize("src,dst", [(44100, 48000), (48000, 44100), (16000, 48000)])
def test_stream_resampler_output_length_is_about_right(src, dst):
    res = StreamResampler(src, dst, 1)
    total_in, total_out = 0, 0
    block = np.zeros((512, 1), dtype=np.float32)
    for _ in range(40):
        total_in += block.shape[0]
        total_out += res.process(block).shape[0]
    expected = total_in * dst / src
    assert abs(total_out - expected) < 50, (
        f"{total_out} frames out for {total_in} in, expected about {expected:.0f}")


def test_stream_resampler_preserves_a_sine_frequency():
    src, dst, freq = 44100, 48000, 1000.0
    res = StreamResampler(src, dst, 1)
    chunks = []
    phase = 0.0
    for _ in range(60):
        t = (phase + np.arange(512)) / src
        phase += 512
        block = np.sin(2 * np.pi * freq * t).astype(np.float32).reshape(-1, 1)
        chunks.append(res.process(block))
    out = np.concatenate(chunks)[:, 0]
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    peak = np.argmax(spec) * dst / len(out)
    assert peak == pytest.approx(freq, rel=0.02)


def test_stream_resampler_is_a_passthrough_at_equal_rates():
    res = StreamResampler(48000, 48000, 1)
    assert res.passthrough
    block = np.random.default_rng(0).standard_normal((256, 1)).astype(np.float32)
    assert np.array_equal(res.process(block), block)


def test_stream_resampler_has_no_gap_at_block_boundaries():
    """A discontinuity between blocks is an audible tick."""
    src, dst = 48000, 44100
    res = StreamResampler(src, dst, 1)
    freq = 500.0
    chunks = []
    for i in range(20):
        t = (i * 256 + np.arange(256)) / src
        chunks.append(res.process(
            np.sin(2 * np.pi * freq * t).astype(np.float32).reshape(-1, 1)))
    out = np.concatenate(chunks)[:, 0]
    if out.size > 100:
        jumps = np.abs(np.diff(out[50:]))
        expected_step = 2 * np.pi * freq / dst
        assert np.max(jumps) < expected_step * 4


def test_offline_resample_scales_length():
    data = np.zeros((44100, 1), dtype=np.float32)
    out = resample_offline(data, 44100, 48000)
    assert abs(out.shape[0] - 48000) < 20


def test_offline_resample_is_identity_at_equal_rates():
    data = np.arange(100, dtype=np.float32).reshape(-1, 1)
    assert np.array_equal(resample_offline(data, 48000, 48000), data)
