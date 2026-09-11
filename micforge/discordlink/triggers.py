"""Turn voice events into soundboard hits.

Rules are evaluated in order, and every rule that matches fires -- so one join
can play a stinger *and* start a background loop if that is how you set it up.
Each rule carries its own delay and cooldown: the delay exists because Discord
reports ``VOICE_CONNECTED`` a beat before the other side has your stream
running, and the cooldown stops a flaky connection turning into a machine gun.
"""
from __future__ import annotations

import threading
import time

from .. import config, log
from .rpc import VoiceEvent

_log = log.get("discord.triggers")

EVENT_LABELS = {
    "voice_connected": "I fully join a voice channel",
    "voice_disconnected": "I leave a voice channel",
    "channel_changed": "I move to a different channel",
    "self_mute": "I mute myself",
    "self_unmute": "I unmute myself",
    "self_deafen": "I deafen myself",
    "self_undeafen": "I undeafen myself",
    "user_joined": "Someone joins my channel",
    "user_left": "Someone leaves my channel",
    "speaking_start": "Someone starts speaking",
    "speaking_stop": "Someone stops speaking",
}

# Which events the no-setup detector can actually see.
HEURISTIC_EVENTS = {"voice_connected", "voice_disconnected"}


class TriggerEngine:
    def __init__(self, cfg: config.Config, soundboard, on_fired=None):
        self.cfg = cfg
        self.soundboard = soundboard
        self.on_fired = on_fired
        self._last_fired: dict[str, float] = {}
        self._fired_this_session: set[str] = set()
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()
        self.enabled = True
        self.history: list[tuple[float, str]] = []

    # ------------------------------------------------------------- handling
    def handle(self, event: VoiceEvent) -> int:
        """Evaluate every rule against ``event``. Returns how many fired."""
        self._remember(event)
        if not self.enabled or not self.cfg.discord.enabled:
            return 0

        fired = 0
        for rule in list(self.cfg.discord.triggers):
            if self._matches(rule, event):
                if self._fire(rule, event):
                    fired += 1
        return fired

    def _matches(self, rule: config.TriggerRule, event: VoiceEvent) -> bool:
        if not rule.enabled or rule.event != event.kind:
            return False

        if rule.channel_filter:
            needle = rule.channel_filter.strip().lower()
            haystacks = [event.channel_name.lower(), event.channel_id.lower()]
            if not any(needle in h for h in haystacks if h):
                return False

        if rule.user_filter:
            needle = rule.user_filter.strip().lower()
            haystacks = [event.user_name.lower(), event.user_id.lower()]
            if not any(needle in h for h in haystacks if h):
                return False

        if rule.once_per_session and rule.id in self._fired_this_session:
            return False

        with self._lock:
            last = self._last_fired.get(rule.id, 0.0)
            if rule.cooldown_ms > 0 and (time.monotonic() - last) * 1000 < rule.cooldown_ms:
                _log.debug("rule %s still cooling down", rule.id)
                return False
        return True

    def _fire(self, rule: config.TriggerRule, event: VoiceEvent) -> bool:
        entry = self.cfg.sound_by_id(rule.sound_id)
        if entry is None:
            _log.warning("rule for %s points at a sound that no longer exists",
                         rule.event)
            return False

        with self._lock:
            self._last_fired[rule.id] = time.monotonic()
        if rule.once_per_session:
            self._fired_this_session.add(rule.id)

        delay = max(0, int(rule.delay_ms)) / 1000.0
        label = f"{EVENT_LABELS.get(rule.event, rule.event)} -> {entry.name}"

        def play():
            ok = self.soundboard.play(entry, rule.gain_db)
            _log.info("trigger %s: %s", "fired" if ok else "FAILED", label)
            cb = self.on_fired
            if cb is not None:
                try:
                    cb(rule, entry, ok)
                except Exception:
                    pass

        if delay <= 0:
            play()
        else:
            timer = threading.Timer(delay, play)
            timer.daemon = True
            timer.start()
            with self._lock:
                self._timers = [t for t in self._timers if t.is_alive()]
                self._timers.append(timer)
        return True

    # ---------------------------------------------------------------- admin
    def _remember(self, event: VoiceEvent) -> None:
        self.history.append((time.time(), event.describe()))
        if len(self.history) > 200:
            del self.history[:-200]

    def reset_session(self) -> None:
        self._fired_this_session.clear()
        with self._lock:
            self._last_fired.clear()

    def cancel_pending(self) -> None:
        with self._lock:
            timers, self._timers = self._timers, []
        for t in timers:
            try:
                t.cancel()
            except Exception:
                pass

    def test_rule(self, rule: config.TriggerRule) -> bool:
        """Fire a rule now, ignoring filters and cooldown - the Test button."""
        entry = self.cfg.sound_by_id(rule.sound_id)
        if entry is None:
            return False
        return self.soundboard.play(entry, rule.gain_db)

    def describe_rule(self, rule: config.TriggerRule) -> str:
        entry = self.cfg.sound_by_id(rule.sound_id)
        sound = entry.name if entry else "(missing sound)"
        bits = [f"When {EVENT_LABELS.get(rule.event, rule.event)}, play {sound}"]
        if rule.delay_ms:
            bits.append(f"after {rule.delay_ms} ms")
        if rule.channel_filter:
            bits.append(f"in channels matching '{rule.channel_filter}'")
        if rule.user_filter:
            bits.append(f"for users matching '{rule.user_filter}'")
        if rule.once_per_session:
            bits.append("once per session")
        return ", ".join(bits)


def default_join_rule(sound_id: str) -> config.TriggerRule:
    """The rule almost everyone wants first: a sound when you join a call."""
    return config.TriggerRule(
        event="voice_connected",
        sound_id=sound_id,
        delay_ms=600,
        cooldown_ms=3000,
        note="Plays once your voice connection is actually live.",
    )
