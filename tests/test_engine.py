"""Engine wiring that can be checked without opening an audio device."""
from __future__ import annotations

import numpy as np
import pytest

from micforge import config
from micforge.audio.engine import AudioEngine
from micforge.dsp import db_to_lin
from micforge.dsp.chain import VoiceChain
from micforge.sound.player import Soundboard


@pytest.fixture()
def engine():
    cfg = config.Config()
    cfg.devices.virtual_out = ""
    cfg.devices.monitor_out = ""
    return AudioEngine(cfg, Soundboard(cfg, cfg.devices.samplerate)), cfg


def test_capture_gets_its_own_chain_instance(engine):
    """Sharing one chain between the mic and the captured app corrupts both.

    Every stage carries state - filter memories, delay lines, the vocoder's
    phase accumulator. Pushing two different signals through one instance in
    the same tick interleaves that state and both outputs come out wrong.
    """
    eng, _cfg = engine
    assert eng.chain is not eng.capture_chain


def test_interleaving_two_signals_through_one_chain_really_does_differ():
    """The reason the test above exists, demonstrated."""
    cfg = config.Config()
    cfg.voice.fx["reverb"]["enabled"] = True
    cfg.voice.fx["reverb"]["mix"] = 0.5

    rng = np.random.default_rng(0)
    a = rng.standard_normal(240).astype(np.float32) * 0.2
    b = rng.standard_normal(240).astype(np.float32) * 0.2

    solo = VoiceChain(48000)
    solo.configure(cfg.voice, cfg.fx)
    for _ in range(4):
        clean = solo.process(a)

    shared = VoiceChain(48000)
    shared.configure(cfg.voice, cfg.fx)
    for _ in range(4):
        polluted = shared.process(a)
        shared.process(b)

    assert not np.allclose(clean, polluted, atol=1e-6)


def test_apply_settings_reconfigures_both_chains(engine):
    eng, cfg = engine
    cfg.voice.fx["reverb"]["enabled"] = True
    eng.apply_settings()
    assert "reverb" in eng.chain.active_kinds()
    assert "reverb" in eng.capture_chain.active_kinds()


def test_duck_curve_falls_to_the_configured_depth(engine):
    eng, cfg = engine
    cfg.mixer.duck_depth_db = -12.0
    cfg.mixer.duck_attack_ms = 10.0
    target = db_to_lin(-12.0)
    for _ in range(200):
        curve = eng._duck_curve(240, playing=True)
    assert curve[-1] == pytest.approx(target, rel=0.05)


def test_duck_curve_returns_to_unity_when_nothing_plays(engine):
    eng, cfg = engine
    cfg.mixer.duck_release_ms = 50.0
    for _ in range(50):
        eng._duck_curve(240, playing=True)
    for _ in range(300):
        curve = eng._duck_curve(240, playing=False)
    assert curve[-1] == pytest.approx(1.0, rel=0.01)


def test_duck_curve_is_a_ramp_not_a_step(engine):
    """A jump in gain is an audible click."""
    eng, _cfg = engine
    curve = eng._duck_curve(240, playing=True)
    assert np.max(np.abs(np.diff(curve))) < 0.02


def test_status_and_hints_do_not_crash_before_start(engine):
    from micforge.app import MicForgeApp

    _eng, cfg = engine
    app = MicForgeApp(cfg)
    assert isinstance(app.status_line(), str)
    assert isinstance(app.first_run_hints(), list)
    assert isinstance(app.discord_summary(), str)


def test_ring_health_reports_every_bus(engine):
    eng, _cfg = engine
    health = eng.ring_health()
    assert set(health) == {"mic", "capture", "out", "monitor"}


def test_needs_restart_detects_a_device_change(engine):
    eng, cfg = engine
    before = config.DeviceSettings(**vars(cfg.devices))
    assert not eng.needs_restart_for(before)
    cfg.devices.microphone = "Something Else"
    assert eng.needs_restart_for(before)


def test_hotkey_actions_route_to_the_right_place():
    from micforge.app import ACTION_PTT, MicForgeApp

    cfg = config.Config()
    app = MicForgeApp(cfg)
    app._on_hotkey_press(ACTION_PTT)
    assert app.engine.ptt_down is True
    app._on_hotkey_release(ACTION_PTT)
    assert app.engine.ptt_down is False


def test_rebuild_hotkeys_collects_every_binding():
    from micforge.app import MicForgeApp

    cfg = config.Config()
    entry = config.SoundEntry(name="Air", path="a.wav", hotkey="ctrl+f1")
    cfg.soundboard.sounds.append(entry)
    cfg.soundboard.stop_all_hotkey = "ctrl+f12"
    cfg.mixer.ptt_hotkey = "f13"
    app = MicForgeApp(cfg)
    bad = app.rebuild_hotkeys()
    assert bad == []
    bound = app.hotkeys.bound_specs()
    assert f"sound:{entry.id}" in bound
    assert "stop_all" in bound
    assert "ptt" in bound
