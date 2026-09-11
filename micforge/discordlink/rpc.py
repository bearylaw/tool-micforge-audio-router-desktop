"""Discord RPC client: knows when you are *fully* connected to a voice channel.

Joining a voice channel is not one event, it is a sequence. Discord first tells
you which channel was selected (``VOICE_CHANNEL_SELECT``), then walks through
``VOICE_CONNECTION_STATUS`` states -- AWAITING_ENDPOINT, AUTHENTICATING,
CONNECTING, ICE_CHECKING -- before landing on ``VOICE_CONNECTED``. Only that
last state means audio will actually be heard, which is why a join sound fired
on channel-select alone usually plays into a void.

So: the trigger fires on ``VOICE_CONNECTED``, held for
``require_stable_ms`` to filter out the brief reconnects Discord does when it
moves you between voice servers.

Scopes note: the ``rpc`` scope is normally allow-listed by Discord, but the
owner of an application (and their team) can always use it on their own
account. Since each user creates their own application for this, it works.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .. import config, log
from . import ipc, oauth

_log = log.get("discord.rpc")

# The states Discord walks through; only the last one means "you can be heard".
CONNECTED_STATE = "VOICE_CONNECTED"
DISCONNECTED_STATES = {"DISCONNECTED", "VOICE_DISCONNECTED", "NO_ROUTE"}
PENDING_STATES = {"AWAITING_ENDPOINT", "AUTHENTICATING", "CONNECTING",
                  "ICE_CHECKING", "VOICE_CONNECTING", "CONNECTED"}


@dataclass
class VoiceEvent:
    kind: str
    channel_id: str = ""
    channel_name: str = ""
    guild_id: str = ""
    guild_name: str = ""
    user_id: str = ""
    user_name: str = ""
    state: str = ""
    source: str = "rpc"
    at: float = field(default_factory=time.time)

    def describe(self) -> str:
        where = self.channel_name or self.channel_id or "?"
        who = self.user_name or self.user_id
        if self.kind in ("user_joined", "user_left", "speaking_start", "speaking_stop"):
            return f"{self.kind}: {who} in {where}"
        return f"{self.kind}: {where}" + (f" ({self.state})" if self.state else "")


class DiscordRpc:
    """Connects, authenticates, subscribes, and emits :class:`VoiceEvent`."""

    def __init__(self, cfg: config.Config, on_event=None, on_state=None):
        self.cfg = cfg
        self.on_event = on_event
        self.on_state = on_state

        self._conn: ipc.IpcConnection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: dict[str, dict] = {}
        self._waiters: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

        self.state = "offline"
        """offline | connecting | ready | authorising | authenticated | error"""
        self.last_error = ""
        self.user_name = ""
        self.current_channel_id = ""
        self.current_channel_name = ""
        self.current_guild_id = ""
        self.connected_to_voice = False
        self.last_connection_state = ""

        self._channel_subscriptions: set[tuple[str, str]] = set()
        self._stable_timer: threading.Timer | None = None
        self._self_mute = False
        self._self_deaf = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discord-rpc",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel_stable_timer()
        conn = self._conn
        if conn is not None:
            conn.close()
        self._conn = None
        t = self._thread
        self._thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._set_state("offline")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _set_state(self, state: str, error: str = "") -> None:
        if state == self.state and error == self.last_error:
            return
        self.state = state
        self.last_error = error
        cb = self.on_state
        if cb is not None:
            try:
                cb(state, error)
            except Exception:
                pass

    # ---------------------------------------------------------------- loop
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self.cfg.discord.client_id:
                self._set_state("error", "No Discord application ID set.")
                if self._stop.wait(5.0):
                    break
                continue

            self._set_state("connecting")
            conn = ipc.IpcConnection(on_message=self._on_message,
                                     on_disconnect=self._on_disconnect)
            if not conn.connect():
                self._set_state("offline", "Discord is not running.")
                wait = min(backoff, float(self.cfg.discord.reconnect_interval_s) or 5.0)
                if self._stop.wait(max(wait, 2.0)):
                    break
                backoff = min(backoff * 1.5, 30.0)
                continue

            self._conn = conn
            backoff = 1.0
            try:
                self._handshake()
                self._authenticate_flow()
                self._subscribe_global()
                self._prime_current_state()
            except Exception as exc:
                _log.warning("RPC setup failed: %s", exc)
                self._set_state("error", str(exc))
                conn.close()
                self._conn = None
                if self._stop.wait(float(self.cfg.discord.reconnect_interval_s) or 5.0):
                    break
                continue

            # Stay parked until the socket drops or we are told to stop.
            while not self._stop.is_set() and conn.connected:
                self._stop.wait(0.5)
            conn.close()
            self._conn = None
            if self._stop.is_set():
                break
            self._set_state("connecting", "Reconnecting to Discord...")
            self._stop.wait(float(self.cfg.discord.reconnect_interval_s) or 5.0)

    def _on_disconnect(self) -> None:
        self.connected_to_voice = False
        self._channel_subscriptions.clear()
        self._set_state("offline", "Lost the Discord connection.")

    # ------------------------------------------------------------ messaging
    def _send(self, payload: dict, opcode: int = ipc.OP_FRAME) -> None:
        conn = self._conn
        if conn is None or not conn.connected:
            raise ipc.IpcError("not connected to Discord")
        conn.send(opcode, payload)

    def _request(self, cmd: str, args: dict | None = None, evt: str | None = None,
                 timeout: float = 10.0) -> dict:
        """Send a command and block until the matching nonce comes back."""
        nonce = uuid.uuid4().hex
        payload: dict = {"cmd": cmd, "nonce": nonce}
        if args is not None:
            payload["args"] = args
        if evt is not None:
            payload["evt"] = evt

        event = threading.Event()
        with self._lock:
            self._waiters[nonce] = event
        try:
            self._send(payload)
            if not event.wait(timeout):
                raise TimeoutError(f"{cmd} timed out after {timeout:.0f}s")
            with self._lock:
                response = self._pending.pop(nonce, {})
        finally:
            with self._lock:
                self._waiters.pop(nonce, None)
                self._pending.pop(nonce, None)

        if response.get("evt") == "ERROR":
            data = response.get("data") or {}
            raise RuntimeError(str(data.get("message") or "Discord returned an error"))
        return response.get("data") or {}

    def _on_message(self, message: dict) -> None:
        nonce = message.get("nonce")
        if nonce:
            with self._lock:
                waiter = self._waiters.get(nonce)
                if waiter is not None:
                    self._pending[nonce] = message
                    waiter.set()
                    return

        evt = message.get("evt")
        if not evt:
            return
        data = message.get("data") or {}
        try:
            self._handle_event(evt, data)
        except Exception:
            _log.exception("failed handling %s", evt)

    # ----------------------------------------------------------------- auth
    def _handshake(self) -> None:
        conn = self._conn
        assert conn is not None
        ready = threading.Event()
        holder: dict = {}

        original = conn.on_message

        def first(message):
            if message.get("evt") == "READY":
                holder["ready"] = message
                conn.on_message = original
                ready.set()
                return
            original(message)

        conn.on_message = first
        conn.send(ipc.OP_HANDSHAKE, {"v": 1,
                                     "client_id": str(self.cfg.discord.client_id)})
        if not ready.wait(10.0):
            conn.on_message = original
            raise TimeoutError("Discord did not answer the handshake. "
                               "Is the application ID correct?")
        user = ((holder.get("ready") or {}).get("data") or {}).get("user") or {}
        self.user_name = str(user.get("username") or "")
        self._set_state("ready")
        _log.info("handshake ok (client user: %s)", self.user_name or "unknown")

    def _authenticate_flow(self) -> None:
        d = self.cfg.discord
        token = d.access_token

        if token and oauth.is_expired(d.token_expires_at) and d.refresh_token:
            try:
                fresh = oauth.refresh_token(d.client_id, d.client_secret, d.refresh_token)
                self._store_token(fresh)
                token = fresh["access_token"]
                _log.info("refreshed the Discord token")
            except oauth.OAuthError as exc:
                _log.warning("token refresh failed: %s", exc)
                token = ""

        if token:
            try:
                data = self._request("AUTHENTICATE", {"access_token": token})
                self._note_authenticated(data)
                return
            except Exception as exc:
                _log.info("stored token rejected (%s) - asking again", exc)

        if not d.client_secret:
            raise RuntimeError(
                "Not authorised yet. Add the application's Client Secret and press "
                "Authorise, then approve the prompt inside Discord.")

        self._set_state("authorising")
        auth = self._request("AUTHORIZE",
                             {"client_id": str(d.client_id), "scopes": oauth.SCOPES},
                             timeout=180.0)
        code = str(auth.get("code") or "")
        if not code:
            raise RuntimeError("Discord did not return an authorisation code.")
        fresh = oauth.exchange_code(d.client_id, d.client_secret, code)
        self._store_token(fresh)
        data = self._request("AUTHENTICATE", {"access_token": fresh["access_token"]})
        self._note_authenticated(data)

    def _store_token(self, fresh: dict) -> None:
        d = self.cfg.discord
        d.access_token = fresh["access_token"]
        d.refresh_token = fresh.get("refresh_token") or d.refresh_token
        d.token_expires_at = float(fresh.get("expires_at") or 0.0)
        try:
            config.save(self.cfg)
        except Exception as exc:
            _log.warning("could not persist the Discord token: %s", exc)

    def _note_authenticated(self, data: dict) -> None:
        user = data.get("user") or {}
        self.user_name = str(user.get("username") or self.user_name)
        self._set_state("authenticated")
        _log.info("authenticated as %s (token %s)", self.user_name,
                  oauth.redact(self.cfg.discord.access_token))

    # ------------------------------------------------------------ subscribe
    def _subscribe_global(self) -> None:
        for evt in ("VOICE_CHANNEL_SELECT", "VOICE_CONNECTION_STATUS",
                    "VOICE_SETTINGS_UPDATE"):
            try:
                self._request("SUBSCRIBE", {}, evt=evt)
                _log.debug("subscribed to %s", evt)
            except Exception as exc:
                _log.warning("cannot subscribe to %s: %s", evt, exc)

    def _subscribe_channel(self, channel_id: str) -> None:
        """Per-channel events, so we know who comes and goes around us."""
        self._unsubscribe_channels()
        if not channel_id:
            return
        for evt in ("VOICE_STATE_CREATE", "VOICE_STATE_DELETE",
                    "SPEAKING_START", "SPEAKING_STOP"):
            try:
                self._request("SUBSCRIBE", {"channel_id": channel_id}, evt=evt,
                              timeout=5.0)
                self._channel_subscriptions.add((evt, channel_id))
            except Exception as exc:
                _log.debug("cannot subscribe to %s on %s: %s", evt, channel_id, exc)

    def _unsubscribe_channels(self) -> None:
        for evt, channel_id in list(self._channel_subscriptions):
            try:
                self._request("UNSUBSCRIBE", {"channel_id": channel_id}, evt=evt,
                              timeout=3.0)
            except Exception:
                pass
        self._channel_subscriptions.clear()

    def _prime_current_state(self) -> None:
        """If we attach while already in a call, do not miss it."""
        try:
            data = self._request("GET_SELECTED_VOICE_CHANNEL", {}, timeout=5.0)
        except Exception:
            return
        if not data:
            return
        channel_id = str(data.get("id") or "")
        if not channel_id:
            return
        self.current_channel_id = channel_id
        self.current_channel_name = str(data.get("name") or "")
        self.current_guild_id = str(data.get("guild_id") or "")
        self._subscribe_channel(channel_id)
        _log.info("already in voice channel %s", self.current_channel_name or channel_id)

    # --------------------------------------------------------------- events
    def _handle_event(self, evt: str, data: dict) -> None:
        if evt == "VOICE_CHANNEL_SELECT":
            self._on_channel_select(data)
        elif evt == "VOICE_CONNECTION_STATUS":
            self._on_connection_status(data)
        elif evt == "VOICE_SETTINGS_UPDATE":
            self._on_voice_settings(data)
        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_DELETE"):
            user = (data.get("user") or {})
            self._emit(VoiceEvent(
                kind="user_joined" if evt == "VOICE_STATE_CREATE" else "user_left",
                channel_id=self.current_channel_id,
                channel_name=self.current_channel_name,
                guild_id=self.current_guild_id,
                user_id=str(user.get("id") or ""),
                user_name=str(user.get("username") or user.get("global_name") or "")))
        elif evt in ("SPEAKING_START", "SPEAKING_STOP"):
            self._emit(VoiceEvent(
                kind="speaking_start" if evt == "SPEAKING_START" else "speaking_stop",
                channel_id=self.current_channel_id,
                channel_name=self.current_channel_name,
                user_id=str(data.get("user_id") or "")))

    def _on_channel_select(self, data: dict) -> None:
        channel_id = str(data.get("channel_id") or "")
        previous = self.current_channel_id

        if not channel_id:
            self.current_channel_id = ""
            self.current_channel_name = ""
            self._unsubscribe_channels()
            self._cancel_stable_timer()
            if self.connected_to_voice:
                self.connected_to_voice = False
                self._emit(VoiceEvent(kind="voice_disconnected", channel_id=previous,
                                      channel_name=self.current_channel_name))
            return

        self.current_channel_id = channel_id
        self.current_guild_id = str(data.get("guild_id") or "")
        self.current_channel_name = self._channel_name(channel_id)
        self._subscribe_channel(channel_id)

        if previous and previous != channel_id and self.connected_to_voice:
            self._emit(VoiceEvent(kind="channel_changed", channel_id=channel_id,
                                  channel_name=self.current_channel_name,
                                  guild_id=self.current_guild_id))

    def _on_connection_status(self, data: dict) -> None:
        state = str(data.get("state") or "")
        self.last_connection_state = state
        if state == CONNECTED_STATE:
            self._arm_stable_timer()
        elif state in DISCONNECTED_STATES:
            self._cancel_stable_timer()
            if self.connected_to_voice:
                self.connected_to_voice = False
                self._emit(VoiceEvent(kind="voice_disconnected",
                                      channel_id=self.current_channel_id,
                                      channel_name=self.current_channel_name,
                                      state=state))

    def _arm_stable_timer(self) -> None:
        if self.connected_to_voice:
            return
        self._cancel_stable_timer()
        delay = max(0.0, float(self.cfg.discord.require_stable_ms) / 1000.0)

        def fire():
            if self.last_connection_state != CONNECTED_STATE or self._stop.is_set():
                return
            self.connected_to_voice = True
            if not self.current_channel_name and self.current_channel_id:
                self.current_channel_name = self._channel_name(self.current_channel_id)
            self._emit(VoiceEvent(kind="voice_connected",
                                  channel_id=self.current_channel_id,
                                  channel_name=self.current_channel_name,
                                  guild_id=self.current_guild_id,
                                  state=CONNECTED_STATE))

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        self._stable_timer = timer
        timer.start()

    def _cancel_stable_timer(self) -> None:
        timer = self._stable_timer
        self._stable_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _on_voice_settings(self, data: dict) -> None:
        mute = bool(data.get("mute", self._self_mute))
        deaf = bool(data.get("deaf", self._self_deaf))
        if mute != self._self_mute:
            self._self_mute = mute
            self._emit(VoiceEvent(kind="self_mute" if mute else "self_unmute",
                                  channel_id=self.current_channel_id,
                                  channel_name=self.current_channel_name))
        if deaf != self._self_deaf:
            self._self_deaf = deaf
            self._emit(VoiceEvent(kind="self_deafen" if deaf else "self_undeafen",
                                  channel_id=self.current_channel_id,
                                  channel_name=self.current_channel_name))

    def _channel_name(self, channel_id: str) -> str:
        try:
            data = self._request("GET_CHANNEL", {"channel_id": channel_id}, timeout=5.0)
            return str(data.get("name") or "")
        except Exception:
            return ""

    def _emit(self, event: VoiceEvent) -> None:
        if self.cfg.discord.log_events:
            _log.info("event: %s", event.describe())
        cb = self.on_event
        if cb is not None:
            try:
                cb(event)
            except Exception:
                _log.exception("event handler raised")

    # ----------------------------------------------------------------- misc
    def forget_authorisation(self) -> None:
        d = self.cfg.discord
        if d.access_token and d.client_id and d.client_secret:
            oauth.revoke(d.client_id, d.client_secret, d.access_token)
        d.access_token = ""
        d.refresh_token = ""
        d.token_expires_at = 0.0
        config.save(self.cfg)
        _log.info("cleared the stored Discord authorisation")

    def summary(self) -> str:
        if self.state == "authenticated":
            where = self.current_channel_name or self.current_channel_id
            if self.connected_to_voice and where:
                return f"Connected as {self.user_name} - in voice: {where}"
            return f"Connected as {self.user_name} - not in a voice channel"
        if self.state == "ready":
            return "Talking to Discord, not authorised yet"
        if self.state == "authorising":
            return "Waiting for you to approve the prompt in Discord"
        if self.last_error:
            return self.last_error
        return "Not connected"
