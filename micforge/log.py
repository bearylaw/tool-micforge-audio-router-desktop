"""Central logging: rotating file + in-memory ring the UI can display."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()
_RING: collections.deque[str] = collections.deque(maxlen=2000)
_LISTENERS: list = []
_CONFIGURED = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        with _LOCK:
            _RING.append(line)
            listeners = list(_LISTENERS)
        for cb in listeners:
            try:
                cb(line, record.levelno)
            except Exception:
                pass


def log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "MicForge" / "logs"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "micforge" / "logs"


def setup(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    root = logging.getLogger("micforge")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.setLevel(level)
    root.addHandler(stream)

    ring = _RingHandler()
    ring.setFormatter(fmt)
    ring.setLevel(logging.DEBUG)
    root.addHandler(ring)

    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            d / "micforge.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        fileh.setLevel(logging.DEBUG)
        root.addHandler(fileh)
    except Exception:
        pass


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"micforge.{name}")


def history() -> list[str]:
    with _LOCK:
        return list(_RING)


def subscribe(cb) -> None:
    with _LOCK:
        _LISTENERS.append(cb)


def unsubscribe(cb) -> None:
    with _LOCK:
        if cb in _LISTENERS:
            _LISTENERS.remove(cb)
