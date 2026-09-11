"""Linux audio plumbing through PulseAudio / PipeWire (``pactl``).

Linux needs much less special-casing than Windows: PipeWire already exposes a
monitor source for every sink, and PortAudio can open those directly, so
desktop capture is just "record from the monitor". The two things that do need
help are

* **the virtual microphone** -- a null sink plus a remapped source, which is
  what Discord will list as an input device, and
* **per-application capture** -- done by moving that application's stream onto
  a private null sink and recording its monitor, with an optional loopback back
  to the speakers so you still hear the game.

Everything degrades to "unavailable" if ``pactl`` is missing, rather than
raising, so the rest of the app does not need to care which platform it is on.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from .. import log

_log = log.get("pulse")

MIC_SINK = "micforge_mic"
MIC_SOURCE = "micforge_mic_source"
MIC_DESCRIPTION = "MicForge Virtual Microphone"
CAPTURE_SINK = "micforge_capture"
CAPTURE_DESCRIPTION = "MicForge App Capture"

_TIMEOUT = 5.0


def available() -> bool:
    return shutil.which("pactl") is not None


def _run(args: list[str], check: bool = False) -> tuple[int, str, str]:
    if not available():
        return 127, "", "pactl not found"
    try:
        proc = subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "", "pactl timed out"
    except Exception as exc:  # pragma: no cover - platform specific
        return 1, "", str(exc)
    if check and proc.returncode != 0:
        _log.warning("pactl %s failed: %s", " ".join(args), proc.stderr.strip())
    return proc.returncode, proc.stdout, proc.stderr


def _list_json(what: str) -> list[dict]:
    code, out, _ = _run(["-f", "json", "list", what])
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------- queries
@dataclass
class SinkInput:
    """One application playback stream."""

    index: int
    app_name: str
    binary: str
    pid: int
    sink: int
    media_name: str

    @property
    def label(self) -> str:
        name = self.app_name or self.binary or f"stream {self.index}"
        if self.media_name and self.media_name.lower() not in name.lower():
            return f"{name} - {self.media_name}"
        return name


def list_sink_inputs() -> list[SinkInput]:
    out: list[SinkInput] = []
    for entry in _list_json("sink-inputs"):
        props = entry.get("properties") or {}
        try:
            pid = int(props.get("application.process.id") or 0)
        except (TypeError, ValueError):
            pid = 0
        out.append(SinkInput(
            index=int(entry.get("index", -1)),
            app_name=str(props.get("application.name") or ""),
            binary=str(props.get("application.process.binary") or ""),
            pid=pid,
            sink=int(entry.get("sink", -1) or -1),
            media_name=str(props.get("media.name") or ""),
        ))
    return [s for s in out if s.index >= 0]


def list_sinks() -> list[tuple[str, str]]:
    return [(str(e.get("name", "")), str(e.get("description", e.get("name", ""))))
            for e in _list_json("sinks") if e.get("name")]


def list_sources() -> list[tuple[str, str]]:
    return [(str(e.get("name", "")), str(e.get("description", e.get("name", ""))))
            for e in _list_json("sources") if e.get("name")]


def default_sink() -> str:
    code, out, _ = _run(["get-default-sink"])
    return out.strip() if code == 0 else ""


def monitor_of(sink_name: str) -> str:
    return f"{sink_name}.monitor" if sink_name else ""


def loaded_modules() -> list[tuple[int, str, str]]:
    out = []
    for e in _list_json("modules"):
        out.append((int(e.get("index", -1)), str(e.get("name", "")),
                    str(e.get("argument") or "")))
    return out


def _find_module(name: str, needle: str) -> int:
    for idx, mod, arg in loaded_modules():
        if mod == name and needle in arg:
            return idx
    return -1


def _load_module(name: str, *args: str) -> int:
    code, out, err = _run(["load-module", name, *args], check=True)
    if code != 0:
        return -1
    try:
        return int(out.strip())
    except ValueError:
        return -1


def unload_module(index: int) -> bool:
    if index is None or index < 0:
        return False
    return _run(["unload-module", str(index)])[0] == 0


# --------------------------------------------------------------------------- virtual mic
@dataclass
class VirtualMic:
    sink_module: int = -1
    source_module: int = -1
    sink_name: str = MIC_SINK
    source_name: str = MIC_SOURCE

    @property
    def ok(self) -> bool:
        return self.sink_module >= 0 or self.source_module >= 0


def virtual_mic_exists() -> bool:
    names = {n for n, _ in list_sources()}
    return MIC_SOURCE in names


def create_virtual_mic() -> VirtualMic:
    """Create (or adopt) the null sink + remapped source pair.

    Idempotent: if a previous run left the modules loaded, they are reused
    rather than stacked, which otherwise leaves the user with five identical
    microphones in the Discord dropdown.
    """
    vm = VirtualMic()
    if not available():
        _log.warning("pactl unavailable - cannot create a virtual microphone")
        return vm

    existing = _find_module("module-null-sink", f"sink_name={MIC_SINK}")
    if existing >= 0:
        vm.sink_module = existing
    else:
        vm.sink_module = _load_module(
            "module-null-sink",
            f"sink_name={MIC_SINK}",
            f"sink_properties=device.description='{MIC_DESCRIPTION}'",
        )

    existing_src = _find_module("module-remap-source", f"source_name={MIC_SOURCE}")
    if existing_src >= 0:
        vm.source_module = existing_src
    else:
        vm.source_module = _load_module(
            "module-remap-source",
            f"source_name={MIC_SOURCE}",
            f"master={MIC_SINK}.monitor",
            f"source_properties=device.description='{MIC_DESCRIPTION}'",
        )

    if vm.ok:
        _log.info("virtual mic ready: sink=%s source=%s", MIC_SINK, MIC_SOURCE)
    else:
        _log.error("failed to create the virtual microphone")
    return vm


def destroy_virtual_mic(vm: VirtualMic | None = None) -> None:
    if vm is not None:
        unload_module(vm.source_module)
        unload_module(vm.sink_module)
        return
    idx = _find_module("module-remap-source", f"source_name={MIC_SOURCE}")
    unload_module(idx)
    idx = _find_module("module-null-sink", f"sink_name={MIC_SINK}")
    unload_module(idx)


# --------------------------------------------------------------------------- app capture
@dataclass
class AppCapture:
    """A private sink an application has been redirected into."""

    sink_module: int = -1
    loopback_module: int = -1
    sink_name: str = CAPTURE_SINK
    moved: list[tuple[int, int]] = None  # (sink_input index, original sink)

    def __post_init__(self):
        if self.moved is None:
            self.moved = []

    @property
    def monitor(self) -> str:
        return monitor_of(self.sink_name)


def capture_application(pid: int = 0, binary: str = "",
                        passthrough_sink: str = "") -> AppCapture:
    """Route one application into a private sink so it can be captured alone.

    ``passthrough_sink`` (usually the default sink) gets a loopback so the user
    still hears the app. Without it the game would go silent for the streamer,
    which is never what anyone wants.
    """
    cap = AppCapture()
    if not available():
        return cap

    existing = _find_module("module-null-sink", f"sink_name={CAPTURE_SINK}")
    cap.sink_module = existing if existing >= 0 else _load_module(
        "module-null-sink",
        f"sink_name={CAPTURE_SINK}",
        f"sink_properties=device.description='{CAPTURE_DESCRIPTION}'",
    )
    if cap.sink_module < 0:
        return cap

    target = passthrough_sink or default_sink()
    if target and target != CAPTURE_SINK:
        existing_lb = _find_module("module-loopback", f"source={CAPTURE_SINK}.monitor")
        cap.loopback_module = existing_lb if existing_lb >= 0 else _load_module(
            "module-loopback",
            f"source={CAPTURE_SINK}.monitor",
            f"sink={target}",
            "latency_msec=20",
        )

    for si in list_sink_inputs():
        if (pid and si.pid == pid) or (binary and binary.lower() in si.binary.lower()):
            if _run(["move-sink-input", str(si.index), CAPTURE_SINK])[0] == 0:
                cap.moved.append((si.index, si.sink))
                _log.info("moved %s (pid %s) into %s", si.label, si.pid, CAPTURE_SINK)
    return cap


def release_application(cap: AppCapture, restore_sink: str = "") -> None:
    """Put the streams back and drop the private sink."""
    target = restore_sink or default_sink()
    for index, _original in cap.moved:
        if target:
            _run(["move-sink-input", str(index), target])
    cap.moved.clear()
    unload_module(cap.loopback_module)
    unload_module(cap.sink_module)
    cap.loopback_module = -1
    cap.sink_module = -1


def cleanup_all() -> None:
    """Remove anything this app may have left loaded in a previous run."""
    if not available():
        return
    for name, needle in (
        ("module-loopback", f"source={CAPTURE_SINK}.monitor"),
        ("module-null-sink", f"sink_name={CAPTURE_SINK}"),
        ("module-remap-source", f"source_name={MIC_SOURCE}"),
        ("module-null-sink", f"sink_name={MIC_SINK}"),
    ):
        idx = _find_module(name, needle)
        if idx >= 0:
            unload_module(idx)
