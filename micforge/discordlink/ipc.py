"""Raw transport for the Discord client's local RPC socket.

The desktop client listens on ``discord-ipc-0`` .. ``discord-ipc-9``: a named
pipe on Windows, a unix socket under the runtime directory on Linux. Frames are
``<uint32 opcode LE><uint32 length LE><utf-8 json>``.

This layer knows nothing about voice channels -- it connects, frames messages
and hands parsed payloads to a callback.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

from .. import log

_log = log.get("discord.ipc")

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

MAX_FRAME = 64 * 1024 * 1024


class IpcError(RuntimeError):
    pass


def _candidate_paths() -> list[str]:
    """Every place a Discord IPC socket might live, most likely first."""
    if sys.platform == "win32":
        return [rf"\\?\pipe\discord-ipc-{i}" for i in range(10)]

    base_dirs: list[str] = []
    for var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(var)
        if value:
            base_dirs.append(value)
    base_dirs.append(tempfile.gettempdir())

    # Flatpak and Snap put the socket one or two directories deeper.
    nested = ["", "app/com.discordapp.Discord", "app/com.discordapp.DiscordCanary",
              "snap.discord", "snap.discord-canary", ".flatpak/com.discordapp.Discord/xdg-run"]

    seen: set[str] = set()
    out: list[str] = []
    for base in base_dirs:
        for sub in nested:
            folder = Path(base) / sub if sub else Path(base)
            for i in range(10):
                p = str(folder / f"discord-ipc-{i}")
                if p not in seen:
                    seen.add(p)
                    out.append(p)
    return out


class IpcConnection:
    """One connection to the local Discord client."""

    def __init__(self, on_message=None, on_disconnect=None):
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self._sock: socket.socket | None = None
        self._pipe = None
        self._path = ""
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self.connected = False

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------- connect
    def connect(self) -> bool:
        for path in _candidate_paths():
            try:
                if sys.platform == "win32":
                    self._open_pipe(path)
                else:
                    if not Path(path).exists():
                        continue
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect(path)
                    sock.settimeout(None)
                    self._sock = sock
                self._path = path
                self.connected = True
                self._stop.clear()
                self._reader = threading.Thread(target=self._read_loop,
                                                name="discord-ipc", daemon=True)
                self._reader.start()
                _log.info("connected to %s", path)
                return True
            except FileNotFoundError:
                continue
            except OSError as exc:
                # Pipe busy / permission: try the next index rather than giving up.
                _log.debug("%s not usable: %s", path, exc)
                continue
        return False

    def _open_pipe(self, path: str) -> None:
        import ctypes
        from ctypes import wintypes

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        INVALID_HANDLE = ctypes.c_void_p(-1).value

        # use_last_error is required for get_last_error() to report anything
        # real; without it the error code in the message is meaningless.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        handle = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                      OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE or handle is None:
            err = ctypes.get_last_error()
            raise FileNotFoundError(f"cannot open {path} (error {err})")
        self._pipe = handle

    # -------------------------------------------------------------- close
    def close(self) -> None:
        self._stop.set()
        self.connected = False
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._pipe is not None:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._pipe)
            except Exception:
                pass
            self._pipe = None
        reader = self._reader
        self._reader = None
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    # --------------------------------------------------------------- io
    def _write_raw(self, data: bytes) -> None:
        if self._sock is not None:
            self._sock.sendall(data)
            return
        if self._pipe is not None:
            import ctypes
            from ctypes import wintypes

            written = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.WriteFile(
                self._pipe, data, len(data), ctypes.byref(written), None)
            if not ok:
                raise IpcError("pipe write failed")
            return
        raise IpcError("not connected")

    def _read_raw(self, count: int) -> bytes:
        if count <= 0:
            return b""
        if self._sock is not None:
            chunks = []
            remaining = count
            while remaining > 0:
                chunk = self._sock.recv(remaining)
                if not chunk:
                    raise IpcError("socket closed")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        if self._pipe is not None:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_string_buffer(count)
            read = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.ReadFile(
                self._pipe, buf, count, ctypes.byref(read), None)
            if not ok or read.value == 0:
                raise IpcError("pipe closed")
            if read.value < count:
                return buf.raw[:read.value] + self._read_raw(count - read.value)
            return buf.raw[:count]
        raise IpcError("not connected")

    def send(self, opcode: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(body))
        with self._write_lock:
            self._write_raw(header + body)

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                header = self._read_raw(8)
                opcode, length = struct.unpack("<II", header)
                if length > MAX_FRAME:
                    raise IpcError(f"absurd frame length {length}")
                body = self._read_raw(length) if length else b""
                if opcode == OP_PING:
                    self.send(OP_PONG, json.loads(body or b"{}"))
                    continue
                if opcode == OP_CLOSE:
                    _log.info("Discord closed the connection")
                    break
                if opcode in (OP_FRAME, OP_HANDSHAKE):
                    try:
                        message = json.loads(body.decode("utf-8"))
                    except Exception:
                        continue
                    cb = self.on_message
                    if cb is not None:
                        try:
                            cb(message)
                        except Exception:
                            _log.exception("message handler raised")
        except Exception as exc:
            if not self._stop.is_set():
                _log.info("IPC read loop ended: %s", exc)
        finally:
            was_connected = self.connected
            self.connected = False
            if was_connected and not self._stop.is_set():
                cb = self.on_disconnect
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        pass


def discord_is_running() -> bool:
    """Cheap check: is any IPC socket present?"""
    if sys.platform == "win32":
        import ctypes

        for i in range(10):
            path = rf"\\?\pipe\discord-ipc-{i}"
            if ctypes.windll.kernel32.WaitNamedPipeW(path, 1):
                return True
        # A busy pipe reports "not found" by that call, so fall back to listing.
        try:
            return any(p.name.startswith("discord-ipc-")
                       for p in Path(r"\\.\pipe").iterdir())
        except Exception:
            return False
    return any(Path(p).exists() for p in _candidate_paths())
