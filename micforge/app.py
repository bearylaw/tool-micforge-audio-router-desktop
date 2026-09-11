"""Application controller: owns the config and every subsystem.

The UI talks to this and nothing else, so the whole app can be driven headless
(see ``__main__.py --headless``) and tested without Qt.
"""
from __future__ import annotations

import threading
import time

from . import config, hotkeys, log
from .audio import devices
from .audio.engine import AudioEngine
from .discordlink import fallback, rpc, triggers
from .sound.player import Soundboard

_log = log.get("app")

ACTION_STOP_ALL = "stop_all"
ACTION_PTT = "ptt"
ACTION_TOGGLE_MUTE = "toggle_mute"
ACTION_TOGGLE_FX = "toggle_fx"
ACTION_PANIC = "panic"
SOUND_PREFIX = "sound:"


class MicForgeApp:
    def __init__(self, cfg: config.Config | None = None):
        self.cfg = cfg or config.load()
        self.soundboard = Soundboard(self.cfg, self.cfg.devices.samplerate)
        self.engine = AudioEngine(self.cfg, self.soundboard)
        self.triggers = triggers.TriggerEngine(self.cfg, self.soundboard)
        self.rpc = rpc.DiscordRpc(self.cfg, on_event=self._on_voice_event,
                                  on_state=self._on_rpc_state)
        self.heuristic = fallback.HeuristicVoiceDetector(
            self.cfg, on_event=self._on_voice_event)
        self.hotkeys = hotkeys.HotkeyManager(on_press=self._on_hotkey_press,
                                             on_release=self._on_hotkey_release)

        self.on_event_log = None
        self.on_discord_state = None
        self.event_log: list[tuple[float, str]] = []
        self._save_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.engine.start()
        self.rebuild_hotkeys()
        if self.cfg.soundboard.hotkeys_enabled:
            self.hotkeys.start()
        self.start_discord()

    def shutdown(self) -> None:
        self.triggers.cancel_pending()
        self.stop_discord()
        self.hotkeys.stop()
        self.engine.stop()
        self.soundboard.panic()
        self._flush_save()

    # ---------------------------------------------------------------- config
    def save(self, delay: float = 1.0) -> None:
        """Debounced save - the UI calls this on every slider move."""
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            timer = threading.Timer(delay, self._flush_save)
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def _flush_save(self) -> None:
        with self._lock:
            self._save_timer = None
        try:
            config.save(self.cfg)
        except Exception as exc:
            _log.error("could not save the config: %s", exc)

    def apply_voice(self) -> None:
        self.engine.apply_settings()
        self.save()

    def apply_mixer(self) -> None:
        self.engine.apply_settings()
        self.save()

    # --------------------------------------------------------------- discord
    def start_discord(self) -> None:
        mode = self.cfg.discord.detection
        if not self.cfg.discord.enabled or mode == "off":
            return
        want_rpc = mode in ("rpc", "auto") and bool(self.cfg.discord.client_id)
        want_heuristic = mode in ("heuristic", "auto")

        if want_rpc:
            self.rpc.start()
        if want_heuristic and not (want_rpc and mode == "rpc"):
            # In auto mode both run: RPC gives channel names, the heuristic keeps
            # working if RPC is not set up or loses authorisation.
            self.heuristic.start()

    def stop_discord(self) -> None:
        self.rpc.stop()
        self.heuristic.stop()

    def restart_discord(self) -> None:
        self.stop_discord()
        self.start_discord()

    def discord_summary(self) -> str:
        mode = self.cfg.discord.detection
        if not self.cfg.discord.enabled or mode == "off":
            return "Discord integration is off"
        parts = []
        if self.rpc.running or self.rpc.state != "offline":
            parts.append(f"RPC: {self.rpc.summary()}")
        if self.heuristic.running:
            parts.append(f"Detector: {self.heuristic.summary()}")
        return " | ".join(parts) or "Starting..."

    def _on_voice_event(self, event: rpc.VoiceEvent) -> None:
        # In auto mode the RPC client is authoritative; ignore the heuristic
        # while RPC is healthy, or every join would fire the sound twice.
        if (event.source == "heuristic" and self.rpc.state == "authenticated"
                and self.cfg.discord.detection == "auto"):
            _log.debug("ignoring heuristic %s - RPC is live", event.kind)
            return
        self._log_event(event.describe())
        self.triggers.handle(event)

    def _on_rpc_state(self, state: str, error: str) -> None:
        self._log_event(f"Discord RPC: {state}" + (f" ({error})" if error else ""))
        cb = self.on_discord_state
        if cb is not None:
            try:
                cb(state, error)
            except Exception:
                pass

    def _log_event(self, text: str) -> None:
        self.event_log.append((time.time(), text))
        if len(self.event_log) > 300:
            del self.event_log[:-300]
        cb = self.on_event_log
        if cb is not None:
            try:
                cb(text)
            except Exception:
                pass

    # --------------------------------------------------------------- hotkeys
    def rebuild_hotkeys(self) -> list[str]:
        bindings: dict[str, str] = {}
        for entry in self.cfg.soundboard.sounds:
            if entry.hotkey:
                bindings[f"{SOUND_PREFIX}{entry.id}"] = entry.hotkey
        if self.cfg.soundboard.stop_all_hotkey:
            bindings[ACTION_STOP_ALL] = self.cfg.soundboard.stop_all_hotkey
        if self.cfg.mixer.ptt_hotkey:
            bindings[ACTION_PTT] = self.cfg.mixer.ptt_hotkey
        bad = self.hotkeys.set_bindings(bindings)
        self.hotkeys.enabled = self.cfg.soundboard.hotkeys_enabled
        return bad

    def _on_hotkey_press(self, action: str) -> None:
        if action.startswith(SOUND_PREFIX):
            self.soundboard.play_by_id(action[len(SOUND_PREFIX):])
        elif action == ACTION_STOP_ALL:
            self.soundboard.stop_all()
        elif action == ACTION_PTT:
            self.engine.ptt_down = True
        elif action == ACTION_TOGGLE_MUTE:
            self.cfg.mixer.mic_muted = not self.cfg.mixer.mic_muted
        elif action == ACTION_TOGGLE_FX:
            self.engine.chain.bypassed = not self.engine.chain.bypassed
        elif action == ACTION_PANIC:
            self.soundboard.panic()

    def _on_hotkey_release(self, action: str) -> None:
        if action == ACTION_PTT:
            self.engine.ptt_down = False

    # ----------------------------------------------------------------- setup
    def first_run_hints(self) -> list[str]:
        """Plain-language nudges shown on the dashboard until they are resolved."""
        hints: list[str] = []
        status = devices.virtual_cable_status()
        if not self.cfg.devices.virtual_out:
            if status.installed:
                hints.append(
                    f"Pick {status.output_name} as the MicForge output, then choose "
                    f"{status.input_name or 'the matching input'} as your microphone "
                    "in Discord.")
            else:
                # Short on purpose: the Mix tab shows the full install
                # instructions, and repeating them in the banner is just noise.
                hints.append("No virtual microphone yet - the Mix tab explains how to "
                             "set one up.")
        if not self.cfg.devices.monitor_out:
            hints.append("Choose a monitor device (your headphones) to hear your own "
                         "soundboard.")
        if not self.cfg.soundboard.sounds:
            hints.append("Add a few sounds on the Soundboard tab - drag MP3s straight "
                         "onto the grid.")
        elif not self.cfg.discord.triggers:
            hints.append("Add a trigger on the Discord tab to play a sound when you "
                         "join a voice channel.")
        return hints

    def status_line(self) -> str:
        st = self.engine.status
        if not st.running:
            return "Engine stopped"
        bits = [f"{st.samplerate // 1000} kHz", f"{st.latency_ms:.0f} ms",
                f"{st.cpu_percent:.0f}% cpu"]
        if st.capture_mode != "off":
            bits.append(f"capturing {st.capture_target}"
                        if st.capture_ok else "capture failed")
        if st.xruns:
            bits.append(f"{st.xruns} dropouts")
        return " | ".join(bits)
