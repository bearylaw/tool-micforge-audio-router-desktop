"""Mix tab: where the audio goes, what gets captured, and how loud everything is."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QRadioButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import log
from ..audio import devices, sources
from . import theme
from .widgets import Card, LevelMeter, Toggle, ValueSlider, hint, hline, section

_log = log.get("ui.mix")

CAPTURE_MODES = [
    ("off", "Nothing", "Only your microphone goes to Discord."),
    ("desktop", "Everything you hear", "The whole desktop mix - music, game, browser."),
    ("process", "One app or game", "Just the app you pick. Nothing else leaks through."),
    ("exclude", "Everything except one app",
     "Useful to capture the desktop without Discord itself, which would echo."),
]


class MixTab(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.cfg = app.cfg
        self._loading = False

        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        root.addWidget(self._build_routing())
        root.addWidget(self._build_capture())
        root.addWidget(self._build_mixer())
        root.addStretch(1)

        outer.setWidget(container)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(outer)

        self.refresh_devices()
        self.refresh_sources()
        self.load_from_config()

        self._source_timer = QTimer(self)
        self._source_timer.timeout.connect(self._refresh_source_levels)
        self._source_timer.start(1500)

    # ------------------------------------------------------------- routing
    def _build_routing(self) -> Card:
        card = Card("Routing",
                    "MicForge listens to your microphone, mixes in whatever else you "
                    "choose, and sends the result to a virtual cable that Discord "
                    "reads as a microphone.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        def row(index: int, label: str, tip: str) -> QComboBox:
            name = QLabel(label)
            name.setObjectName("CardHint")
            name.setToolTip(tip)
            grid.addWidget(name, index, 0)
            combo = QComboBox()
            combo.setToolTip(tip)
            grid.addWidget(combo, index, 1)
            return combo

        self.mic_combo = row(0, "Microphone", "Your real microphone.")
        self.out_combo = row(1, "Send to Discord via",
                             "The virtual cable. Pick the matching input as your "
                             "microphone inside Discord.")
        self.monitor_combo = row(2, "I listen on",
                                 "Your headphones. Never pick the virtual cable here "
                                 "or you will create a feedback loop.")

        self.mic_combo.currentIndexChanged.connect(self._on_device_changed)
        self.out_combo.currentIndexChanged.connect(self._on_device_changed)
        self.monitor_combo.currentIndexChanged.connect(self._on_device_changed)
        card.add_layout(grid)

        self.cable_status = QLabel("")
        self.cable_status.setWordWrap(True)
        self.cable_status.setObjectName("CardHint")
        card.add(self.cable_status)

        buttons = QHBoxLayout()
        refresh = QPushButton("Rescan devices")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self._rescan)
        buttons.addWidget(refresh)

        self.create_mic_button = QPushButton("Create virtual microphone")
        self.create_mic_button.clicked.connect(self._create_virtual_mic)
        buttons.addWidget(self.create_mic_button)

        self.restart_button = QPushButton("Apply and restart audio")
        self.restart_button.setObjectName("Primary")
        self.restart_button.clicked.connect(self._restart_engine)
        buttons.addWidget(self.restart_button)
        buttons.addStretch(1)
        card.add_layout(buttons)
        return card

    # ------------------------------------------------------------- capture
    def _build_capture(self) -> Card:
        card = Card("What else should Discord hear?",
                    "Pick a single game and only that game goes down the wire - your "
                    "music, notifications and voice chat stay out of it.")
        self.mode_group = QButtonGroup(self)
        mode_row = QVBoxLayout()
        mode_row.setSpacing(4)
        self.mode_buttons: dict[str, QRadioButton] = {}
        for value, label, tip in CAPTURE_MODES:
            button = QRadioButton(label)
            button.setToolTip(tip)
            button.toggled.connect(
                lambda checked, v=value: self._on_mode_changed(v) if checked else None)
            self.mode_group.addButton(button)
            self.mode_buttons[value] = button
            mode_row.addWidget(button)
        card.add_layout(mode_row)

        card.add(hline())

        self.app_panel = QWidget()
        panel = QVBoxLayout(self.app_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(8)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search running apps and windows...")
        self.search.textChanged.connect(self._filter_sources)
        search_row.addWidget(self.search, 1)
        rescan = QPushButton("Refresh")
        rescan.setObjectName("Ghost")
        rescan.clicked.connect(self.refresh_sources)
        search_row.addWidget(rescan)
        panel.addLayout(search_row)

        self.source_list = QListWidget()
        self.source_list.setMinimumHeight(160)
        self.source_list.itemSelectionChanged.connect(self._on_source_selected)
        panel.addWidget(self.source_list)

        panel.addWidget(hint(
            "A dot means the app is making sound right now. Multi-process apps like "
            "browsers are handled automatically - their child processes are included."))

        self.reattach = Toggle("Follow this app if it restarts", True,
                               "Re-binds by executable name when the game closes and "
                               "reopens, so you do not have to pick it again.")
        self.reattach.toggled.connect(self._on_reattach)
        panel.addWidget(self.reattach)
        card.add(self.app_panel)

        self.endpoint_panel = QWidget()
        ep = QHBoxLayout(self.endpoint_panel)
        ep.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Capture from")
        label.setObjectName("CardHint")
        label.setMinimumWidth(110)
        ep.addWidget(label)
        self.endpoint_combo = QComboBox()
        self.endpoint_combo.currentTextChanged.connect(self._on_endpoint)
        ep.addWidget(self.endpoint_combo, 1)
        card.add(self.endpoint_panel)

        self.capture_status = QLabel("")
        self.capture_status.setObjectName("CardHint")
        self.capture_status.setWordWrap(True)
        card.add(self.capture_status)
        return card

    # --------------------------------------------------------------- mixer
    def _build_mixer(self) -> Card:
        card = Card("Mixer", "Levels for everything going out, plus what you hear back.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        row = 0

        def strip(label: str, minimum: float, maximum: float, value: float,
                  tip: str) -> tuple[ValueSlider, LevelMeter, Toggle]:
            nonlocal row
            toggle = Toggle("", True)
            toggle.setFixedWidth(20)
            toggle.setToolTip(f"Mute/unmute {label.lower()}")
            grid.addWidget(toggle, row, 0)
            slider = ValueSlider(label, minimum, maximum, 0.5, "dB", value,
                                 label_width=96)
            slider.setToolTip(tip)
            grid.addWidget(slider, row, 1)
            meter = LevelMeter()
            meter.setMinimumWidth(120)
            grid.addWidget(meter, row, 2)
            row += 1
            return slider, meter, toggle

        self.mic_gain, self.mic_meter, self.mic_on = strip(
            "Microphone", -24, 36, 0, "Boost a quiet mic here. The limiter stops it "
            "clipping, but watch the meter - solid red is too far.")
        self.cap_gain, self.cap_meter, self.cap_on = strip(
            "Game / system", -40, 24, -6, "How loud the captured app is relative to "
            "your voice. Start low; people want to hear you, not the game.")
        self.sb_gain, self.sb_meter, self.sb_on = strip(
            "Soundboard", -40, 24, 0, "Level of soundboard clips.")
        self.master_gain, self.out_meter, self.master_on = strip(
            "Output", -24, 24, 0, "Final level sent to Discord.")
        self.master_on.setVisible(False)
        card.add_layout(grid)

        self.mic_on.toggled.connect(self._on_mixer)
        self.cap_on.toggled.connect(self._on_mixer)
        self.sb_on.toggled.connect(self._on_mixer)
        for slider in (self.mic_gain, self.cap_gain, self.sb_gain, self.master_gain):
            slider.valueChanged.connect(self._on_mixer)

        card.add(hline())
        card.add(section("What you hear"))
        monitor_row = QHBoxLayout()
        self.mon_mic = Toggle("My voice", False,
                              "Hearing yourself with effects on is useful for tuning, "
                              "but distracting while talking.")
        self.mon_cap = Toggle("Game audio", False,
                              "You normally already hear the game directly - turning "
                              "this on would double it.")
        self.mon_sb = Toggle("Soundboard", True, "Hear your own soundboard clips.")
        for toggle in (self.mon_mic, self.mon_cap, self.mon_sb):
            toggle.toggled.connect(self._on_mixer)
            monitor_row.addWidget(toggle)
        monitor_row.addStretch(1)
        card.add_layout(monitor_row)

        self.mon_gain = ValueSlider("Monitor level", -40, 12, 0.5, "dB", -6,
                                    label_width=96)
        self.mon_gain.valueChanged.connect(self._on_mixer)
        card.add(self.mon_gain)
        monitor_meter_row, self.mon_meter = self._meter_row("Monitor")
        card.add(monitor_meter_row)

        card.add(hline())
        card.add(section("Ducking"))
        card.add(hint("Pull the other sources down while a soundboard clip plays, so "
                      "the clip is actually audible."))
        duck_row = QHBoxLayout()
        self.duck_on = Toggle("Duck while a clip plays", True)
        self.duck_mic = Toggle("Duck my voice", False)
        self.duck_cap = Toggle("Duck game audio", True)
        for toggle in (self.duck_on, self.duck_mic, self.duck_cap):
            toggle.toggled.connect(self._on_mixer)
            duck_row.addWidget(toggle)
        duck_row.addStretch(1)
        card.add_layout(duck_row)

        self.duck_depth = ValueSlider("Duck by", -48, 0, 0.5, "dB", -12, label_width=96)
        self.duck_attack = ValueSlider("Duck in", 1, 500, 1, "ms", 40, label_width=96)
        self.duck_release = ValueSlider("Duck out", 10, 2000, 10, "ms", 300,
                                        label_width=96)
        for slider in (self.duck_depth, self.duck_attack, self.duck_release):
            slider.valueChanged.connect(self._on_mixer)
            card.add(slider)

        card.add(hline())
        card.add(section("Safety"))
        self.limiter_on = Toggle("Limiter (stops clipping into Discord)", True)
        self.limiter_on.toggled.connect(self._on_mixer)
        card.add(self.limiter_on)
        self.limiter_ceiling = ValueSlider("Ceiling", -12, 0, 0.5, "dB", -1,
                                           label_width=96)
        self.limiter_ceiling.valueChanged.connect(self._on_mixer)
        card.add(self.limiter_ceiling)
        self.gr_label = QLabel("")
        self.gr_label.setObjectName("Value")
        card.add(self.gr_label)
        return card

    @staticmethod
    def _meter_row(label: str):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        name = QLabel(label)
        name.setObjectName("CardHint")
        name.setMinimumWidth(96)
        row.addWidget(name)
        meter = LevelMeter()
        row.addWidget(meter, 1)
        return holder, meter

    # ------------------------------------------------------------ population
    def refresh_devices(self) -> None:
        self._loading = True
        for combo, pool, allow_empty in (
            (self.mic_combo, devices.input_devices(), False),
            (self.out_combo, devices.output_devices(), True),
            (self.monitor_combo, devices.output_devices(), True),
        ):
            current = combo.currentData()
            combo.clear()
            if allow_empty:
                combo.addItem("(none)", "")
            for dev in pool:
                label = dev.label
                if dev.is_virtual_output:
                    label = f"* {label}"
                combo.addItem(label, dev.name)
            if current:
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

        status = devices.virtual_cable_status()
        if status.installed:
            self.cable_status.setText(status.message)
            self.cable_status.setStyleSheet(f"color: {theme.GOOD};")
            self.create_mic_button.setVisible(False)
        else:
            self.cable_status.setText(status.install_hint)
            self.cable_status.setStyleSheet(f"color: {theme.WARN};")
            import sys

            self.create_mic_button.setVisible(sys.platform.startswith("linux"))

        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        self.endpoint_combo.addItem("Default playback device", "")
        try:
            import sys

            if sys.platform == "win32":
                from ..audio import wasapi

                for _did, name in wasapi.list_render_endpoints():
                    self.endpoint_combo.addItem(name, name)
            else:
                from ..audio import pulse

                for name, description in pulse.list_sinks():
                    self.endpoint_combo.addItem(description or name, name)
        except Exception as exc:
            _log.debug("endpoint list unavailable: %s", exc)
        self.endpoint_combo.blockSignals(False)
        self._loading = False

    def refresh_sources(self) -> None:
        selected = self.cfg.capture.process_pid
        self.source_list.clear()
        self._all_sources = sources.list_sources(include_all_processes=False)
        for src in self._all_sources:
            item = QListWidgetItem(f"{'  ● ' if src.playing else '     '}{src.label}")
            item.setData(Qt.UserRole, src)
            item.setToolTip(f"{src.name} (pid {src.pid}) - {src.kind}")
            if src.playing:
                item.setForeground(Qt.white)
            self.source_list.addItem(item)
            if src.pid == selected:
                self.source_list.setCurrentItem(item)
        self._filter_sources(self.search.text())

    def _filter_sources(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self.source_list.count()):
            item = self.source_list.item(i)
            src = item.data(Qt.UserRole)
            visible = (not needle or needle in src.label.lower()
                       or needle in src.name.lower())
            item.setHidden(not visible)

    def _refresh_source_levels(self) -> None:
        if not self.isVisible() or self.cfg.capture.mode not in ("process", "exclude"):
            return
        if self.search.text().strip():
            return
        self.refresh_sources()

    def load_from_config(self) -> None:
        self._loading = True
        d, mx, cap = self.cfg.devices, self.cfg.mixer, self.cfg.capture

        for combo, value in ((self.mic_combo, d.microphone),
                             (self.out_combo, d.virtual_out),
                             (self.monitor_combo, d.monitor_out)):
            idx = combo.findData(value)
            combo.setCurrentIndex(idx if idx >= 0 else 0)

        button = self.mode_buttons.get(cap.mode) or self.mode_buttons["off"]
        button.setChecked(True)
        self.reattach.setChecked(cap.auto_reattach)
        idx = self.endpoint_combo.findData(cap.endpoint)
        self.endpoint_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self.mic_gain.set_value(mx.mic_gain_db)
        self.cap_gain.set_value(mx.capture_gain_db)
        self.sb_gain.set_value(mx.soundboard_gain_db)
        self.master_gain.set_value(mx.master_gain_db)
        self.mic_on.setChecked(mx.mic_enabled)
        self.cap_on.setChecked(mx.capture_enabled)
        self.sb_on.setChecked(True)

        self.mon_mic.setChecked(mx.monitor_mic)
        self.mon_cap.setChecked(mx.monitor_capture)
        self.mon_sb.setChecked(mx.monitor_soundboard)
        self.mon_gain.set_value(mx.monitor_gain_db)

        self.duck_on.setChecked(mx.duck_enabled)
        self.duck_mic.setChecked(mx.duck_mic)
        self.duck_cap.setChecked(mx.duck_capture)
        self.duck_depth.set_value(mx.duck_depth_db)
        self.duck_attack.set_value(mx.duck_attack_ms)
        self.duck_release.set_value(mx.duck_release_ms)

        self.limiter_on.setChecked(mx.limiter_enabled)
        self.limiter_ceiling.set_value(mx.limiter_ceiling_db)
        self._loading = False
        self._update_mode_panels()

    # ------------------------------------------------------------- handlers
    def _on_device_changed(self) -> None:
        if self._loading:
            return
        self.cfg.devices.microphone = self.mic_combo.currentData() or ""
        self.cfg.devices.virtual_out = self.out_combo.currentData() or ""
        self.cfg.devices.monitor_out = self.monitor_combo.currentData() or ""
        self.app.save()
        self.restart_button.setText("Apply and restart audio *")

    def _on_mode_changed(self, mode: str) -> None:
        if self._loading:
            return
        self.cfg.capture.mode = mode
        self._update_mode_panels()
        self.app.save()
        if self.app.engine.status.running:
            self.app.engine.start_capture()
            self._update_capture_status()

    def _update_mode_panels(self) -> None:
        mode = self.cfg.capture.mode
        self.app_panel.setVisible(mode in ("process", "exclude"))
        self.endpoint_panel.setVisible(mode == "desktop")
        self._update_capture_status()

    def _on_source_selected(self) -> None:
        if self._loading:
            return
        item = self.source_list.currentItem()
        if item is None:
            return
        src = item.data(Qt.UserRole)
        self.cfg.capture.process_pid = src.pid
        self.cfg.capture.process_name = src.name
        self.cfg.capture.window_title = src.window_title
        self.app.save()
        if self.app.engine.status.running and self.cfg.capture.mode in ("process",
                                                                        "exclude"):
            self.app.engine.start_capture()
        self._update_capture_status()

    def _on_reattach(self, checked: bool) -> None:
        if self._loading:
            return
        self.cfg.capture.auto_reattach = bool(checked)
        self.app.save()

    def _on_endpoint(self, _text: str) -> None:
        if self._loading:
            return
        self.cfg.capture.endpoint = self.endpoint_combo.currentData() or ""
        self.app.save()
        if self.app.engine.status.running and self.cfg.capture.mode == "desktop":
            self.app.engine.start_capture()
            self._update_capture_status()

    def _on_mixer(self, *_args) -> None:
        if self._loading:
            return
        mx = self.cfg.mixer
        mx.mic_gain_db = self.mic_gain.value()
        mx.capture_gain_db = self.cap_gain.value()
        mx.soundboard_gain_db = self.sb_gain.value()
        mx.master_gain_db = self.master_gain.value()
        mx.mic_enabled = self.mic_on.isChecked()
        mx.capture_enabled = self.cap_on.isChecked()

        mx.monitor_mic = self.mon_mic.isChecked()
        mx.monitor_capture = self.mon_cap.isChecked()
        mx.monitor_soundboard = self.mon_sb.isChecked()
        mx.monitor_gain_db = self.mon_gain.value()

        mx.duck_enabled = self.duck_on.isChecked()
        mx.duck_mic = self.duck_mic.isChecked()
        mx.duck_capture = self.duck_cap.isChecked()
        mx.duck_depth_db = self.duck_depth.value()
        mx.duck_attack_ms = self.duck_attack.value()
        mx.duck_release_ms = self.duck_release.value()

        mx.limiter_enabled = self.limiter_on.isChecked()
        mx.limiter_ceiling_db = self.limiter_ceiling.value()

        if not self.sb_on.isChecked():
            mx.soundboard_gain_db = -80.0
        self.app.apply_mixer()

    def _rescan(self) -> None:
        devices.refresh()
        self.refresh_devices()
        self.load_from_config()

    def _create_virtual_mic(self) -> None:
        from ..audio import pulse

        vm = pulse.create_virtual_mic()
        if vm.ok:
            devices.refresh()
            self.refresh_devices()
            self.cable_status.setText(
                f"Created {pulse.MIC_DESCRIPTION}. Choose it below, then select it as "
                "your microphone in Discord.")
            self.cable_status.setStyleSheet(f"color: {theme.GOOD};")
        else:
            self.cable_status.setText(
                "Could not create the virtual microphone. Is pactl installed?")
            self.cable_status.setStyleSheet(f"color: {theme.BAD};")

    def _restart_engine(self) -> None:
        self.restart_button.setText("Restarting...")
        self.restart_button.setEnabled(False)
        QTimer.singleShot(50, self._do_restart)

    def _do_restart(self) -> None:
        self.app.engine.restart()
        self.restart_button.setText("Apply and restart audio")
        self.restart_button.setEnabled(True)
        self._update_capture_status()

    def _update_capture_status(self) -> None:
        st = self.app.engine.status
        cap = self.cfg.capture
        if cap.mode == "off":
            self.capture_status.setText("")
            return
        if not st.running:
            self.capture_status.setText("Audio engine is stopped.")
            self.capture_status.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            return
        if st.capture_ok:
            self.capture_status.setText(f"Capturing: {st.capture_target}")
            self.capture_status.setStyleSheet(f"color: {theme.GOOD};")
        else:
            target = cap.process_name or "the selected source"
            self.capture_status.setText(
                f"Not capturing {target} yet. If the app is not running, start it and "
                "press Refresh.")
            self.capture_status.setStyleSheet(f"color: {theme.WARN};")

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        levels = self.app.engine.levels
        self.mic_meter.set_level(levels.mic_out or levels.mic_in)
        self.cap_meter.set_level(levels.capture)
        self.sb_meter.set_level(levels.soundboard)
        self.out_meter.set_level(levels.output)
        self.mon_meter.set_level(levels.monitor)

        bits = []
        if levels.limiter_gr_db < -0.1:
            bits.append(f"limiter {levels.limiter_gr_db:.1f} dB")
        if levels.duck < 0.99:
            import math

            bits.append(f"ducking {20 * math.log10(max(levels.duck, 1e-6)):.1f} dB")
        self.gr_label.setText("  ".join(bits))
