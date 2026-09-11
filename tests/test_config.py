"""Config persistence: round-trips, migration, and surviving a mangled file."""
from __future__ import annotations

import json

import pytest

from micforge import config


def test_roundtrip_preserves_every_section(tmp_path):
    cfg = config.Config()
    cfg.mixer.mic_gain_db = 7.5
    cfg.capture.mode = "process"
    cfg.capture.process_name = "game.exe"
    cfg.voice.fx["pitch"]["semitones"] = 4.5
    cfg.soundboard.sounds.append(config.SoundEntry(name="Airhorn", path="a.mp3"))
    cfg.discord.triggers.append(config.TriggerRule(event="voice_connected"))

    path = tmp_path / "config.json"
    config.save(cfg, path)
    loaded = config.load(path)

    assert loaded.mixer.mic_gain_db == 7.5
    assert loaded.capture.mode == "process"
    assert loaded.capture.process_name == "game.exe"
    assert loaded.fx("pitch")["semitones"] == 4.5
    assert len(loaded.soundboard.sounds) == 1
    assert loaded.soundboard.sounds[0].name == "Airhorn"
    assert len(loaded.discord.triggers) == 1


def test_missing_file_gives_defaults(tmp_path):
    cfg = config.load(tmp_path / "nope.json")
    assert cfg.devices.samplerate == 48000
    assert cfg.version == config.CONFIG_VERSION


def test_unreadable_file_falls_back_and_keeps_a_copy(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    cfg = config.load(path)
    assert cfg.devices.samplerate == 48000
    assert (tmp_path / "config.broken.json").exists(), (
        "a corrupt config should be preserved, not silently overwritten")


def test_unknown_keys_are_ignored_and_missing_keys_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "version": config.CONFIG_VERSION,
        "mixer": {"mic_gain_db": 3.0, "invented_by_a_future_version": 42},
        "devices": {},
    }), encoding="utf-8")
    cfg = config.load(path)
    assert cfg.mixer.mic_gain_db == 3.0
    assert cfg.mixer.master_gain_db == 0.0
    assert not hasattr(cfg.mixer, "invented_by_a_future_version")


def test_wrong_types_do_not_crash_the_load(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "devices": {"samplerate": "48000", "blocksize": 240.0},
        "soundboard": {"sounds": "this should be a list"},
    }), encoding="utf-8")
    cfg = config.load(path)
    assert cfg.devices.samplerate == 48000
    assert cfg.devices.blocksize == 240
    assert cfg.soundboard.sounds == []


def test_v2_monitor_flag_migrates_to_split_flags(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "version": 2,
        "mixer": {"monitor_all": True},
    }), encoding="utf-8")
    cfg = config.load(path)
    assert cfg.mixer.monitor_mic is True
    assert cfg.mixer.monitor_capture is True
    assert cfg.version == config.CONFIG_VERSION


def test_fx_helper_backfills_keys_a_hand_edited_file_lost():
    cfg = config.Config()
    cfg.voice.fx["reverb"] = {"enabled": True}
    params = cfg.fx("reverb")
    assert params["enabled"] is True
    assert "size" in params and "damping" in params


def test_fx_helper_repairs_a_non_dict_entry():
    cfg = config.Config()
    cfg.voice.fx["delay"] = "corrupt"
    params = cfg.fx("delay")
    assert isinstance(params, dict)
    assert "time_ms" in params


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    cfg = config.Config()
    path = tmp_path / "config.json"
    for _ in range(3):
        config.save(cfg, path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == []


def test_sound_lookup_by_id():
    cfg = config.Config()
    entry = config.SoundEntry(name="Boom")
    cfg.soundboard.sounds.append(entry)
    assert cfg.sound_by_id(entry.id) is entry
    assert cfg.sound_by_id("nope") is None


def test_sound_entries_get_unique_ids():
    ids = {config.SoundEntry().id for _ in range(200)}
    assert len(ids) == 200


@pytest.mark.parametrize("kind", list(config._fx_defaults()))
def test_every_effect_has_an_enabled_flag(kind):
    assert "enabled" in config._fx_defaults()[kind]


def test_default_order_covers_every_effect():
    assert set(config.DEFAULT_FX_ORDER) == set(config._fx_defaults())
