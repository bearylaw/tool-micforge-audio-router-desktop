"""Render the README screenshots from demo data.

Deliberately stubs the device and process lists: real ones carry the machine's
hardware names, window titles and Discord channel names, none of which belong
in a public repository.

Usage:  python docs/make_screenshots.py [output-dir]
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "screenshots")
OUT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
os.environ["MICFORGE_HOME"] = tempfile.mkdtemp(prefix="micforge-shots-")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from micforge import config  # noqa: E402
from micforge.audio import devices, sources  # noqa: E402

# --------------------------------------------------------------- demo devices
DEMO_INPUTS = [
    ("Microphone (Studio USB)", "Windows WASAPI", 2),
    ("Headset Microphone (Gaming Headset)", "Windows WASAPI", 1),
    ("CABLE Output (VB-Audio Virtual Cable)", "Windows WASAPI", 2),
]
DEMO_OUTPUTS = [
    ("CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI", 2),
    ("Headphones (Gaming Headset)", "Windows WASAPI", 2),
    ("Speakers (Realtek Audio)", "Windows WASAPI", 2),
]
DEMO_SOURCES = [
    ("Rocket League", "RocketLeague.exe", 4512, "session", True),
    ("Spotify", "Spotify.exe", 7788, "session", True),
    ("Firefox", "firefox.exe", 3320, "window", False),
    ("Steam", "steam.exe", 2140, "window", False),
    ("OBS Studio", "obs64.exe", 9012, "window", False),
]


def _device(index, name, api, channels, is_input):
    return devices.DeviceInfo(
        index=index, name=name, hostapi=0, hostapi_name=api,
        max_input=channels if is_input else 0,
        max_output=0 if is_input else channels,
        default_samplerate=48000.0,
        is_default_input=is_input and index == 0,
        is_default_output=not is_input and index == 1)


devices.input_devices = lambda dedupe=True: [
    _device(i, n, a, c, True) for i, (n, a, c) in enumerate(DEMO_INPUTS)]
devices.output_devices = lambda dedupe=True: [
    _device(i, n, a, c, False) for i, (n, a, c) in enumerate(DEMO_OUTPUTS)]
devices.refresh = lambda: None
sources.list_sources = lambda include_windows=True, include_all_processes=False: [
    sources.AudioSource(pid=pid, name=exe, label=label, kind=kind, playing=playing)
    for label, exe, pid, kind, playing in DEMO_SOURCES]

# ----------------------------------------------------------------- demo state
cfg = config.load()
cfg.ui.autostart_engine = False
cfg.ui.minimise_to_tray = False
cfg.devices.microphone = "Microphone (Studio USB)"
cfg.devices.virtual_out = "CABLE Input (VB-Audio Virtual Cable)"
cfg.devices.monitor_out = "Headphones (Gaming Headset)"
cfg.capture.mode = "process"
cfg.capture.process_pid = 4512
cfg.capture.process_name = "RocketLeague.exe"
cfg.mixer.mic_gain_db = 6.0
cfg.mixer.capture_gain_db = -9.0

demo_clip = Path(os.environ["MICFORGE_HOME"]) / "demo.wav"
t = np.arange(24000) / 48000
sf.write(demo_clip, (0.4 * np.sin(2 * np.pi * 520 * t)).astype("float32"), 48000)

for name, key, colour in [("Airhorn", "ctrl+f1", "#e05563"),
                          ("Bruh", "ctrl+f2", "#e8a33d"),
                          ("Join stinger", "", "#35c07d"),
                          ("Rimshot", "ctrl+f4", "#3d7dff"),
                          ("Wow", "", "#a06ae0"),
                          ("Vine boom", "ctrl+f6", "#35c0b0"),
                          ("Sad trombone", "", "#e07a3d"),
                          ("Applause", "ctrl+f8", "#6ac0e0")]:
    cfg.soundboard.sounds.append(
        config.SoundEntry(name=name, path=str(demo_clip), hotkey=key, color=colour))

from micforge.discordlink import triggers as trig  # noqa: E402

join = trig.default_join_rule(cfg.soundboard.sounds[2].id)
join.channel_filter = "gaming"
cfg.discord.triggers.append(join)
cfg.discord.triggers.append(config.TriggerRule(
    event="voice_disconnected", sound_id=cfg.soundboard.sounds[6].id, delay_ms=0))
cfg.discord.triggers.append(config.TriggerRule(
    event="user_joined", sound_id=cfg.soundboard.sounds[7].id, delay_ms=200,
    cooldown_ms=5000))
cfg.discord.client_id = "1234567890123456789"

from micforge.dsp import presets  # noqa: E402

presets.apply_preset(cfg, "Woman")

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from micforge.app import MicForgeApp  # noqa: E402
from micforge.ui import theme  # noqa: E402
from micforge.ui.main_window import MainWindow  # noqa: E402

controller = MicForgeApp(cfg)
qt = QApplication(sys.argv[:1])
qt.setStyleSheet(theme.stylesheet(cfg.ui.accent))
window = MainWindow(controller)
window.resize(1280, 880)
window.show()

TABS = ["mix", "voice", "soundboard", "discord", "settings"]


def shoot():
    for index, name in enumerate(TABS):
        window.tabs.setCurrentIndex(index)
        for _ in range(8):
            QCoreApplication.processEvents()
        tick = getattr(window.tabs.widget(index), "tick", None)
        if tick:
            tick()
        QCoreApplication.processEvents()
        path = OUT / f"{name}.png"
        window.grab().save(str(path))
        print("wrote", path)
    qt.quit()


QTimer.singleShot(800, shoot)
qt.exec()
controller.shutdown()
