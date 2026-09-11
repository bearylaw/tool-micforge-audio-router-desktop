"""Device discovery, host-API preference and virtual-cable detection."""
from __future__ import annotations

import sys
from dataclasses import dataclass

from .. import log

_log = log.get("devices")

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - import guard
    sd = None  # type: ignore
    _log.error("sounddevice unavailable: %s", exc)

# Best host API first. WASAPI gives the lowest latency on Windows and reports
# untruncated device names; MME truncates to 31 characters and adds ~100 ms.
HOSTAPI_PREFERENCE = {
    "win32": ["Windows WASAPI", "Windows DirectSound", "Windows WDM-KS", "MME"],
    "linux": ["ALSA", "JACK Audio Connection Kit", "OSS"],
    "darwin": ["Core Audio"],
}

# Substrings that identify a device as a virtual cable rather than real hardware.
VIRTUAL_OUTPUT_HINTS = [
    "cable input",          # VB-CABLE
    "vb-audio",
    "cable-a input", "cable-b input", "cable-c input", "cable-d input",
    "voicemeeter input", "voicemeeter aux input", "voicemeeter vaio3 input",
    "virtual audio cable", "line 1 (virtual", "line 2 (virtual",
    "micforge",             # our own PipeWire sink
    "null sink", "virtual_sink",
]

VIRTUAL_INPUT_HINTS = [
    "cable output",
    "voicemeeter output", "voicemeeter aux output",
    "micforge",
]


@dataclass
class DeviceInfo:
    index: int
    name: str
    hostapi: int
    hostapi_name: str
    max_input: int
    max_output: int
    default_samplerate: float
    is_default_input: bool = False
    is_default_output: bool = False

    @property
    def label(self) -> str:
        return f"{self.name}  [{self.hostapi_name}]"

    @property
    def is_virtual_output(self) -> bool:
        low = self.name.lower()
        return any(h in low for h in VIRTUAL_OUTPUT_HINTS)

    @property
    def is_virtual_input(self) -> bool:
        low = self.name.lower()
        return any(h in low for h in VIRTUAL_INPUT_HINTS)


def _platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return "win32"


def refresh() -> None:
    """Re-scan the device list (call after a device is plugged in or removed)."""
    if sd is None:
        return
    try:
        sd._terminate()
        sd._initialize()
        _log.info("device list refreshed")
    except Exception as exc:
        _log.warning("device refresh failed: %s", exc)


def _all() -> list[DeviceInfo]:
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception as exc:
        _log.error("cannot query devices: %s", exc)
        return []

    out: list[DeviceInfo] = []
    for i, d in enumerate(devices):
        api = int(d["hostapi"])
        out.append(DeviceInfo(
            index=i,
            name=str(d["name"]).strip(),
            hostapi=api,
            hostapi_name=str(hostapis[api]["name"]) if api < len(hostapis) else "?",
            max_input=int(d["max_input_channels"]),
            max_output=int(d["max_output_channels"]),
            default_samplerate=float(d["default_samplerate"]),
            is_default_input=(i == default_in),
            is_default_output=(i == default_out),
        ))
    return out


_FORCED_HOSTAPI = ""


def set_preferred_hostapi(name: str) -> None:
    """Force one host API to the top of the ranking.

    The built-in order is right for almost everyone, but someone with an ASIO
    or JACK setup, or a device that only behaves under DirectSound, needs the
    override.
    """
    global _FORCED_HOSTAPI
    _FORCED_HOSTAPI = "" if (name or "auto").strip().lower() == "auto" else name.strip()
    if _FORCED_HOSTAPI:
        _log.info("host API forced to %s", _FORCED_HOSTAPI)


def available_hostapis() -> list[str]:
    seen: list[str] = []
    for dev in _all():
        if dev.hostapi_name not in seen:
            seen.append(dev.hostapi_name)
    return seen


def _rank(dev: DeviceInfo) -> tuple[int, int]:
    if _FORCED_HOSTAPI and dev.hostapi_name.lower() == _FORCED_HOSTAPI.lower():
        return -1, dev.index
    prefs = HOSTAPI_PREFERENCE.get(_platform_key(), [])
    try:
        rank = prefs.index(dev.hostapi_name)
    except ValueError:
        rank = len(prefs)
    return rank, dev.index


def input_devices(dedupe: bool = True) -> list[DeviceInfo]:
    devs = [d for d in _all() if d.max_input > 0]
    return _dedupe(sorted(devs, key=_rank)) if dedupe else devs


def output_devices(dedupe: bool = True) -> list[DeviceInfo]:
    devs = [d for d in _all() if d.max_output > 0]
    return _dedupe(sorted(devs, key=_rank)) if dedupe else devs


def _dedupe(devs: list[DeviceInfo]) -> list[DeviceInfo]:
    """One entry per physical device, keeping the best host API.

    Windows lists the same speakers under four host APIs; showing all of them
    makes the dropdown useless.
    """
    seen: dict[str, DeviceInfo] = {}
    for d in devs:
        key = d.name.lower().strip()
        if key not in seen:
            seen[key] = d
    return list(seen.values())


def resolve(name: str, kind: str = "output") -> DeviceInfo | None:
    """Find a device by name.

    Matching is deliberately forgiving: exact, then case-insensitive, then
    prefix, then substring. Windows renames devices when you move a USB port
    and MME truncates names, so an exact match alone strands users.
    """
    pool = input_devices(dedupe=False) if kind == "input" else output_devices(dedupe=False)
    if not name:
        return default_device(kind)

    target = name.strip()
    low = target.lower()

    for d in pool:
        if d.name == target:
            return d
    ranked = sorted(pool, key=_rank)
    for d in ranked:
        if d.name.lower() == low:
            return d
    for d in ranked:
        if d.name.lower().startswith(low[:31]) or low.startswith(d.name.lower()[:31]):
            return d
    for d in ranked:
        if low in d.name.lower() or d.name.lower() in low:
            return d
    return None


def default_device(kind: str = "output") -> DeviceInfo | None:
    """The system default, but expressed through the best available host API.

    PortAudio reports the default under MME on Windows, which both truncates
    the name to 31 characters and adds around 100 ms of latency. Re-resolving
    by name finds the same hardware under WASAPI.
    """
    chosen = None
    for d in _all():
        if (kind == "input" and d.is_default_input) or \
           (kind == "output" and d.is_default_output):
            chosen = d
            break

    pool = input_devices(dedupe=False) if kind == "input" else output_devices(dedupe=False)
    if chosen is not None:
        prefs = HOSTAPI_PREFERENCE.get(_platform_key(), [])
        best = chosen
        for d in sorted(pool, key=_rank):
            same = (d.name.lower().startswith(chosen.name.lower()[:31])
                    or chosen.name.lower().startswith(d.name.lower()[:31]))
            if same and _rank(d)[0] < _rank(best)[0]:
                best = d
        if prefs:
            return best
        return chosen

    ranked = _dedupe(sorted(pool, key=_rank))
    return ranked[0] if ranked else None


def virtual_outputs() -> list[DeviceInfo]:
    """Output devices that look like a virtual cable (what Discord should read)."""
    return [d for d in output_devices() if d.is_virtual_output]


def virtual_inputs() -> list[DeviceInfo]:
    return [d for d in input_devices() if d.is_virtual_input]


@dataclass
class VirtualCableStatus:
    installed: bool
    output_name: str
    input_name: str
    message: str
    install_hint: str


def virtual_cable_status() -> VirtualCableStatus:
    """Is there somewhere to send audio that Discord can read as a microphone?"""
    outs = virtual_outputs()
    ins = virtual_inputs()
    if outs:
        pair = ins[0].name if ins else ""
        return VirtualCableStatus(
            installed=True,
            output_name=outs[0].name,
            input_name=pair,
            message=(f"Found {outs[0].name}. Send MicForge here, then pick "
                     f"{pair or 'the matching input'} as your microphone in Discord."),
            install_hint="",
        )

    if _platform_key() == "linux":
        return VirtualCableStatus(
            installed=False, output_name="", input_name="",
            message="No virtual microphone yet.",
            install_hint=("MicForge can create one for you with PipeWire/PulseAudio "
                          "- press Create virtual microphone. No download needed."),
        )

    return VirtualCableStatus(
        installed=False, output_name="", input_name="",
        message="No virtual audio cable found.",
        install_hint=(
            "Windows has no built-in way for an app to appear as a microphone, so a "
            "small driver is needed. Install VB-CABLE (free) from vb-audio.com/Cable, "
            "reboot, then choose CABLE Input here and CABLE Output as your Discord "
            "microphone. VoiceMeeter works too if you already have it."),
    )


def describe_device(dev: DeviceInfo | None) -> str:
    if dev is None:
        return "not set"
    bits = [dev.name, f"{dev.hostapi_name}"]
    if dev.max_input:
        bits.append(f"{dev.max_input} in")
    if dev.max_output:
        bits.append(f"{dev.max_output} out")
    bits.append(f"{int(dev.default_samplerate)} Hz")
    return " | ".join(bits)


def supports(dev: DeviceInfo | None, samplerate: int, channels: int,
             kind: str = "output") -> bool:
    """Ask PortAudio whether a format is actually openable before we try."""
    if sd is None or dev is None:
        return False
    try:
        if kind == "input":
            sd.check_input_settings(device=dev.index, channels=channels,
                                    samplerate=samplerate)
        else:
            sd.check_output_settings(device=dev.index, channels=channels,
                                     samplerate=samplerate)
        return True
    except Exception:
        return False


def best_samplerate(dev: DeviceInfo | None, preferred: int = 48000,
                    kind: str = "output", channels: int = 2) -> int:
    if dev is None:
        return preferred
    for sr in (preferred, int(dev.default_samplerate), 48000, 44100, 32000, 16000):
        if supports(dev, sr, channels, kind):
            return sr
    return preferred
