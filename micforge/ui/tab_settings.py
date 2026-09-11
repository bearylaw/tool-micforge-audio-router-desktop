"""Settings tab: audio quality, startup behaviour, push-to-talk and diagnostics."""
from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import config, log
from ..audio import devices
from . import theme
from .widgets import Card, ChoiceRow, HotkeyEdit, Toggle, ValueSlider, hint, hline

_log = log.get("ui.settings")

SAMPLERATES = ["44100", "48000"]
BLOCK_CHOICES = {
    "Lowest latency (2.5 ms)": 120,
    "Low (5 ms)": 240,
    "Balanced (10 ms)": 480,
    "Safe (20 ms)": 960,
    "Very safe (40 ms)": 1920,
}


class SettingsTab(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.cfg = app.cfg
        self._loading = False

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        root.addWidget(self._build_audio())
        root.addWidget(self._build_ptt())
        root.addWidget(self._build_startup())
        root.addWidget(self._build_diagnostics())
        root.addStretch(1)
        area.setWidget(container)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(area)

        self.load_from_config()

    # --------------------------------------------------------------- audio
    def _build_audio(self) -> Card:
        card = Card("Audio quality",
                    "Smaller buffers mean less delay but more chance of crackling. "
                    "If you hear dropouts, move one step safer.")

        self.samplerate = ChoiceRow("Sample rate", SAMPLERATES, label_width=120)
        self.samplerate.valueChanged.connect(self._on_audio)
        card.add(self.samplerate)

        self.blocksize = ChoiceRow("Buffer", list(BLOCK_CHOICES), label_width=120)
        self.blocksize.valueChanged.connect(self._on_audio)
        card.add(self.blocksize)

        self.mic_channels = ChoiceRow("Mic channel", ["mono", "left", "right"],
                                      label_width=120)
        self.mic_channels.valueChanged.connect(self._on_audio)
        card.add(self.mic_channels)
        card.add(hint("Pick left or right if your interface puts the microphone on one "
                      "side of a stereo input."))

        self.exclusive = Toggle("Exclusive mode (Windows, lower latency)", False,
                                "Takes sole control of the device. Lower latency, but "
                                "other apps cannot use it at the same time.")
        self.exclusive.toggled.connect(self._on_audio)
        card.add(self.exclusive)

        self.latency_label = QLabel("")
        self.latency_label.setObjectName("Value")
        card.add(self.latency_label)

        row = QHBoxLayout()
        restart = QPushButton("Apply and restart audio")
        restart.setObjectName("Primary")
        restart.clicked.connect(self._restart)
        row.addWidget(restart)
        row.addStretch(1)
        card.add_layout(row)
        return card

    # ----------------------------------------------------------------- ptt
    def _build_ptt(self) -> Card:
        card = Card("Push to talk",
                    "Optional. Discord has its own push-to-talk; this one gates the "
                    "microphone inside MicForge, which also silences the effects tail.")
        self.ptt_enabled = Toggle("Enable push to talk", False)
        self.ptt_enabled.toggled.connect(self._on_ptt)
        card.add(self.ptt_enabled)

        label = QLabel("Hold this key")
        label.setObjectName("CardHint")
        card.add(label)
        self.ptt_hotkey = HotkeyEdit(self.app.hotkeys)
        self.ptt_hotkey.changed.connect(self._on_ptt)
        card.add(self.ptt_hotkey)

        self.ptt_inverted = Toggle("Invert (hold to mute instead)", False)
        self.ptt_inverted.toggled.connect(self._on_ptt)
        card.add(self.ptt_inverted)
        card.add(hint("Captured audio and soundboard clips keep playing either way - "
                      "only your voice is gated."))
        return card

    # ------------------------------------------------------------- startup
    def _build_startup(self) -> Card:
        card = Card("Startup and window")
        self.autostart = Toggle("Start the audio engine when MicForge opens", True)
        self.tray = Toggle("Keep running in the tray when the window is closed", True)
        self.start_min = Toggle("Start minimised to the tray", False)
        for toggle in (self.autostart, self.tray, self.start_min):
            toggle.toggled.connect(self._on_startup)
            card.add(toggle)
        return card

    # --------------------------------------------------------- diagnostics
    def _build_diagnostics(self) -> Card:
        card = Card("Diagnostics")
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setFixedHeight(190)
        card.add(self.diag)

        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh_diagnostics)
        row.addWidget(refresh)

        copy = QPushButton("Copy report")
        copy.setObjectName("Ghost")
        copy.clicked.connect(self._copy_report)
        row.addWidget(copy)

        open_config = QPushButton("Open config folder")
        open_config.setObjectName("Ghost")
        open_config.clicked.connect(lambda: self._open(config.data_dir()))
        row.addWidget(open_config)

        open_logs = QPushButton("Open log folder")
        open_logs.setObjectName("Ghost")
        open_logs.clicked.connect(lambda: self._open(log.log_dir()))
        row.addWidget(open_logs)
        row.addStretch(1)
        card.add_layout(row)

        card.add(hline())
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(160)
        self.log_view.setPlainText("\n".join(log.history()[-200:]))
        card.add(self.log_view)
        log.subscribe(self._on_log)
        return card

    def _on_log(self, line: str, _level: int) -> None:
        QTimer.singleShot(0, lambda: self._append_log(line))

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        if self.log_view.blockCount() > 400:
            cursor = self.log_view.textCursor()
            cursor.movePosition(cursor.Start)
            for _ in range(100):
                cursor.select(cursor.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

    # --------------------------------------------------------------- state
    def load_from_config(self) -> None:
        self._loading = True
        d, mx, ui = self.cfg.devices, self.cfg.mixer, self.cfg.ui
        self.samplerate.set_value(str(d.samplerate))
        for label, frames in BLOCK_CHOICES.items():
            if frames == d.blocksize:
                self.blocksize.set_value(label)
                break
        self.mic_channels.set_value(d.mic_channel_mode)
        self.exclusive.setChecked(d.exclusive_mode)

        self.ptt_enabled.setChecked(mx.ptt_enabled)
        self.ptt_hotkey.set_value(mx.ptt_hotkey)
        self.ptt_inverted.setChecked(mx.ptt_inverted)

        self.autostart.setChecked(ui.autostart_engine)
        self.tray.setChecked(ui.minimise_to_tray)
        self.start_min.setChecked(ui.start_minimised)
        self._loading = False
        self._update_latency_label()
        self.refresh_diagnostics()

    def _update_latency_label(self) -> None:
        block = self.cfg.devices.blocksize
        sr = self.cfg.devices.samplerate
        ms = 1000.0 * block / max(sr, 1)
        chain = self.app.engine.chain.latency
        chain_ms = 1000.0 * chain / max(sr, 1)
        self.latency_label.setText(
            f"buffer {ms:.1f} ms  |  voice chain {chain_ms:.1f} ms  |  "
            f"engine reports {self.app.engine.status.latency_ms:.0f} ms end to end")

    # ------------------------------------------------------------ handlers
    def _on_audio(self, *_args) -> None:
        if self._loading:
            return
        d = self.cfg.devices
        d.samplerate = int(self.samplerate.value())
        d.blocksize = BLOCK_CHOICES.get(self.blocksize.value(), 240)
        d.mic_channel_mode = self.mic_channels.value()
        d.exclusive_mode = self.exclusive.isChecked()
        self.app.save(0.2)
        self._update_latency_label()

    def _on_ptt(self, *_args) -> None:
        if self._loading:
            return
        mx = self.cfg.mixer
        mx.ptt_enabled = self.ptt_enabled.isChecked()
        mx.ptt_hotkey = self.ptt_hotkey.value()
        mx.ptt_inverted = self.ptt_inverted.isChecked()
        self.app.rebuild_hotkeys()
        if mx.ptt_enabled and not self.app.hotkeys.running:
            self.app.hotkeys.start()
        self.app.save(0.2)

    def _on_startup(self, *_args) -> None:
        if self._loading:
            return
        ui = self.cfg.ui
        ui.autostart_engine = self.autostart.isChecked()
        ui.minimise_to_tray = self.tray.isChecked()
        ui.start_minimised = self.start_min.isChecked()
        self.app.save(0.2)

    def _restart(self) -> None:
        self.app.engine.restart()
        self._update_latency_label()
        self.refresh_diagnostics()

    def _open(self, path) -> None:
        path = str(path)
        os.makedirs(path, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            _log.warning("cannot open %s: %s", path, exc)

    def _copy_report(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.diag.toPlainText())

    def refresh_diagnostics(self) -> None:
        st = self.app.engine.status
        lines = [
            f"MicForge      : {__import__('micforge').__version__} on {sys.platform}",
            f"Python        : {sys.version.split()[0]}",
            f"Engine        : {'running' if st.running else 'stopped'}  "
            f"{st.samplerate} Hz  block {st.blocksize}",
            f"Microphone    : {st.mic_device or '(none)'}",
            f"To Discord    : {st.out_device or '(none)'}",
            f"Monitor       : {st.monitor_device or '(none)'}",
            f"Capture       : {st.capture_mode} -> {st.capture_target or '-'} "
            f"({'ok' if st.capture_ok else 'not running'})",
            f"Latency       : {st.latency_ms:.0f} ms   CPU {st.cpu_percent:.0f}%   "
            f"dropouts {st.xruns}",
            f"Ring buffers  : {self.app.engine.ring_health()}",
            f"Hotkeys       : {'running' if self.app.hotkeys.running else 'off'}  "
            f"{self.app.hotkeys.bound_specs()}",
            f"Discord       : {self.app.discord_summary()}",
            f"Config        : {config.config_path()}",
        ]
        if st.errors:
            lines.append("")
            lines.append("Errors:")
            lines += [f"  - {e}" for e in st.errors]
        if st.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines += [f"  - {w}" for w in st.warnings]

        lines.append("")
        lines.append("Output devices:")
        for dev in devices.output_devices():
            mark = " (virtual)" if dev.is_virtual_output else ""
            lines.append(f"  - {dev.label}{mark}")
        lines.append("Input devices:")
        for dev in devices.input_devices():
            lines.append(f"  - {dev.label}")
        self.diag.setPlainText("\n".join(lines))

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        if self.isVisible():
            self._update_latency_label()
