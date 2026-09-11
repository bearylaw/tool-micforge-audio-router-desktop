"""The audio engine: capture in, effects, mix, virtual mic and monitor out.

Design note -- why a dedicated mix thread rather than doing the work inside an
output callback. There are up to four independent clocks in play (microphone,
loopback capture, virtual cable, headphones) and no two of them tick together.
Driving the mix from one callback would starve the others. Instead every source
writes into a ring buffer, one thread mixes at a steady rate into two more ring
buffers, and each output callback just drains its own ring with drift
correction. The cost is about one extra block of latency; the benefit is that
any device can appear, vanish or run at its own rate without glitching the rest.

Signal flow per tick::

    mic ---> gain -> voice chain --+
                                   |
    loopback capture -> gain ------+--> duck -> sum -> limiter -> virtual mic
                                   |
    soundboard (mic bus) ----------+

    soundboard (monitor bus) + optional mic/capture -> monitor out
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .. import config, log
from ..dsp import db_to_lin
from ..dsp.chain import VoiceChain
from ..dsp.dynamics import Limiter
from . import devices, sources
from .ring import RingBuffer, StreamResampler, to_mono

_log = log.get("engine")

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None  # type: ignore

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    try:
        from . import wasapi
    except Exception as exc:  # pragma: no cover
        wasapi = None  # type: ignore
        _log.error("WASAPI backend unavailable: %s", exc)
else:
    wasapi = None  # type: ignore
    from . import pulse


@dataclass
class Levels:
    """Peak levels (linear 0..1) for the meters."""

    mic_in: float = 0.0
    mic_out: float = 0.0
    capture: float = 0.0
    soundboard: float = 0.0
    output: float = 0.0
    monitor: float = 0.0
    duck: float = 1.0
    limiter_gr_db: float = 0.0
    gate_gr_db: float = 0.0
    comp_gr_db: float = 0.0


@dataclass
class EngineStatus:
    running: bool = False
    samplerate: int = 48000
    blocksize: int = 240
    mic_device: str = ""
    out_device: str = ""
    monitor_device: str = ""
    capture_mode: str = "off"
    capture_target: str = ""
    capture_ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    xruns: int = 0
    ticks: int = 0
    latency_ms: float = 0.0
    cpu_percent: float = 0.0


class _OutputPath:
    """Ring -> device, resampling if the device will not take the engine rate."""

    def __init__(self, ring: RingBuffer, engine_sr: int, device_sr: int):
        self.ring = ring
        self.resampler = (None if engine_sr == device_sr
                          else StreamResampler(engine_sr, device_sr, 1))
        self._pending = np.zeros((0, 1), dtype=np.float32)

    def fill(self, frames: int, target_fill: int) -> np.ndarray:
        if self.resampler is None:
            return self.ring.read_drift_corrected(frames, target_fill)
        ratio = self.resampler.ratio
        guard = 0
        while self._pending.shape[0] < frames and guard < 8:
            need = int(np.ceil((frames - self._pending.shape[0]) * ratio)) + 4
            src = self.ring.read_drift_corrected(need, target_fill)
            conv = self.resampler.process(src)
            if conv.shape[0] == 0:
                guard += 1
                continue
            self._pending = np.concatenate((self._pending, conv))
        if self._pending.shape[0] < frames:
            pad = np.zeros((frames - self._pending.shape[0], 1), dtype=np.float32)
            self._pending = np.concatenate((self._pending, pad))
        out = self._pending[:frames]
        self._pending = self._pending[frames:]
        return out


class AudioEngine:
    def __init__(self, cfg: config.Config, soundboard):
        self.cfg = cfg
        self.soundboard = soundboard
        self.levels = Levels()
        self.status = EngineStatus()

        self.samplerate = int(cfg.devices.samplerate)
        self.blocksize = int(cfg.devices.blocksize)

        self.chain = VoiceChain(self.samplerate)
        self.chain.configure(cfg.voice, cfg.fx)
        # The capture path needs its own instance. Every stage is stateful, so
        # pushing two different signals through one chain in the same tick
        # interleaves the filter and delay memories and both come out wrong.
        self.capture_chain = VoiceChain(self.samplerate)
        self.capture_chain.configure(cfg.voice, cfg.fx)
        self._limiter = Limiter(self.samplerate)

        self._mic_ring = RingBuffer(self.samplerate * 2, 1)
        self._cap_ring = RingBuffer(self.samplerate * 2, 1)
        self._out_ring = RingBuffer(self.samplerate * 2, 1)
        self._mon_ring = RingBuffer(self.samplerate * 2, 1)

        self._mic_stream = None
        self._out_stream = None
        self._mon_stream = None
        self._mic_resampler: StreamResampler | None = None
        self._cap_resampler: StreamResampler | None = None
        self._out_path: _OutputPath | None = None
        self._mon_path: _OutputPath | None = None

        self._capture = None           # WasapiCapture (Windows)
        self._capture_stream = None    # sd.InputStream (Linux monitor source)
        self._linux_capture = None     # pulse.AppCapture

        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._duck_gain = 1.0
        self._prev_duck = 1.0
        self._tick_cost = 0.0
        self._timer_raised = False

        self.ptt_down = False
        """Set by the hotkey manager when a push-to-talk key is held."""
        self.on_status_change = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self.status.running:
            return True
        if sd is None:
            self.status.errors = ["sounddevice is not installed"]
            return False

        with self._lock:
            self._stop.clear()
            self.status = EngineStatus(samplerate=self.samplerate,
                                       blocksize=self.blocksize)
            self._raise_timer_resolution()

            devices.set_preferred_hostapi(self.cfg.devices.prefer_hostapi)
            self.samplerate = int(self.cfg.devices.samplerate)
            self.blocksize = max(32, int(self.cfg.devices.blocksize))
            self.chain = VoiceChain(self.samplerate)
            self.chain.configure(self.cfg.voice, self.cfg.fx)
            self.capture_chain = VoiceChain(self.samplerate)
            self.capture_chain.configure(self.cfg.voice, self.cfg.fx)
            self._limiter = Limiter(self.samplerate)
            self.soundboard.set_samplerate(self.samplerate)

            for ring in (self._mic_ring, self._cap_ring, self._out_ring, self._mon_ring):
                ring.clear()
                ring.reset_stats()

            self._open_output()
            self._open_monitor()
            self._open_mic()
            self.start_capture()

            self.status.running = True
            self.status.samplerate = self.samplerate
            self.status.blocksize = self.blocksize
            self._thread = threading.Thread(target=self._loop, name="micforge-engine",
                                            daemon=True)
            self._thread.start()
            self._watchdog = threading.Thread(target=self._watch, name="micforge-watch",
                                              daemon=True)
            self._watchdog.start()

        self._update_latency()
        _log.info("engine started: %d Hz, block %d (%.1f ms)", self.samplerate,
                  self.blocksize, 1000.0 * self.blocksize / self.samplerate)
        self._notify()
        return True

    def stop(self) -> None:
        if not self.status.running and self._thread is None:
            return
        self._stop.set()
        t, w = self._thread, self._watchdog
        self._thread = self._watchdog = None
        if t and t.is_alive():
            t.join(timeout=2.0)
        if w and w.is_alive():
            w.join(timeout=2.0)

        with self._lock:
            self.stop_capture()
            for name in ("_mic_stream", "_out_stream", "_mon_stream"):
                stream = getattr(self, name)
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
                    setattr(self, name, None)
            self.status.running = False
            self._release_timer_resolution()
        _log.info("engine stopped")
        self._notify()

    def restart(self) -> bool:
        self.stop()
        return self.start()

    # ------------------------------------------------------------ hot changes
    def apply_settings(self) -> None:
        """Re-read everything that does not need the streams reopened."""
        self.chain.configure(self.cfg.voice, self.cfg.fx)
        self.capture_chain.configure(self.cfg.voice, self.cfg.fx)
        self._limiter.set_params({
            "enabled": self.cfg.mixer.limiter_enabled,
            "ceiling_db": self.cfg.mixer.limiter_ceiling_db,
            "release_ms": self.cfg.mixer.limiter_release_ms,
        })
        self._update_latency()

    def needs_restart_for(self, old: config.DeviceSettings) -> bool:
        d = self.cfg.devices
        return (old.microphone != d.microphone or old.virtual_out != d.virtual_out
                or old.monitor_out != d.monitor_out or old.samplerate != d.samplerate
                or old.blocksize != d.blocksize or old.exclusive_mode != d.exclusive_mode)

    # --------------------------------------------------------------- streams
    def _resolve_channels(self, dev, wanted: int = 2) -> int:
        if dev is None:
            return wanted
        return max(1, min(wanted, dev.max_output or wanted))

    def _extra_settings(self, kind: str):
        if not IS_WINDOWS or sd is None:
            return None
        try:
            if self.cfg.devices.exclusive_mode:
                return sd.WasapiSettings(exclusive=True)
        except Exception:
            return None
        return None

    def _open_output(self) -> None:
        name = self.cfg.devices.virtual_out
        dev = devices.resolve(name, "output") if name else None
        if dev is None:
            if name:
                self.status.errors.append(
                    f"Virtual output '{name}' not found - nothing will reach Discord.")
            else:
                self.status.warnings.append(
                    "No virtual microphone selected yet, so nothing is sent to Discord.")
            return

        channels = self._resolve_channels(dev)
        sr = self.samplerate
        if not devices.supports(dev, sr, channels, "output"):
            sr = devices.best_samplerate(dev, sr, "output", channels)
            self.status.warnings.append(
                f"{dev.name} will not run at {self.samplerate} Hz; using {sr} Hz.")

        self._out_path = _OutputPath(self._out_ring, self.samplerate, sr)
        try:
            self._out_stream = sd.OutputStream(
                device=dev.index, channels=channels, samplerate=sr,
                dtype="float32", blocksize=0, latency=self.cfg.devices.output_latency,
                callback=self._out_callback, extra_settings=self._extra_settings("out"))
            self._out_stream.start()
            self.status.out_device = dev.name
            _log.info("virtual mic output: %s @ %d Hz %d ch", dev.name, sr, channels)
        except Exception as exc:
            self._out_stream = None
            self.status.errors.append(f"Cannot open {dev.name}: {exc}")
            _log.error("output open failed: %s", exc)

    def _open_monitor(self) -> None:
        if not self.cfg.mixer.monitor_enabled:
            return
        name = self.cfg.devices.monitor_out
        dev = devices.resolve(name, "output") if name else None
        if dev is None:
            if name:
                self.status.warnings.append(f"Monitor output '{name}' not found.")
            return
        if self.status.out_device and dev.name == self.status.out_device:
            self.status.warnings.append(
                "Monitor and virtual mic are the same device - monitoring disabled "
                "to avoid a feedback loop.")
            return

        channels = self._resolve_channels(dev)
        sr = self.samplerate
        if not devices.supports(dev, sr, channels, "output"):
            sr = devices.best_samplerate(dev, sr, "output", channels)
        self._mon_path = _OutputPath(self._mon_ring, self.samplerate, sr)
        try:
            self._mon_stream = sd.OutputStream(
                device=dev.index, channels=channels, samplerate=sr,
                dtype="float32", blocksize=0, latency=self.cfg.devices.output_latency,
                callback=self._mon_callback)
            self._mon_stream.start()
            self.status.monitor_device = dev.name
            _log.info("monitor output: %s @ %d Hz", dev.name, sr)
        except Exception as exc:
            self._mon_stream = None
            self.status.warnings.append(f"Cannot open monitor {dev.name}: {exc}")

    def _open_mic(self) -> None:
        if not self.cfg.devices.keep_alive and not self.cfg.mixer.mic_enabled:
            # keep_alive normally holds the stream open so muting and unmuting
            # is instant; turning it off releases the device for other apps.
            _log.info("microphone disabled and keep-alive off - not opening it")
            return
        name = self.cfg.devices.microphone
        dev = devices.resolve(name, "input") if name else devices.default_device("input")
        if dev is None:
            self.status.warnings.append("No microphone found.")
            return

        channels = max(1, min(2, dev.max_input or 1))
        sr = self.samplerate
        if not devices.supports(dev, sr, channels, "input"):
            sr = devices.best_samplerate(dev, sr, "input", channels)
            self.status.warnings.append(
                f"{dev.name} will not run at {self.samplerate} Hz; using {sr} Hz.")
        self._mic_resampler = (None if sr == self.samplerate
                               else StreamResampler(sr, self.samplerate, 1))
        try:
            self._mic_stream = sd.InputStream(
                device=dev.index, channels=channels, samplerate=sr,
                dtype="float32", blocksize=0, latency=self.cfg.devices.input_latency,
                callback=self._mic_callback)
            self._mic_stream.start()
            self.status.mic_device = dev.name
            _log.info("microphone: %s @ %d Hz %d ch", dev.name, sr, channels)
        except Exception as exc:
            self._mic_stream = None
            self.status.errors.append(f"Cannot open microphone {dev.name}: {exc}")
            _log.error("mic open failed: %s", exc)

    # ------------------------------------------------------------- callbacks
    def _mic_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.status.xruns += 1
        mode = self.cfg.devices.mic_channel_mode
        mono = to_mono(indata, "left" if mode == "left"
                       else "right" if mode == "right" else "downmix")
        if self._mic_resampler is not None:
            mono = self._mic_resampler.process(mono.reshape(-1, 1))[:, 0]
        self._mic_ring.write(mono)

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self.status.xruns += 1
        assert self._out_path is not None
        block = self._out_path.fill(frames, self.blocksize * 3)
        outdata[:] = block  # (frames, 1) broadcasts across the device channels

    def _mon_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self.status.xruns += 1
        assert self._mon_path is not None
        outdata[:] = self._mon_path.fill(frames, self.blocksize * 3)

    def _capture_audio(self, block: np.ndarray, sr: int) -> None:
        """Called from the WASAPI capture thread."""
        mono = to_mono(block, self.cfg.capture.stereo_to_mono)
        if sr != self.samplerate:
            if self._cap_resampler is None or self._cap_resampler.src_rate != sr:
                self._cap_resampler = StreamResampler(sr, self.samplerate, 1)
            mono = self._cap_resampler.process(mono.reshape(-1, 1))[:, 0]
        self._cap_ring.write(mono)

    def _linux_capture_callback(self, indata, frames, time_info, status) -> None:
        self._capture_audio(indata, self.samplerate)

    # ---------------------------------------------------------------- capture
    def start_capture(self) -> bool:
        cap = self.cfg.capture
        self.stop_capture()
        self.status.capture_mode = cap.mode
        self.status.capture_ok = False
        self.status.capture_target = ""
        if cap.mode == "off":
            return True

        if IS_WINDOWS:
            return self._start_capture_windows()
        return self._start_capture_linux()

    def _start_capture_windows(self) -> bool:
        if wasapi is None:
            self.status.warnings.append("WASAPI capture backend unavailable.")
            return False
        cap = self.cfg.capture
        pid = int(cap.process_pid)
        if cap.mode in ("process", "exclude"):
            if cap.auto_reattach and pid and not sources.is_alive(pid):
                found = sources.find_pid(cap.process_name)
                if found:
                    _log.info("target %s restarted: pid %d -> %d",
                              cap.process_name, pid, found)
                    pid = cap.process_pid = found
            if not pid:
                pid = sources.find_pid(cap.process_name)
                cap.process_pid = pid
            if not pid:
                self.status.warnings.append(
                    f"'{cap.process_name or 'target app'}' is not running.")
                return False
            if not wasapi.is_process_loopback_supported():
                self.status.warnings.append(
                    "This Windows build has no per-app capture; using whole desktop.")
                cap.mode = "desktop"

        device_id = ""
        if cap.mode == "desktop" and cap.endpoint:
            for did, name in wasapi.list_render_endpoints():
                if name == cap.endpoint:
                    device_id = did
                    break

        self._capture = wasapi.WasapiCapture(
            self._capture_audio, mode=cap.mode, pid=pid,
            exclude=(cap.mode == "exclude"), device_id=device_id,
            samplerate=self.samplerate)
        ok = self._capture.start()
        self.status.capture_ok = ok
        self.status.capture_target = (
            f"{cap.process_name or sources.process_name(pid)} (pid {pid})"
            if cap.mode in ("process", "exclude") else (cap.endpoint or "default output"))
        if not ok:
            msg = self._capture.error or "unknown error"
            self.status.warnings.append(f"Capture failed: {msg}")
            _log.warning("capture failed: %s", msg)
            if cap.mode in ("process", "exclude"):
                # Fall back so the user still gets audio, and say so.
                self._capture = wasapi.WasapiCapture(self._capture_audio,
                                                     mode="desktop",
                                                     samplerate=self.samplerate)
                if self._capture.start():
                    self.status.capture_ok = True
                    self.status.capture_target = "whole desktop (per-app failed)"
                    self.status.warnings.append("Fell back to whole-desktop capture.")
        return self.status.capture_ok

    def _start_capture_linux(self) -> bool:
        cap = self.cfg.capture
        if not pulse.available():
            self.status.warnings.append(
                "pactl not found - install pulseaudio-utils for system capture.")
            return False

        if cap.mode in ("process", "exclude"):
            if cap.mode == "exclude":
                self.status.warnings.append(
                    "Exclude mode is Windows-only; capturing the app instead.")
            self._linux_capture = pulse.capture_application(
                pid=cap.process_pid, binary=cap.process_name)
            source = self._linux_capture.monitor
            self.status.capture_target = cap.process_name or f"pid {cap.process_pid}"
        else:
            sink = cap.endpoint or pulse.default_sink()
            source = pulse.monitor_of(sink)
            self.status.capture_target = sink or "default sink"

        if not source:
            self.status.warnings.append("Could not work out which monitor to record.")
            return False

        dev = devices.resolve(source, "input")
        if dev is None:
            devices.refresh()
            dev = devices.resolve(source, "input")
        if dev is None:
            self.status.warnings.append(f"Monitor source '{source}' not visible to PortAudio.")
            return False

        try:
            self._capture_stream = sd.InputStream(
                device=dev.index, channels=min(2, max(1, dev.max_input)),
                samplerate=self.samplerate, dtype="float32", blocksize=0,
                callback=self._linux_capture_callback)
            self._capture_stream.start()
            self.status.capture_ok = True
            return True
        except Exception as exc:
            self.status.warnings.append(f"Cannot record {source}: {exc}")
            return False

    def stop_capture(self) -> None:
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None
        if self._capture_stream is not None:
            try:
                self._capture_stream.stop()
                self._capture_stream.close()
            except Exception:
                pass
            self._capture_stream = None
        if self._linux_capture is not None and not IS_WINDOWS:
            try:
                pulse.release_application(self._linux_capture)
            except Exception:
                pass
            self._linux_capture = None
        self._cap_ring.clear()
        self.status.capture_ok = False

    def set_capture_target(self, mode: str, pid: int = 0, name: str = "",
                           endpoint: str = "") -> bool:
        cap = self.cfg.capture
        cap.mode = mode
        cap.process_pid = int(pid)
        cap.process_name = name
        if endpoint:
            cap.endpoint = endpoint
        if not self.status.running:
            return True
        ok = self.start_capture()
        self._notify()
        return ok

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        period = self.blocksize / self.samplerate
        target_fill = self.blocksize * 3
        next_tick = time.perf_counter()
        idle = max(0.0002, period / 6.0)

        while not self._stop.is_set():
            did_work = False
            if self._out_stream is not None:
                if self._out_ring.available < target_fill:
                    self._tick()
                    did_work = True
            elif self._mon_stream is not None:
                if self._mon_ring.available < target_fill:
                    self._tick()
                    did_work = True
            else:
                now = time.perf_counter()
                if now >= next_tick:
                    self._tick()
                    did_work = True
                    next_tick += period
                    if now - next_tick > 0.25:  # we stalled; resync rather than sprint
                        next_tick = now + period
            if not did_work:
                time.sleep(idle)

    def _tick(self) -> None:
        started = time.perf_counter()
        n = self.blocksize
        mx = self.cfg.mixer
        target = n * 2

        # --- microphone
        mic = self._mic_ring.read_drift_corrected(n, target)[:, 0]
        self.levels.mic_in = float(np.max(np.abs(mic))) if mic.size else 0.0

        gate_open = True
        if mx.ptt_enabled:
            gate_open = (not self.ptt_down) if mx.ptt_inverted else self.ptt_down
        if mx.mic_enabled and not mx.mic_muted and gate_open:
            mic = mic * db_to_lin(mx.mic_gain_db)
            mic = self.chain.process(mic)
        else:
            mic = np.zeros(n, dtype=np.float32)
        self.levels.mic_out = float(np.max(np.abs(mic))) if mic.size else 0.0

        # --- system / app capture
        cap = self._cap_ring.read_drift_corrected(n, target)[:, 0]
        if mx.capture_enabled:
            cap = cap * db_to_lin(mx.capture_gain_db)
            if self.cfg.voice.apply_to_capture:
                cap = self.capture_chain.process(cap)
        else:
            cap = np.zeros(n, dtype=np.float32)
        self.levels.capture = float(np.max(np.abs(cap))) if cap.size else 0.0

        # --- soundboard
        sb_mic, sb_mon = self.soundboard.read(n)
        sb_gain = db_to_lin(mx.soundboard_gain_db)
        sb_mic = sb_mic * sb_gain
        sb_mon = sb_mon * sb_gain
        self.levels.soundboard = float(np.max(np.abs(sb_mic))) if sb_mic.size else 0.0

        # --- ducking
        duck = self._duck_curve(n, playing=self.soundboard.active_count > 0)
        if mx.duck_enabled:
            if mx.duck_mic:
                mic = mic * duck
            if mx.duck_capture:
                cap = cap * duck

        # --- mix to the virtual microphone
        out = mic + cap + sb_mic
        out = out * db_to_lin(mx.master_gain_db)
        if mx.limiter_enabled:
            out = self._limiter.process(out)
            self.levels.limiter_gr_db = self._limiter.gain_reduction_db
        else:
            np.clip(out, -1.0, 1.0, out=out)
        self.levels.output = float(np.max(np.abs(out))) if out.size else 0.0
        self._out_ring.write(out)

        # --- monitor mix
        mon = np.zeros(n, dtype=np.float32)
        if mx.monitor_mic:
            mon += mic
        if mx.monitor_capture:
            mon += cap
        if mx.monitor_soundboard:
            mon += sb_mon
        mon = mon * db_to_lin(mx.monitor_gain_db)
        np.clip(mon, -1.0, 1.0, out=mon)
        self.levels.monitor = float(np.max(np.abs(mon))) if mon.size else 0.0
        if self._mon_stream is not None:
            # With nothing draining it the ring would just churn and log
            # overflows, so only fill it when someone is listening.
            self._mon_ring.write(mon)

        meters = self.chain.meters()
        self.levels.gate_gr_db = meters.get("gate", 0.0)
        self.levels.comp_gr_db = meters.get("compressor", 0.0)

        self.status.ticks += 1
        cost = time.perf_counter() - started
        budget = n / self.samplerate
        self._tick_cost = 0.9 * self._tick_cost + 0.1 * (cost / budget)
        self.status.cpu_percent = round(self._tick_cost * 100.0, 1)

    def _duck_curve(self, n: int, playing: bool) -> np.ndarray:
        mx = self.cfg.mixer
        target = db_to_lin(mx.duck_depth_db) if playing else 1.0
        tau = (mx.duck_attack_ms if target < self._duck_gain else mx.duck_release_ms)
        block_ms = 1000.0 * n / self.samplerate
        alpha = 1.0 - float(np.exp(-block_ms / max(tau, 1.0)))
        prev = self._duck_gain
        self._duck_gain = prev + (target - prev) * alpha
        self.levels.duck = self._duck_gain
        return np.linspace(prev, self._duck_gain, n, dtype=np.float32)

    # -------------------------------------------------------------- watchdog
    def _watch(self) -> None:
        """Re-bind per-app capture when the target restarts."""
        while not self._stop.wait(self.cfg.capture.reattach_interval_s or 2.0):
            cap = self.cfg.capture
            if cap.mode not in ("process", "exclude") or not cap.auto_reattach:
                continue
            dead = cap.process_pid and not sources.is_alive(cap.process_pid)
            failed = self._capture is not None and not self._capture.running
            if not (dead or failed):
                continue
            new_pid = sources.find_pid(cap.process_name, exclude_pid=0)
            if not new_pid:
                continue
            if new_pid == cap.process_pid and not failed:
                continue
            _log.info("re-attaching capture to %s (pid %d)", cap.process_name, new_pid)
            cap.process_pid = new_pid
            with self._lock:
                self.start_capture()
            self._notify()

    # ----------------------------------------------------------------- misc
    def _update_latency(self) -> None:
        block_ms = 1000.0 * self.blocksize / max(self.samplerate, 1)
        chain_ms = 1000.0 * self.chain.latency / max(self.samplerate, 1)
        dev_ms = 0.0
        for stream in (self._mic_stream, self._out_stream):
            if stream is not None:
                try:
                    dev_ms += float(stream.latency) * 1000.0
                except Exception:
                    pass
        self.status.latency_ms = round(block_ms * 2 + chain_ms + dev_ms, 1)

    def _raise_timer_resolution(self) -> None:
        """Windows sleeps in 15.6 ms steps by default, which wrecks the pacing."""
        if not IS_WINDOWS or self._timer_raised:
            return
        try:
            import ctypes

            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_raised = True
        except Exception:
            pass

    def _release_timer_resolution(self) -> None:
        if not IS_WINDOWS or not self._timer_raised:
            return
        try:
            import ctypes

            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        self._timer_raised = False

    def _notify(self) -> None:
        cb = self.on_status_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def ring_health(self) -> dict:
        return {
            "mic": (self._mic_ring.available, self._mic_ring.underflows,
                    self._mic_ring.overflows),
            "capture": (self._cap_ring.available, self._cap_ring.underflows,
                        self._cap_ring.overflows),
            "out": (self._out_ring.available, self._out_ring.underflows,
                    self._out_ring.overflows),
            "monitor": (self._mon_ring.available, self._mon_ring.underflows,
                        self._mon_ring.overflows),
        }
