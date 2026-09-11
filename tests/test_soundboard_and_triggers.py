"""Soundboard playback and the Discord trigger rule engine."""
from __future__ import annotations

import time

import numpy as np
import pytest

from micforge import config
from micforge.discordlink.rpc import VoiceEvent
from micforge.discordlink.triggers import TriggerEngine, default_join_rule
from micforge.sound import decode
from micforge.sound.player import Soundboard

SR = 48000


@pytest.fixture()
def clip_path(tmp_path):
    """One second of a 440 Hz tone as a real file on disk."""
    sf = pytest.importorskip("soundfile")
    path = tmp_path / "tone.wav"
    t = np.arange(SR, dtype=np.float64) / SR
    sf.write(path, (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), SR)
    decode.clear_cache()
    return str(path)


@pytest.fixture()
def board(clip_path):
    cfg = config.Config()
    cfg.soundboard.sounds.append(
        config.SoundEntry(name="Tone", path=clip_path, fade_out_ms=0.0))
    return Soundboard(cfg, SR), cfg


# ------------------------------------------------------------------ decoding
def test_decode_returns_the_expected_shape_and_rate(clip_path):
    clip = decode.load(clip_path, SR)
    assert clip.samplerate == SR
    assert clip.frames == pytest.approx(SR, abs=10)
    assert clip.duration == pytest.approx(1.0, abs=0.01)


def test_decode_caches_by_path(clip_path):
    decode.clear_cache()
    first = decode.load(clip_path, SR)
    second = decode.load(clip_path, SR)
    assert first is second
    count, _ = decode.cache_stats()
    assert count == 1


def test_decode_rejects_a_missing_file():
    with pytest.raises(decode.DecodeError):
        decode.load("does-not-exist.wav", SR)


# ------------------------------------------------------------------ playback
def test_playing_a_clip_produces_audio_on_the_mic_bus(board):
    sb, cfg = board
    assert sb.play(cfg.soundboard.sounds[0])
    mic, mon = sb.read(4800)
    assert np.max(np.abs(mic)) > 0.1
    assert np.max(np.abs(mon)) > 0.1


def test_routing_flags_split_the_two_busses(board):
    sb, cfg = board
    entry = cfg.soundboard.sounds[0]
    entry.to_mic = False
    entry.to_monitor = True
    sb.play(entry)
    mic, mon = sb.read(4800)
    assert np.max(np.abs(mic)) == 0.0
    assert np.max(np.abs(mon)) > 0.1


def test_a_clip_finishes_and_frees_its_voice(board):
    sb, cfg = board
    sb.play(cfg.soundboard.sounds[0])
    for _ in range(30):
        sb.read(4800)
    assert sb.active_count == 0


def test_looping_keeps_going_past_the_end(board):
    sb, cfg = board
    entry = cfg.soundboard.sounds[0]
    entry.loop = True
    sb.play(entry)
    for _ in range(30):
        mic, _ = sb.read(4800)
    assert sb.active_count == 1
    assert np.max(np.abs(mic)) > 0.1


def test_stop_all_fades_out_rather_than_cutting(board):
    sb, cfg = board
    cfg.soundboard.sounds[0].fade_out_ms = 50.0
    sb.play(cfg.soundboard.sounds[0])
    sb.read(2400)
    sb.stop_all(fade_ms=50.0)
    mic, _ = sb.read(4800)
    # The fade must reach zero, and must not do it in one jump.
    assert abs(mic[-1]) < 1e-6
    assert np.max(np.abs(np.diff(mic[:2400]))) < 0.1


def test_panic_stops_everything_immediately(board):
    sb, cfg = board
    sb.play(cfg.soundboard.sounds[0])
    sb.panic()
    assert sb.active_count == 0
    mic, _ = sb.read(480)
    assert np.all(mic == 0.0)


def test_voice_limit_steals_the_oldest(board):
    sb, cfg = board
    cfg.soundboard.max_voices = 2
    cfg.soundboard.restart_on_retrigger = False
    entry = cfg.soundboard.sounds[0]
    for _ in range(8):
        sb.play(entry)
        sb.read(64)
    # Voices already fading out still make sound, so they are allowed to linger
    # past the limit - but the pool must stay bounded either way.
    assert sb.playing_count <= cfg.soundboard.max_voices
    assert sb.active_count <= cfg.soundboard.max_voices * 2


def test_gain_is_applied(board):
    sb, cfg = board
    entry = cfg.soundboard.sounds[0]
    sb.play(entry)
    loud, _ = sb.read(4800)
    sb.panic()
    entry.gain_db = -20.0
    sb.play(entry)
    quiet, _ = sb.read(4800)
    assert np.max(np.abs(quiet)) < np.max(np.abs(loud)) * 0.2


def test_pitch_shift_changes_the_playback_rate(board):
    sb, cfg = board
    entry = cfg.soundboard.sounds[0]
    entry.pitch_semitones = 12.0
    sb.play(entry)
    chunks = [sb.read(2400)[0] for _ in range(8)]
    out = np.concatenate(chunks)
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    peak = np.argmax(spec) * SR / len(out)
    assert peak == pytest.approx(880.0, rel=0.05)


def test_trim_limits_the_played_region(board):
    sb, cfg = board
    entry = cfg.soundboard.sounds[0]
    entry.start_ms = 0.0
    entry.end_ms = 100.0
    entry.fade_out_ms = 0.0
    sb.play(entry)
    total = 0
    for _ in range(20):
        mic, _ = sb.read(2400)
        total += int(np.sum(np.abs(mic) > 1e-4))
        if sb.active_count == 0:
            break
    assert total < SR * 0.2, "a 100 ms trim should not play for a whole second"


def test_missing_file_reports_an_error_instead_of_raising(board):
    sb, cfg = board
    cfg.soundboard.sounds[0].path = "gone.wav"
    assert sb.play(cfg.soundboard.sounds[0]) is False
    assert sb.last_error


def test_read_with_no_voices_is_silent(board):
    sb, _cfg = board
    mic, mon = sb.read(480)
    assert np.all(mic == 0.0) and np.all(mon == 0.0)


# ------------------------------------------------------------------ triggers
class _FakeBoard:
    def __init__(self):
        self.played: list[str] = []

    def play(self, entry, gain_db=0.0):
        self.played.append(entry.id)
        return True


@pytest.fixture()
def rig():
    cfg = config.Config()
    entry = config.SoundEntry(name="Join", path="x.wav")
    cfg.soundboard.sounds.append(entry)
    board = _FakeBoard()
    engine = TriggerEngine(cfg, board)
    return cfg, entry, board, engine


def _connected(**kwargs) -> VoiceEvent:
    return VoiceEvent(kind="voice_connected", **kwargs)


def test_a_matching_rule_fires(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 0
    cfg.discord.triggers.append(rule)
    assert engine.handle(_connected()) == 1
    assert board.played == [entry.id]


def test_a_different_event_does_not_fire(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 0
    cfg.discord.triggers.append(rule)
    engine.handle(VoiceEvent(kind="voice_disconnected"))
    assert board.played == []


def test_disabled_rules_are_skipped(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 0
    rule.enabled = False
    cfg.discord.triggers.append(rule)
    engine.handle(_connected())
    assert board.played == []


def test_cooldown_blocks_a_rapid_second_fire(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 0
    rule.cooldown_ms = 5000
    cfg.discord.triggers.append(rule)
    engine.handle(_connected())
    engine.handle(_connected())
    assert len(board.played) == 1, "a flapping connection must not machine-gun the sound"


def test_channel_filter_matches_by_name(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 0
    rule.cooldown_ms = 0
    rule.channel_filter = "gaming"
    cfg.discord.triggers.append(rule)

    engine.handle(_connected(channel_name="General"))
    assert board.played == []
    engine.handle(_connected(channel_name="Gaming Lounge"))
    assert len(board.played) == 1


def test_user_filter_matches_by_name(rig):
    cfg, entry, board, engine = rig
    rule = config.TriggerRule(event="user_joined", sound_id=entry.id, delay_ms=0,
                              cooldown_ms=0, user_filter="ripley")
    cfg.discord.triggers.append(rule)
    engine.handle(VoiceEvent(kind="user_joined", user_name="someone"))
    assert board.played == []
    engine.handle(VoiceEvent(kind="user_joined", user_name="EllenRipley"))
    assert len(board.played) == 1


def test_once_per_session_only_fires_once(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 0
    rule.cooldown_ms = 0
    rule.once_per_session = True
    cfg.discord.triggers.append(rule)
    engine.handle(_connected())
    engine.handle(_connected())
    assert len(board.played) == 1
    engine.reset_session()
    engine.handle(_connected())
    assert len(board.played) == 2


def test_several_rules_can_fire_on_one_event(rig):
    cfg, entry, board, engine = rig
    second = config.SoundEntry(name="Second", path="y.wav")
    cfg.soundboard.sounds.append(second)
    for sound_id in (entry.id, second.id):
        rule = default_join_rule(sound_id)
        rule.delay_ms = 0
        cfg.discord.triggers.append(rule)
    assert engine.handle(_connected()) == 2
    assert set(board.played) == {entry.id, second.id}


def test_delay_defers_the_play(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule(entry.id)
    rule.delay_ms = 120
    cfg.discord.triggers.append(rule)
    engine.handle(_connected())
    assert board.played == [], "the sound should wait for its delay"
    time.sleep(0.35)
    assert board.played == [entry.id]
    engine.cancel_pending()


def test_a_rule_pointing_at_a_deleted_sound_is_ignored(rig):
    cfg, entry, board, engine = rig
    rule = default_join_rule("no-such-sound")
    rule.delay_ms = 0
    cfg.discord.triggers.append(rule)
    assert engine.handle(_connected()) == 0
    assert board.played == []


def test_history_records_events(rig):
    _cfg, _entry, _board, engine = rig
    engine.handle(_connected(channel_name="General"))
    assert engine.history
    assert "voice_connected" in engine.history[-1][1]
