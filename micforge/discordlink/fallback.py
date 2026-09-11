"""Voice-call detection that needs no Discord application or authorisation.

Discord opens a UDP socket to a voice server for the duration of a call and
closes it when you leave. Watching for that gives a usable "am I in a call"
signal with zero setup -- no developer portal, no client secret, no approval
prompt. It cannot tell you *which* channel you joined or who else is in it, so
the RPC client is still the better path when configured; this exists so the
join-sound feature works out of the box.

Debounced in both directions, because the socket appears a moment before the
call is really up and can flicker when Discord moves you between servers.
"""
from __future__ import annotations

import ipaddress
import threading
import time

from .. import config, log
from .rpc import VoiceEvent

_log = log.get("discord.detect")

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore


def _is_external(host: str) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_unspecified)


class HeuristicVoiceDetector:
    """Polls Discord's sockets and emits connect/disconnect events."""

    def __init__(self, cfg: config.Config, on_event=None):
        self.cfg = cfg
        self.on_event = on_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.available = psutil is not None
        self.last_error = ""
        self.discord_running = False
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.available:
            self.last_error = "psutil is not installed"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discord-detect",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------------------------------------------------------- probe
    def _discord_pids(self) -> list[int]:
        wanted = {n.lower() for n in self.cfg.discord.heuristic_process_names}
        pids = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = str(proc.info["name"] or "").lower()
            except Exception:
                continue
            if name in wanted:
                pids.append(int(proc.info["pid"]))
        return pids

    def probe(self) -> bool:
        """True when Discord currently holds a UDP socket to a voice server."""
        pids = self._discord_pids()
        self.discord_running = bool(pids)
        if not pids:
            return False
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                # psutil renamed this in 6.0; 5.x only has connections().
                getter = getattr(proc, "net_connections", None) or proc.connections
                conns = getter(kind="udp")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as exc:
                self.last_error = str(exc)
                continue
            for conn in conns:
                raddr = getattr(conn, "raddr", None)
                if not raddr:
                    continue
                host = raddr[0] if isinstance(raddr, tuple) else getattr(raddr, "ip", "")
                if _is_external(str(host)):
                    return True
        return False

    # ----------------------------------------------------------------- loop
    def _run(self) -> None:
        interval = max(0.25, float(self.cfg.discord.heuristic_poll_s))
        needed = max(1, int(round(self.cfg.discord.require_stable_ms / 1000.0 / interval)))
        while not self._stop.wait(interval):
            try:
                in_call = self.probe()
            except Exception as exc:
                self.last_error = str(exc)
                continue

            if in_call:
                self._hits += 1
                self._misses = 0
            else:
                self._misses += 1
                self._hits = 0

            if not self.connected and self._hits >= needed:
                self.connected = True
                self._emit("voice_connected")
            elif self.connected and self._misses >= 2:
                self.connected = False
                self._emit("voice_disconnected")

    def _emit(self, kind: str) -> None:
        event = VoiceEvent(kind=kind, source="heuristic",
                           channel_name="(channel unknown - heuristic mode)")
        _log.info("heuristic detector: %s", kind)
        cb = self.on_event
        if cb is not None:
            try:
                cb(event)
            except Exception:
                _log.exception("event handler raised")

    def summary(self) -> str:
        if not self.available:
            return "Unavailable (psutil missing)"
        if not self.discord_running:
            return "Discord is not running"
        return "In a voice call" if self.connected else "Watching - not in a call"


def quick_check(cfg: config.Config | None = None) -> tuple[bool, bool]:
    """One-shot ``(discord_running, in_call)`` for the settings screen."""
    cfg = cfg or config.Config()
    det = HeuristicVoiceDetector(cfg)
    if not det.available:
        return False, False
    in_call = det.probe()
    return det.discord_running, in_call


if __name__ == "__main__":  # pragma: no cover - manual probe
    running, call = quick_check()
    print(f"discord running: {running}\nin a voice call: {call}")
    time.sleep(0)
