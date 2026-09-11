"""Load audio files into memory as float32 at the engine sample rate.

Two decoders, tried in order:

* ``soundfile`` (libsndfile) -- WAV, FLAC, OGG, AIFF and, on recent builds, MP3.
* ``av`` (PyAV/FFmpeg) -- everything else, notably MP3, M4A/AAC and Opus.

Clips are decoded once and cached, keyed by path + mtime + samplerate, because
a soundboard button has to fire with no perceptible delay. A 30-second stereo
clip at 48 kHz is about 11 MB, so the cache is bounded by total bytes rather
than entry count.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import log
from ..audio.ring import resample_offline

_log = log.get("decode")

SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus",
                        ".wma", ".aiff", ".aif", ".oga", ".webm", ".mp4"}

MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 60 * 30


@dataclass
class Clip:
    data: np.ndarray
    """(frames, channels) float32."""
    samplerate: int
    path: str = ""

    @property
    def frames(self) -> int:
        return int(self.data.shape[0])

    @property
    def channels(self) -> int:
        return int(self.data.shape[1])

    @property
    def duration(self) -> float:
        return self.frames / max(self.samplerate, 1)

    @property
    def nbytes(self) -> int:
        return int(self.data.nbytes)


class DecodeError(RuntimeError):
    pass


_CACHE: dict[tuple, Clip] = {}
_CACHE_ORDER: list[tuple] = []
_CACHE_BYTES = 0
_LOCK = threading.Lock()


def _cache_get(key) -> Clip | None:
    with _LOCK:
        clip = _CACHE.get(key)
        if clip is not None:
            try:
                _CACHE_ORDER.remove(key)
            except ValueError:
                pass
            _CACHE_ORDER.append(key)
        return clip


def _cache_put(key, clip: Clip) -> None:
    global _CACHE_BYTES
    with _LOCK:
        if key in _CACHE:
            return
        _CACHE[key] = clip
        _CACHE_ORDER.append(key)
        _CACHE_BYTES += clip.nbytes
        while _CACHE_BYTES > MAX_CACHE_BYTES and len(_CACHE_ORDER) > 1:
            oldest = _CACHE_ORDER.pop(0)
            dropped = _CACHE.pop(oldest, None)
            if dropped is not None:
                _CACHE_BYTES -= dropped.nbytes


def clear_cache() -> None:
    global _CACHE_BYTES
    with _LOCK:
        _CACHE.clear()
        _CACHE_ORDER.clear()
        _CACHE_BYTES = 0


def cache_stats() -> tuple[int, int]:
    with _LOCK:
        return len(_CACHE), _CACHE_BYTES


# --------------------------------------------------------------------------- decoders
def _decode_soundfile(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return np.asarray(data, dtype=np.float32), int(sr)


def _decode_av(path: Path) -> tuple[np.ndarray, int]:
    import av

    with av.open(str(path)) as container:
        streams = [s for s in container.streams if s.type == "audio"]
        if not streams:
            raise DecodeError(f"{path.name} has no audio stream")
        stream = streams[0]
        stream.thread_type = "AUTO"
        sr = int(stream.codec_context.sample_rate or 48000)
        channels = int(getattr(stream.codec_context, "channels", 0) or 2)

        chunks: list[np.ndarray] = []
        total = 0
        limit = MAX_SECONDS * sr
        for frame in container.decode(stream):
            arr = frame.to_ndarray()
            # PyAV hands back (channels, samples) for planar formats and
            # (1, samples*channels) interleaved for packed ones.
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            if arr.shape[0] == 1 and channels > 1 and arr.shape[1] % channels == 0:
                arr = arr.reshape(-1, channels)
            else:
                arr = arr.T
            chunks.append(_to_float32(arr))
            total += arr.shape[0]
            if total > limit:
                _log.warning("%s is longer than %d s - truncating", path.name, MAX_SECONDS)
                break

    if not chunks:
        raise DecodeError(f"{path.name} decoded to nothing")
    return np.concatenate(chunks, axis=0), sr


def _to_float32(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.float32:
        return arr
    if arr.dtype == np.float64:
        return arr.astype(np.float32)
    if arr.dtype == np.int16:
        return arr.astype(np.float32) / 32768.0
    if arr.dtype == np.int32:
        return arr.astype(np.float32) / 2147483648.0
    if arr.dtype == np.uint8:
        return (arr.astype(np.float32) - 128.0) / 128.0
    return arr.astype(np.float32)


def load(path: str | Path, samplerate: int = 48000, use_cache: bool = True) -> Clip:
    """Decode ``path`` and resample to ``samplerate``."""
    p = Path(path)
    if not p.exists():
        raise DecodeError(f"file not found: {p}")
    if not p.is_file():
        raise DecodeError(f"not a file: {p}")

    try:
        mtime = p.stat().st_mtime_ns
        size = p.stat().st_size
    except OSError as exc:
        raise DecodeError(f"cannot stat {p}: {exc}") from exc

    key = (str(p.resolve()).lower(), mtime, size, int(samplerate))
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    errors: list[str] = []
    data = None
    src_sr = samplerate
    for name, fn in (("soundfile", _decode_soundfile), ("av", _decode_av)):
        try:
            data, src_sr = fn(p)
            break
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            data = None

    if data is None:
        raise DecodeError(f"could not decode {p.name} ({'; '.join(errors)})")

    if data.ndim == 1:
        data = data.reshape(-1, 1)
    if data.size == 0:
        raise DecodeError(f"{p.name} is empty")

    if src_sr != samplerate:
        data = resample_offline(data, src_sr, samplerate)

    # Guard against files that decode to something wild.
    np.nan_to_num(data, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 8.0:
        data = data / peak
        _log.warning("%s peaked at %.1f - normalised", p.name, peak)

    clip = Clip(data=np.ascontiguousarray(data, dtype=np.float32),
                samplerate=int(samplerate), path=str(p))
    if use_cache:
        _cache_put(key, clip)
    _log.info("decoded %s: %.2fs %d ch", p.name, clip.duration, clip.channels)
    return clip


def probe(path: str | Path) -> tuple[float, int, int]:
    """(duration_seconds, samplerate, channels) without a full decode where possible."""
    p = Path(path)
    try:
        import soundfile as sf

        info = sf.info(str(p))
        return float(info.duration), int(info.samplerate), int(info.channels)
    except Exception:
        pass
    try:
        import av

        with av.open(str(p)) as container:
            streams = [s for s in container.streams if s.type == "audio"]
            if streams:
                s = streams[0]
                dur = float(container.duration / 1_000_000) if container.duration else 0.0
                return (dur, int(s.codec_context.sample_rate or 0),
                        int(getattr(s.codec_context, "channels", 0) or 0))
    except Exception:
        pass
    return 0.0, 0, 0


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS
