"""Configuration model + JSON persistence.

Everything the user can tweak lives here as a dataclass tree. The tree is
serialised to a single ``config.json`` next to the sound library. Unknown keys
in an on-disk file are ignored (forward compatible) and missing keys fall back
to the dataclass default (backward compatible).
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import typing
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import log

_log = log.get("config")

CONFIG_VERSION = 3


# --------------------------------------------------------------------------- paths
def data_dir() -> Path:
    override = os.environ.get("MICFORGE_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "MicForge"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "micforge"


def config_path() -> Path:
    return data_dir() / "config.json"


def sounds_dir() -> Path:
    return data_dir() / "sounds"


def presets_dir() -> Path:
    return data_dir() / "presets"


# --------------------------------------------------------------------------- audio
@dataclass
class DeviceSettings:
    """Which physical/virtual endpoints the engine binds to."""

    microphone: str = ""
    """Input device name. Empty = system default."""
    virtual_out: str = ""
    """Output device Discord reads as a microphone (VB-CABLE / MicForge Mic)."""
    monitor_out: str = ""
    """Where you hear the mix (headphones). Empty = disabled."""
    samplerate: int = 48000
    blocksize: int = 240
    """Frames per engine tick. 240 @ 48k = 5 ms."""
    mic_channel_mode: str = "mono"
    """mono | left | right"""
    prefer_hostapi: str = "auto"
    """auto | WASAPI | DirectSound | MME | ALSA"""
    exclusive_mode: bool = False
    output_latency: str = "low"
    input_latency: str = "low"
    keep_alive: bool = True
    """Keep streams open while idle so Discord never sees the device vanish."""


@dataclass
class CaptureSettings:
    """System / per-application audio grab."""

    mode: str = "off"
    """off | desktop | process | exclude"""
    endpoint: str = ""
    """Render endpoint for desktop loopback. Empty = default playback device."""
    process_pid: int = 0
    process_name: str = ""
    window_title: str = ""
    include_tree: bool = True
    """Capture the child processes too (Chrome/Discord style multi-process apps)."""
    auto_reattach: bool = True
    """If the target app restarts, re-bind it by executable name."""
    reattach_interval_s: float = 2.0
    stereo_to_mono: str = "downmix"
    """downmix | left | right"""


@dataclass
class MixerSettings:
    mic_enabled: bool = True
    mic_gain_db: float = 0.0
    mic_muted: bool = False
    capture_enabled: bool = True
    capture_gain_db: float = -6.0
    soundboard_gain_db: float = 0.0
    master_gain_db: float = 0.0

    monitor_enabled: bool = True
    monitor_gain_db: float = -6.0
    monitor_mic: bool = False
    monitor_capture: bool = False
    monitor_soundboard: bool = True

    duck_enabled: bool = True
    """Drop mic and game audio while a soundboard clip plays."""
    duck_mic: bool = False
    duck_capture: bool = True
    duck_depth_db: float = -12.0
    duck_attack_ms: float = 40.0
    duck_release_ms: float = 300.0

    limiter_enabled: bool = True
    limiter_ceiling_db: float = -1.0
    limiter_release_ms: float = 120.0

    ptt_enabled: bool = False
    ptt_hotkey: str = ""
    ptt_inverted: bool = False
    """True = push-to-mute instead of push-to-talk."""


# --------------------------------------------------------------------------- fx
DEFAULT_FX_ORDER = [
    "gate",
    "highpass",
    "lowpass",
    "eq",
    "pitch",
    "robot",
    "ringmod",
    "bitcrush",
    "distortion",
    "chorus",
    "vibrato",
    "tremolo",
    "delay",
    "reverb",
    "compressor",
    "makeup",
]


def _fx_defaults() -> dict:
    return {
        "gate": {"enabled": False, "threshold_db": -45.0, "attack_ms": 2.0,
                 "hold_ms": 80.0, "release_ms": 150.0, "range_db": -60.0},
        "highpass": {"enabled": False, "freq": 90.0, "q": 0.707},
        "lowpass": {"enabled": False, "freq": 12000.0, "q": 0.707},
        "eq": {"enabled": False,
               "bands": [
                   {"type": "lowshelf", "freq": 120.0, "gain_db": 0.0, "q": 0.707},
                   {"type": "peaking", "freq": 500.0, "gain_db": 0.0, "q": 1.0},
                   {"type": "peaking", "freq": 1800.0, "gain_db": 0.0, "q": 1.0},
                   {"type": "peaking", "freq": 4500.0, "gain_db": 0.0, "q": 1.0},
                   {"type": "highshelf", "freq": 9000.0, "gain_db": 0.0, "q": 0.707},
               ]},
        "pitch": {"enabled": False, "semitones": 0.0, "formant_semitones": 0.0,
                  "mix": 1.0, "quality": "high"},
        "robot": {"enabled": False, "pitch_hz": 120.0, "mix": 1.0},
        "ringmod": {"enabled": False, "freq": 60.0, "mix": 0.6, "waveform": "sine"},
        "bitcrush": {"enabled": False, "bits": 8.0, "downsample": 2.0, "mix": 1.0},
        "distortion": {"enabled": False, "drive_db": 12.0, "tone": 0.5,
                       "mix": 1.0, "kind": "tanh"},
        "chorus": {"enabled": False, "rate_hz": 0.8, "depth_ms": 3.0,
                   "voices": 2, "mix": 0.35, "spread": 0.5},
        "vibrato": {"enabled": False, "rate_hz": 5.0, "depth_ms": 2.0},
        "tremolo": {"enabled": False, "rate_hz": 5.0, "depth": 0.5, "waveform": "sine"},
        "delay": {"enabled": False, "time_ms": 180.0, "feedback": 0.3,
                  "mix": 0.25, "damping": 0.3},
        "reverb": {"enabled": False, "size": 0.5, "damping": 0.5,
                   "width": 1.0, "mix": 0.2, "predelay_ms": 15.0},
        "compressor": {"enabled": True, "threshold_db": -18.0, "ratio": 3.0,
                       "attack_ms": 8.0, "release_ms": 120.0, "knee_db": 6.0,
                       "makeup_db": 3.0},
        "makeup": {"enabled": True, "gain_db": 0.0},
    }


@dataclass
class VoiceSettings:
    """The mic effect chain."""

    enabled: bool = True
    preset_name: str = "Clean"
    order: list[str] = field(default_factory=lambda: list(DEFAULT_FX_ORDER))
    fx: dict = field(default_factory=_fx_defaults)
    apply_to_capture: bool = False
    """Run game/system audio through the same chain (usually not what you want)."""
    dry_wet: float = 1.0


# --------------------------------------------------------------------------- soundboard
@dataclass
class SoundEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "New sound"
    path: str = ""
    gain_db: float = 0.0
    hotkey: str = ""
    loop: bool = False
    fade_in_ms: float = 0.0
    fade_out_ms: float = 30.0
    start_ms: float = 0.0
    end_ms: float = 0.0
    """0 = play to the end."""
    pitch_semitones: float = 0.0
    speed: float = 1.0
    to_mic: bool = True
    to_monitor: bool = True
    stop_others: bool = False
    category: str = ""
    color: str = "#3d7dff"
    favourite: bool = False


@dataclass
class SoundboardSettings:
    sounds: list[SoundEntry] = field(default_factory=list)
    max_voices: int = 8
    stop_all_hotkey: str = ""
    global_gain_db: float = 0.0
    overlap: bool = True
    restart_on_retrigger: bool = True
    hotkeys_enabled: bool = True
    grid_columns: int = 4


# --------------------------------------------------------------------------- discord
@dataclass
class TriggerRule:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    enabled: bool = True
    event: str = "voice_connected"
    """voice_connected | voice_disconnected | channel_changed | self_mute |
    self_unmute | self_deafen | self_undeafen | user_joined | user_left |
    speaking_start | speaking_stop"""
    sound_id: str = ""
    delay_ms: int = 400
    cooldown_ms: int = 2000
    channel_filter: str = ""
    """Substring of the channel name, or the channel id. Empty = any."""
    user_filter: str = ""
    once_per_session: bool = False
    gain_db: float = 0.0
    note: str = ""


@dataclass
class DiscordSettings:
    enabled: bool = True
    detection: str = "auto"
    """auto | rpc | heuristic | off"""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_expires_at: float = 0.0
    auto_connect: bool = True
    reconnect_interval_s: float = 5.0
    heuristic_poll_s: float = 1.0
    heuristic_process_names: list[str] = field(
        default_factory=lambda: ["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe",
                                 "DiscordDevelopment.exe", "Discord", "discord"]
    )
    require_stable_ms: int = 1200
    """How long the connection must hold before fully-connected fires."""
    triggers: list[TriggerRule] = field(default_factory=list)
    log_events: bool = True


# --------------------------------------------------------------------------- ui/app
@dataclass
class UISettings:
    theme: str = "dark"
    accent: str = "#3d7dff"
    start_minimised: bool = False
    minimise_to_tray: bool = True
    autostart_engine: bool = True
    confirm_exit: bool = False
    window_geometry: str = ""
    last_tab: int = 0
    meter_fps: int = 30
    show_advanced: bool = False


@dataclass
class Config:
    version: int = CONFIG_VERSION
    devices: DeviceSettings = field(default_factory=DeviceSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    mixer: MixerSettings = field(default_factory=MixerSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    soundboard: SoundboardSettings = field(default_factory=SoundboardSettings)
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    ui: UISettings = field(default_factory=UISettings)

    # ---------------------------------------------------------------- helpers
    def sound_by_id(self, sid: str) -> SoundEntry | None:
        for s in self.soundboard.sounds:
            if s.id == sid:
                return s
        return None

    def fx(self, kind: str) -> dict:
        """Parameters for one effect, filling in any key a hand-edited file lacks."""
        base = _fx_defaults().get(kind, {})
        cur = self.voice.fx.get(kind)
        if not isinstance(cur, dict):
            self.voice.fx[kind] = dict(base)
            return self.voice.fx[kind]
        for k, v in base.items():
            cur.setdefault(k, v)
        return cur


# --------------------------------------------------------------------------- (de)serialise
def _is_dataclass_type(tp) -> bool:
    return dataclasses.is_dataclass(tp) and isinstance(tp, type)


def to_dict(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def from_dict(tp, data):
    """Rebuild ``tp`` from ``data``, tolerating missing, extra and mistyped keys."""
    if _is_dataclass_type(tp):
        if not isinstance(data, dict):
            return tp()
        hints = typing.get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name not in data:
                continue
            ftype = hints.get(f.name, f.type)
            try:
                kwargs[f.name] = from_dict(ftype, data[f.name])
            except Exception:
                _log.debug("dropping bad field %s.%s", tp.__name__, f.name)
        return tp(**kwargs)

    origin = typing.get_origin(tp)
    if origin is list:
        args = typing.get_args(tp)
        inner = args[0] if args else typing.Any
        if not isinstance(data, list):
            return []
        return [from_dict(inner, v) for v in data]
    if origin is dict:
        return dict(data) if isinstance(data, dict) else {}
    if origin is typing.Union:
        args = [a for a in typing.get_args(tp) if a is not type(None)]  # noqa: E721
        if len(args) == 1:
            return None if data is None else from_dict(args[0], data)
        return data

    if tp is bool:
        return bool(data)
    if tp is int:
        return int(data)
    if tp is float:
        return float(data)
    if tp is str:
        return "" if data is None else str(data)
    return data


def _migrate(raw: dict) -> dict:
    ver = int(raw.get("version", 1))
    if ver < 2:
        # v1 kept a flat fx_enabled switch; v2 moved it under voice.
        if "fx_enabled" in raw:
            raw.setdefault("voice", {})["enabled"] = bool(raw.pop("fx_enabled"))
    if ver < 3:
        # v2 stored the monitor mix as one bool; v3 splits it per source.
        mixer = raw.get("mixer")
        if isinstance(mixer, dict) and "monitor_all" in mixer:
            allv = bool(mixer.pop("monitor_all"))
            mixer.setdefault("monitor_mic", allv)
            mixer.setdefault("monitor_capture", allv)
    raw["version"] = CONFIG_VERSION
    return raw


def load(path: Path | None = None) -> Config:
    p = path or config_path()
    if not p.exists():
        _log.info("no config at %s - using defaults", p)
        return Config()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.error("config unreadable (%s) - falling back to defaults", exc)
        try:
            p.with_suffix(".broken.json").write_text(
                p.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
        return Config()
    try:
        cfg = from_dict(Config, _migrate(raw))
    except Exception as exc:
        _log.exception("config parse failed (%s)", exc)
        return Config()
    _log.info("loaded config from %s", p)
    return cfg


def save(cfg: Config, path: Path | None = None) -> None:
    """Atomic write, so a crash mid-save cannot leave a truncated config."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(to_dict(cfg), indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cfg", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
