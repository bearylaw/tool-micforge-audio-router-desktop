"""Polyphonic soundboard playback.

Each triggered clip becomes a :class:`Voice` with its own read position, gain
envelope and routing. The engine pulls one block per tick and gets back two
mixes: what goes out of the virtual microphone, and what you hear locally.
Keeping those separate is the whole point -- you almost always want to hear the
clip yourself, and sometimes you want a clip that only you hear.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .. import config, log
from ..dsp import db_to_lin
from . import decode

_log = log.get("soundboard")


class Voice:
    """One playing instance of a clip."""

    __slots__ = ("clip", "entry_id", "name", "gain", "loop", "to_mic", "to_monitor",
                 "rate", "pos", "start_frame", "end_frame", "fade_in", "fade_out",
                 "_faded_in", "_releasing", "_release_left", "_release_total",
                 "finished", "started_at", "_mono")

    def __init__(self, clip: decode.Clip, entry: config.SoundEntry,
                 samplerate: int, gain_db: float = 0.0):
        self.clip = clip
        self.entry_id = entry.id
        self.name = entry.name
        self.gain = db_to_lin(entry.gain_db + gain_db)
        self.loop = bool(entry.loop)
        self.to_mic = bool(entry.to_mic)
        self.to_monitor = bool(entry.to_monitor)

        # Pitch on a soundboard clip is a tape-speed change: it shortens the
        # clip too. That is what people expect from a pitch knob on a sample.
        semis = float(entry.pitch_semitones)
        self.rate = max(0.05, float(entry.speed) * (2.0 ** (semis / 12.0)))

        frames = clip.frames
        self.start_frame = int(np.clip(entry.start_ms * samplerate / 1000.0, 0,
                                       max(frames - 1, 0)))
        end = entry.end_ms
        self.end_frame = frames if end <= 0 else int(
            np.clip(end * samplerate / 1000.0, self.start_frame + 1, frames))

        self.fade_in = max(0.0, entry.fade_in_ms) * samplerate / 1000.0
        self.fade_out = max(0.0, entry.fade_out_ms) * samplerate / 1000.0

        self.pos = float(self.start_frame)
        self._faded_in = 0.0
        self._releasing = False
        self._release_left = 0.0
        self._release_total = max(self.fade_out, 1.0)
        self.finished = False
        self.started_at = time.monotonic()

        # Mono is what the engine mixes in; Discord is mono anyway.
        data = clip.data
        self._mono = data[:, 0] if data.shape[1] == 1 else data.mean(axis=1)

    # ------------------------------------------------------------------ state
    @property
    def length(self) -> int:
        return max(self.end_frame - self.start_frame, 1)

    @property
    def progress(self) -> float:
        if self.finished:
            return 1.0
        return float(np.clip((self.pos - self.start_frame) / self.length, 0.0, 1.0))

    def release(self, fade_ms: float | None = None, samplerate: int = 48000) -> None:
        """Start the fade-out instead of cutting, so stopping never clicks."""
        if self._releasing:
            return
        self._releasing = True
        total = (self.fade_out if fade_ms is None
                 else max(fade_ms, 0.0) * samplerate / 1000.0)
        self._release_total = max(total, 1.0)
        self._release_left = self._release_total
        if total <= 0:
            self.finished = True

    def stop_now(self) -> None:
        self.finished = True

    # ------------------------------------------------------------------- read
    def read(self, n: int) -> np.ndarray:
        """Next ``n`` frames, already gain-staged. Zero-padded at the end."""
        if self.finished or n <= 0:
            return np.zeros(n, dtype=np.float32)

        idx = self.pos + self.rate * np.arange(n, dtype=np.float64)

        if self.loop and not self._releasing:
            span = float(self.length)
            idx = self.start_frame + np.mod(idx - self.start_frame, span)
            self.pos = float(self.start_frame
                             + np.mod(self.pos + self.rate * n - self.start_frame, span))
            ended = 0
        else:
            past = idx >= self.end_frame - 1
            ended = int(np.argmax(past)) if np.any(past) else n
            idx = np.clip(idx, self.start_frame, self.end_frame - 1)
            self.pos += self.rate * n

        i0 = np.floor(idx).astype(np.int64)
        i1 = np.minimum(i0 + 1, self.clip.frames - 1)
        frac = (idx - i0).astype(np.float32)
        out = (self._mono[i0] * (1.0 - frac) + self._mono[i1] * frac).astype(np.float32)

        if ended < n:
            out[ended:] = 0.0

        # Fade in.
        if self.fade_in > 0 and self._faded_in < self.fade_in:
            remaining = self.fade_in - self._faded_in
            k = int(min(n, remaining))
            if k > 0:
                ramp = (self._faded_in + np.arange(k, dtype=np.float32)) / self.fade_in
                out[:k] *= np.clip(ramp, 0.0, 1.0)
            self._faded_in += n

        # Fade out / release.
        if self._releasing and self._release_left > 0:
            k = int(min(n, self._release_left))
            ramp = np.ones(n, dtype=np.float32)
            if k > 0:
                start = self._release_left / self._release_total
                end = max(self._release_left - k, 0) / self._release_total
                ramp[:k] = np.linspace(start, end, k, dtype=np.float32)
            ramp[k:] = 0.0
            out *= ramp
            self._release_left -= n
            if self._release_left <= 0:
                self.finished = True
        elif not self.loop and ended < n:
            self.finished = True
        elif not self.loop and self.pos >= self.end_frame:
            self.finished = True

        return out * self.gain


class Soundboard:
    """Voice pool with separate mic and monitor busses."""

    def __init__(self, cfg: config.Config, samplerate: int = 48000):
        self.cfg = cfg
        self.samplerate = int(samplerate)
        self._voices: list[Voice] = []
        self._lock = threading.Lock()
        self.on_change = None
        """Optional callback fired when the active voice list changes."""
        self.last_error = ""

    # ------------------------------------------------------------- triggering
    def play(self, entry: config.SoundEntry, gain_db: float = 0.0) -> bool:
        if not entry or not entry.path:
            self.last_error = "sound has no file"
            return False
        try:
            clip = decode.load(entry.path, self.samplerate)
        except Exception as exc:
            self.last_error = str(exc)
            _log.error("cannot play %s: %s", entry.name, exc)
            return False

        sb = self.cfg.soundboard
        with self._lock:
            if entry.stop_others:
                for v in self._voices:
                    v.release(30.0, self.samplerate)
            elif not sb.overlap or sb.restart_on_retrigger:
                for v in self._voices:
                    if v.entry_id == entry.id:
                        v.release(20.0, self.samplerate)

            live = [v for v in self._voices if not v.finished]
            if len(live) >= max(1, sb.max_voices):
                # Steal the oldest rather than refusing to play; a soundboard
                # that silently ignores a button feels broken.
                oldest = min(live, key=lambda v: v.started_at)
                oldest.release(20.0, self.samplerate)

            self._voices.append(Voice(clip, entry, self.samplerate,
                                      gain_db + sb.global_gain_db))
        self.last_error = ""
        _log.info("playing %s", entry.name)
        self._notify()
        return True

    def play_by_id(self, sound_id: str, gain_db: float = 0.0) -> bool:
        entry = self.cfg.sound_by_id(sound_id)
        if entry is None:
            self.last_error = f"no sound with id {sound_id}"
            return False
        return self.play(entry, gain_db)

    def stop_entry(self, entry_id: str) -> None:
        with self._lock:
            for v in self._voices:
                if v.entry_id == entry_id:
                    v.release(samplerate=self.samplerate)
        self._notify()

    def stop_all(self, fade_ms: float = 25.0) -> None:
        with self._lock:
            for v in self._voices:
                v.release(fade_ms, self.samplerate)
        self._notify()

    def panic(self) -> None:
        """Cut everything immediately - the big red button."""
        with self._lock:
            self._voices.clear()
        self._notify()

    # ----------------------------------------------------------------- status
    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for v in self._voices if not v.finished)

    def active(self) -> list[tuple[str, str, float]]:
        with self._lock:
            return [(v.entry_id, v.name, v.progress)
                    for v in self._voices if not v.finished]

    def is_playing(self, entry_id: str = "") -> bool:
        with self._lock:
            for v in self._voices:
                if v.finished:
                    continue
                if not entry_id or v.entry_id == entry_id:
                    return True
        return False

    def _notify(self) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # ------------------------------------------------------------------- read
    def read(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """``(to_mic, to_monitor)`` mixes for the next ``n`` frames."""
        mic = np.zeros(n, dtype=np.float32)
        mon = np.zeros(n, dtype=np.float32)
        finished_any = False

        with self._lock:
            if not self._voices:
                return mic, mon
            for v in self._voices:
                if v.finished:
                    finished_any = True
                    continue
                block = v.read(n)
                if v.to_mic:
                    mic += block
                if v.to_monitor:
                    mon += block
                if v.finished:
                    finished_any = True
            if finished_any:
                self._voices = [v for v in self._voices if not v.finished]

        if finished_any:
            self._notify()
        return mic, mon

    def set_samplerate(self, samplerate: int) -> None:
        if int(samplerate) == self.samplerate:
            return
        self.samplerate = int(samplerate)
        self.panic()
        decode.clear_cache()

    def preload(self) -> tuple[int, list[str]]:
        """Decode every configured sound up front. Returns (ok, errors)."""
        ok, errors = 0, []
        for entry in list(self.cfg.soundboard.sounds):
            if not entry.path:
                continue
            try:
                decode.load(entry.path, self.samplerate)
                ok += 1
            except Exception as exc:
                errors.append(f"{entry.name}: {exc}")
        return ok, errors
