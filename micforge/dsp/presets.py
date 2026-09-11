"""Built-in voice presets, plus save/load for user presets.

A preset is a sparse overlay: only the stages it mentions are touched, and
every stage it does *not* mention is switched off. That way switching presets
is predictable -- you never inherit a stray reverb from the last one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import config, log

_log = log.get("presets")

# name -> {stage: {param: value}}.  "enabled" defaults to True per listed stage.
BUILTIN: dict[str, dict] = {
    "Clean": {
        "compressor": {"threshold_db": -18.0, "ratio": 3.0, "makeup_db": 3.0},
        "makeup": {"gain_db": 0.0},
    },
    "Broadcast": {
        "gate": {"threshold_db": -42.0, "release_ms": 180.0},
        "highpass": {"freq": 95.0},
        "eq": {"bands": [
            {"type": "lowshelf", "freq": 120.0, "gain_db": -2.0, "q": 0.707},
            {"type": "peaking", "freq": 320.0, "gain_db": -3.0, "q": 1.1},
            {"type": "peaking", "freq": 1800.0, "gain_db": 1.5, "q": 0.9},
            {"type": "peaking", "freq": 3800.0, "gain_db": 3.5, "q": 0.9},
            {"type": "highshelf", "freq": 9000.0, "gain_db": 2.0, "q": 0.707},
        ]},
        "compressor": {"threshold_db": -22.0, "ratio": 4.0, "attack_ms": 5.0,
                       "release_ms": 140.0, "knee_db": 8.0, "makeup_db": 6.0},
        "makeup": {"gain_db": 1.0},
    },
    "Woman": {
        "highpass": {"freq": 110.0},
        "pitch": {"semitones": 4.5, "formant_semitones": 3.0, "quality": "high"},
        "eq": {"bands": [
            {"type": "lowshelf", "freq": 180.0, "gain_db": -4.0, "q": 0.707},
            {"type": "peaking", "freq": 900.0, "gain_db": 1.5, "q": 1.0},
            {"type": "peaking", "freq": 2800.0, "gain_db": 2.5, "q": 0.9},
            {"type": "peaking", "freq": 5000.0, "gain_db": 1.5, "q": 1.0},
            {"type": "highshelf", "freq": 8000.0, "gain_db": 1.5, "q": 0.707},
        ]},
        "compressor": {"threshold_db": -20.0, "ratio": 3.0, "makeup_db": 4.0},
    },
    "Man (deep)": {
        "pitch": {"semitones": -4.0, "formant_semitones": -2.0, "quality": "high"},
        "eq": {"bands": [
            {"type": "lowshelf", "freq": 140.0, "gain_db": 3.5, "q": 0.707},
            {"type": "peaking", "freq": 400.0, "gain_db": 1.0, "q": 1.0},
            {"type": "peaking", "freq": 2500.0, "gain_db": -1.5, "q": 1.0},
            {"type": "peaking", "freq": 4500.0, "gain_db": 0.0, "q": 1.0},
            {"type": "highshelf", "freq": 9000.0, "gain_db": -2.0, "q": 0.707},
        ]},
        "compressor": {"threshold_db": -20.0, "ratio": 3.5, "makeup_db": 4.0},
    },
    "Chipmunk": {
        "pitch": {"semitones": 8.0, "formant_semitones": 8.0, "quality": "high"},
        "compressor": {"threshold_db": -18.0, "ratio": 3.0, "makeup_db": 3.0},
    },
    "Robot": {
        "gate": {"threshold_db": -40.0},
        "robot": {"pitch_hz": 120.0, "mix": 1.0},
        "eq": {"bands": [
            {"type": "lowshelf", "freq": 150.0, "gain_db": -3.0, "q": 0.707},
            {"type": "peaking", "freq": 800.0, "gain_db": 2.0, "q": 1.2},
            {"type": "peaking", "freq": 2200.0, "gain_db": 3.0, "q": 1.2},
            {"type": "peaking", "freq": 5000.0, "gain_db": -2.0, "q": 1.0},
            {"type": "highshelf", "freq": 9000.0, "gain_db": -6.0, "q": 0.707},
        ]},
        "compressor": {"threshold_db": -22.0, "ratio": 6.0, "makeup_db": 5.0},
    },
    "Dalek": {
        "ringmod": {"freq": 30.0, "mix": 0.85, "waveform": "sine"},
        "distortion": {"drive_db": 10.0, "tone": 0.6, "mix": 0.5, "kind": "tanh"},
        "compressor": {"threshold_db": -24.0, "ratio": 8.0, "makeup_db": 6.0},
    },
    "Demon": {
        "pitch": {"semitones": -7.0, "formant_semitones": -3.0, "quality": "high"},
        "chorus": {"rate_hz": 0.25, "depth_ms": 8.0, "voices": 3, "mix": 0.45},
        "distortion": {"drive_db": 8.0, "tone": 0.35, "mix": 0.4, "kind": "tanh"},
        "reverb": {"size": 0.75, "damping": 0.35, "mix": 0.28, "predelay_ms": 20.0},
        "compressor": {"threshold_db": -22.0, "ratio": 4.0, "makeup_db": 5.0},
    },
    "Alien": {
        "pitch": {"semitones": 3.0, "formant_semitones": -5.0, "quality": "high"},
        "ringmod": {"freq": 220.0, "mix": 0.4, "waveform": "sine"},
        "vibrato": {"rate_hz": 6.5, "depth_ms": 1.6},
        "delay": {"time_ms": 90.0, "feedback": 0.35, "mix": 0.2, "damping": 0.4},
    },
    "Walkie-talkie": {
        "gate": {"threshold_db": -38.0, "release_ms": 90.0},
        "highpass": {"freq": 420.0, "q": 0.9},
        "lowpass": {"freq": 3000.0, "q": 0.9},
        "distortion": {"drive_db": 14.0, "tone": 0.7, "mix": 0.75, "kind": "hard"},
        "compressor": {"threshold_db": -26.0, "ratio": 8.0, "makeup_db": 8.0},
    },
    "Telephone": {
        "highpass": {"freq": 300.0, "q": 0.8},
        "lowpass": {"freq": 3400.0, "q": 0.8},
        "bitcrush": {"bits": 10.0, "downsample": 1.0, "mix": 0.5},
        "compressor": {"threshold_db": -22.0, "ratio": 5.0, "makeup_db": 6.0},
    },
    "Megaphone": {
        "highpass": {"freq": 500.0, "q": 1.2},
        "lowpass": {"freq": 4000.0, "q": 1.2},
        "distortion": {"drive_db": 20.0, "tone": 0.8, "mix": 0.9, "kind": "fuzz"},
        "delay": {"time_ms": 45.0, "feedback": 0.25, "mix": 0.15, "damping": 0.5},
        "compressor": {"threshold_db": -28.0, "ratio": 10.0, "makeup_db": 9.0},
    },
    "Cave": {
        "reverb": {"size": 0.95, "damping": 0.2, "width": 1.0, "mix": 0.45,
                   "predelay_ms": 40.0},
        "delay": {"time_ms": 320.0, "feedback": 0.35, "mix": 0.2, "damping": 0.5},
        "compressor": {"threshold_db": -20.0, "ratio": 3.0, "makeup_db": 4.0},
    },
    "Ghost": {
        "pitch": {"semitones": -2.0, "formant_semitones": 2.0, "quality": "high"},
        "tremolo": {"rate_hz": 3.5, "depth": 0.35, "waveform": "sine"},
        "reverb": {"size": 0.85, "damping": 0.4, "mix": 0.4, "predelay_ms": 30.0},
        "chorus": {"rate_hz": 0.4, "depth_ms": 6.0, "voices": 2, "mix": 0.3},
    },
    "Stadium announcer": {
        "highpass": {"freq": 120.0},
        "eq": {"bands": [
            {"type": "lowshelf", "freq": 150.0, "gain_db": 2.0, "q": 0.707},
            {"type": "peaking", "freq": 400.0, "gain_db": -2.0, "q": 1.0},
            {"type": "peaking", "freq": 2000.0, "gain_db": 3.0, "q": 0.9},
            {"type": "peaking", "freq": 4000.0, "gain_db": 2.0, "q": 0.9},
            {"type": "highshelf", "freq": 8000.0, "gain_db": -3.0, "q": 0.707},
        ]},
        "delay": {"time_ms": 260.0, "feedback": 0.28, "mix": 0.22, "damping": 0.45},
        "reverb": {"size": 0.7, "damping": 0.45, "mix": 0.25},
        "compressor": {"threshold_db": -24.0, "ratio": 6.0, "makeup_db": 8.0},
    },
    "Underwater": {
        "lowpass": {"freq": 900.0, "q": 1.4},
        "vibrato": {"rate_hz": 1.2, "depth_ms": 5.0},
        "reverb": {"size": 0.6, "damping": 0.8, "mix": 0.35},
        "compressor": {"threshold_db": -20.0, "ratio": 3.0, "makeup_db": 6.0},
    },
    "Giant": {
        "pitch": {"semitones": -9.0, "formant_semitones": -6.0, "quality": "high"},
        "reverb": {"size": 0.8, "damping": 0.4, "mix": 0.3, "predelay_ms": 25.0},
        "compressor": {"threshold_db": -20.0, "ratio": 3.5, "makeup_db": 5.0},
    },
    "Small child": {
        "pitch": {"semitones": 6.0, "formant_semitones": 5.0, "quality": "high"},
        "highpass": {"freq": 160.0},
        "compressor": {"threshold_db": -18.0, "ratio": 3.0, "makeup_db": 3.0},
    },
    "Broken radio": {
        "highpass": {"freq": 380.0},
        "lowpass": {"freq": 3200.0},
        "bitcrush": {"bits": 5.0, "downsample": 3.0, "mix": 0.7},
        "tremolo": {"rate_hz": 11.0, "depth": 0.25, "waveform": "random"},
        "distortion": {"drive_db": 16.0, "tone": 0.65, "mix": 0.6, "kind": "hard"},
    },
}


def apply_preset(cfg: config.Config, name: str) -> bool:
    """Overwrite the voice chain from a built-in or user preset."""
    overlay = BUILTIN.get(name)
    if overlay is None:
        overlay = load_user(name)
    if overlay is None:
        _log.warning("no such preset: %s", name)
        return False

    fresh = config._fx_defaults()
    for params in fresh.values():
        params["enabled"] = False
    # The two safety stages stay on unless a preset says otherwise.
    fresh["compressor"]["enabled"] = True
    fresh["makeup"]["enabled"] = True

    for kind, params in overlay.items():
        if kind not in fresh:
            continue
        fresh[kind].update(params)
        fresh[kind].setdefault("enabled", True)
        if "enabled" not in params:
            fresh[kind]["enabled"] = True

    cfg.voice.fx = fresh
    cfg.voice.preset_name = name
    cfg.voice.enabled = True
    _log.info("applied preset %s", name)
    return True


def snapshot(cfg: config.Config) -> dict:
    """Current chain as a preset overlay (only the enabled stages)."""
    out: dict = {}
    for kind, params in cfg.voice.fx.items():
        if not isinstance(params, dict):
            continue
        if params.get("enabled"):
            out[kind] = dict(params)
    return out


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", name).strip()
    return cleaned or "preset"


def user_preset_names() -> list[str]:
    d = config.presets_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def save_user(name: str, cfg: config.Config) -> Path:
    d = config.presets_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_name(name)}.json"
    payload = {
        "name": name,
        "order": list(cfg.voice.order),
        "stages": snapshot(cfg),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log.info("saved preset %s", path)
    return path


def load_user(name: str) -> dict | None:
    path = config.presets_dir() / f"{_safe_name(name)}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.error("preset %s unreadable: %s", path, exc)
        return None
    stages = raw.get("stages")
    return stages if isinstance(stages, dict) else None


def delete_user(name: str) -> bool:
    path = config.presets_dir() / f"{_safe_name(name)}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def all_names() -> list[str]:
    return list(BUILTIN) + [n for n in user_preset_names() if n not in BUILTIN]
