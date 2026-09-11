"""What can we capture? Applications, windows and audio sessions.

The user thinks in terms of "the game" or "that browser tab", so this module
turns the platform's various notions of a program into one list of
:class:`AudioSource` entries keyed by process id -- which is what both the
Windows process-loopback API and the PipeWire stream mover actually need.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from .. import log

_log = log.get("sources")

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

IS_WINDOWS = sys.platform == "win32"

# Processes that are never what someone means by "capture the game".
NOISE_NAMES = {
    "svchost.exe", "audiodg.exe", "dwm.exe", "explorer.exe", "sihost.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe",
    "applicationframehost.exe", "systemsettings.exe", "startmenuexperiencehost.exe",
    "runtimebroker.exe", "widgets.exe", "lockapp.exe", "python.exe", "pythonw.exe",
}


@dataclass
class AudioSource:
    pid: int
    name: str
    """Executable / application name."""
    label: str
    """What to show in the dropdown."""
    kind: str = "process"
    """session | window | stream | process"""
    window_title: str = ""
    playing: bool = False
    peak: float = 0.0
    stream_index: int = -1
    """PipeWire/Pulse sink-input index, Linux only."""
    extra: dict = field(default_factory=dict)

    @property
    def display(self) -> str:
        marker = " *" if self.playing else ""
        return f"{self.label}{marker}"


# --------------------------------------------------------------------------- windows
def _windows_audio_sessions() -> list[AudioSource]:
    """Apps that currently hold an audio session, with their live peak level."""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    except Exception as exc:
        _log.debug("pycaw unavailable: %s", exc)
        return []

    out: list[AudioSource] = []
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception as exc:
        _log.warning("cannot enumerate audio sessions: %s", exc)
        return []

    for session in sessions:
        proc = getattr(session, "Process", None)
        if proc is None:
            continue
        try:
            pid = int(proc.pid)
            name = proc.name()
        except Exception:
            continue

        peak = 0.0
        try:
            meter = session._ctl.QueryInterface(IAudioMeterInformation)
            peak = float(meter.GetPeakValue())
        except Exception:
            pass

        label = str(getattr(session, "DisplayName", "") or "").strip() or name
        out.append(AudioSource(pid=pid, name=name, label=label, kind="session",
                               playing=peak > 0.0005, peak=peak))
    return out


def _windows_windows() -> list[AudioSource]:
    """Visible top-level windows with a title, mapped to their process."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    results: list[AudioSource] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            name = ""
            if psutil is not None:
                try:
                    name = psutil.Process(pid.value).name()
                except Exception:
                    name = ""
            if name.lower() in NOISE_NAMES:
                return True
            results.append(AudioSource(pid=int(pid.value), name=name or title,
                                       label=title, kind="window",
                                       window_title=title))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(callback), 0)
    except Exception as exc:
        _log.warning("EnumWindows failed: %s", exc)
    return results


# --------------------------------------------------------------------------- linux
def _linux_streams() -> list[AudioSource]:
    from . import pulse

    if not pulse.available():
        return []
    out = []
    for si in pulse.list_sink_inputs():
        out.append(AudioSource(pid=si.pid, name=si.binary or si.app_name,
                               label=si.label, kind="stream", playing=True,
                               stream_index=si.index))
    return out


# --------------------------------------------------------------------------- public
def list_sources(include_windows: bool = True,
                 include_all_processes: bool = False) -> list[AudioSource]:
    """Everything capturable, best candidates first.

    Order is deliberate: things making noise right now, then other windows,
    then (optionally) every process. That way the game someone just alt-tabbed
    out of is at the top of the list.
    """
    if IS_WINDOWS:
        sessions = _windows_audio_sessions()
        by_pid = {s.pid: s for s in sessions}

        windows = _windows_windows() if include_windows else []
        for w in windows:
            existing = by_pid.get(w.pid)
            if existing is not None:
                # Prefer the window title: "Rocket League" beats "RL.exe".
                if existing.kind == "session" and w.window_title:
                    existing.window_title = w.window_title
                    existing.label = w.window_title
                continue
            by_pid[w.pid] = w

        if include_all_processes and psutil is not None:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pid = int(proc.info["pid"])
                    name = str(proc.info["name"] or "")
                except Exception:
                    continue
                if pid in by_pid or name.lower() in NOISE_NAMES:
                    continue
                by_pid[pid] = AudioSource(pid=pid, name=name, label=name,
                                          kind="process")

        items = list(by_pid.values())
    else:
        items = _linux_streams()
        if include_all_processes and psutil is not None:
            known = {s.pid for s in items}
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pid = int(proc.info["pid"])
                    name = str(proc.info["name"] or "")
                except Exception:
                    continue
                if pid in known or not name:
                    continue
                items.append(AudioSource(pid=pid, name=name, label=name,
                                         kind="process"))

    def sort_key(s: AudioSource):
        return (0 if s.playing else 1,
                0 if s.kind in ("session", "stream") else 1,
                s.label.lower())

    items.sort(key=sort_key)
    return items


def find_pid(name: str, exclude_pid: int = 0) -> int:
    """Locate a running process by executable name, for auto-reattach.

    Picks the process with the most memory, which for multi-process apps like
    Chrome or a game launcher is the one actually rendering audio far more
    often than the first pid returned.
    """
    if not name or psutil is None:
        return 0
    want = name.lower()
    best_pid, best_rss = 0, -1
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            pname = str(proc.info["name"] or "").lower()
            if pname != want:
                continue
            pid = int(proc.info["pid"])
            if pid == exclude_pid:
                continue
            rss = int(getattr(proc.info["memory_info"], "rss", 0) or 0)
        except Exception:
            continue
        if rss > best_rss:
            best_pid, best_rss = pid, rss
    return best_pid


def process_name(pid: int) -> str:
    if not pid or psutil is None:
        return ""
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def is_alive(pid: int) -> bool:
    if not pid or psutil is None:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False
