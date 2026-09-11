"""Voice tab: presets on the left, the full effect stack on the right.

Controls are generated from ``dsp.chain.STAGE_META``, so an effect gains a
slider here the moment it gains a parameter there.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import log
from ..dsp import chain as chain_mod
from ..dsp import presets as presets_mod
from . import theme
from .widgets import Card, ChoiceRow, LevelMeter, Toggle, ValueSlider, hint, hline

_log = log.get("ui.voice")

BAND_TYPES = ["lowshelf", "peaking", "highshelf", "lowpass", "highpass", "notch"]


class VoiceTab(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.cfg = app.cfg
        self._loading = False
        self._stage_widgets: dict[str, dict] = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        root.addWidget(self._build_left(), 0)
        root.addWidget(self._build_right(), 1)

        self.reload_presets()
        self.load_from_config()

    # ---------------------------------------------------------------- left
    def _build_left(self) -> QWidget:
        holder = QWidget()
        holder.setFixedWidth(260)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        card = Card("Voice presets",
                    "A preset replaces the whole chain, so switching is predictable.")
        self.preset_list = QListWidget()
        self.preset_list.itemDoubleClicked.connect(lambda _i: self._apply_preset())
        card.add(self.preset_list)

        apply_button = QPushButton("Use this preset")
        apply_button.setObjectName("Primary")
        apply_button.clicked.connect(self._apply_preset)
        card.add(apply_button)

        row = QHBoxLayout()
        save = QPushButton("Save as...")
        save.setObjectName("Ghost")
        save.clicked.connect(self._save_preset)
        row.addWidget(save)
        delete = QPushButton("Delete")
        delete.setObjectName("Ghost")
        delete.clicked.connect(self._delete_preset)
        row.addWidget(delete)
        card.add_layout(row)
        column.addWidget(card)

        live = Card("Live")
        self.enable_toggle = Toggle("Effects on", True)
        self.enable_toggle.toggled.connect(self._on_enable)
        live.add(self.enable_toggle)

        self.bypass_button = QPushButton("Hold to compare (bypass)")
        self.bypass_button.setCheckable(True)
        self.bypass_button.pressed.connect(lambda: self._set_bypass(True))
        self.bypass_button.released.connect(lambda: self._set_bypass(False))
        live.add(self.bypass_button)

        self.wet = ValueSlider("Dry / wet", 0, 1, 0.01, "", 1.0, label_width=70)
        self.wet.valueChanged.connect(self._on_wet)
        live.add(self.wet)

        self.capture_fx = Toggle("Also process game audio", False,
                                 "Usually off - effects belong on your voice, not on "
                                 "the game.")
        self.capture_fx.toggled.connect(self._on_capture_fx)
        live.add(self.capture_fx)

        live.add(hline())
        meter_label = QLabel("Voice level")
        meter_label.setObjectName("CardHint")
        live.add(meter_label)
        self.meter = LevelMeter()
        live.add(self.meter)
        self.latency_label = QLabel("")
        self.latency_label.setObjectName("Value")
        live.add(self.latency_label)
        column.addWidget(live)

        column.addStretch(1)
        return holder

    # --------------------------------------------------------------- right
    def _build_right(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget()
        self.stack = QVBoxLayout(container)
        self.stack.setContentsMargins(0, 0, 8, 0)
        self.stack.setSpacing(12)

        header = Card("Effect chain",
                      "Runs top to bottom. Enable only what you need - every stage "
                      "adds a little latency and CPU.")
        self.active_label = QLabel("")
        self.active_label.setObjectName("CardHint")
        self.active_label.setWordWrap(True)
        header.add(self.active_label)
        self.stack.addWidget(header)

        for kind in self.cfg.voice.order:
            if kind in chain_mod.STAGE_META:
                self.stack.addWidget(self._build_stage(kind))
        for kind in chain_mod.STAGE_META:
            if kind not in self.cfg.voice.order:
                self.stack.addWidget(self._build_stage(kind))

        self.stack.addStretch(1)
        area.setWidget(container)
        return area

    def _build_stage(self, kind: str) -> Card:
        label, blurb, params = chain_mod.STAGE_META[kind]
        card = Card(alt=True)
        widgets: dict = {}

        head = QHBoxLayout()
        toggle = Toggle(label, False)
        toggle.setStyleSheet("font-weight: 600;")
        toggle.toggled.connect(lambda checked, k=kind: self._on_stage_toggle(k, checked))
        head.addWidget(toggle)
        head.addStretch(1)
        gr = QLabel("")
        gr.setObjectName("Value")
        head.addWidget(gr)
        card.add_layout(head)
        widgets["enabled"] = toggle
        widgets["gr"] = gr

        card.add(hint(blurb))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 0)
        body_layout.setSpacing(6)

        for key, plabel, widget_kind, lo, hi, step, suffix in params:
            if widget_kind == "bands":
                widgets[key] = self._build_eq(body_layout, kind)
            elif widget_kind.startswith("choice:"):
                options = widget_kind.split(":", 1)[1].split("|")
                row = ChoiceRow(plabel, options, label_width=96)
                row.valueChanged.connect(
                    lambda value, k=kind, p=key: self._on_param(k, p, value))
                body_layout.addWidget(row)
                widgets[key] = row
            elif widget_kind == "pct":
                row = ValueSlider(plabel, lo, hi, step, "", label_width=96, decimals=2)
                row.valueChanged.connect(
                    lambda value, k=kind, p=key: self._on_param(k, p, value))
                body_layout.addWidget(row)
                widgets[key] = row
            else:
                decimals = 0 if widget_kind == "int" else None
                row = ValueSlider(plabel, lo, hi, step, suffix, label_width=96,
                                  decimals=decimals)
                row.valueChanged.connect(
                    lambda value, k=kind, p=key: self._on_param(k, p, value))
                body_layout.addWidget(row)
                widgets[key] = row

        card.add(body)
        widgets["body"] = body
        self._stage_widgets[kind] = widgets
        return card

    def _build_eq(self, layout: QVBoxLayout, kind: str) -> list[dict]:
        rows: list[dict] = []
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        for column, title in enumerate(("Band", "Type", "Freq", "Gain", "Q")):
            header = QLabel(title)
            header.setObjectName("SectionLabel")
            grid.addWidget(header, 0, column)

        bands = self.cfg.fx("eq").get("bands", [])
        for index, _band in enumerate(bands):
            number = QLabel(str(index + 1))
            number.setObjectName("CardHint")
            grid.addWidget(number, index + 1, 0)

            type_combo = QComboBox()
            type_combo.addItems(BAND_TYPES)
            type_combo.currentTextChanged.connect(
                lambda value, i=index: self._on_band(i, "type", value))
            grid.addWidget(type_combo, index + 1, 1)

            freq = ValueSlider("", 20, 20000, 10, "Hz", label_width=0)
            freq.valueChanged.connect(lambda value, i=index: self._on_band(i, "freq", value))
            grid.addWidget(freq, index + 1, 2)

            gain = ValueSlider("", -24, 24, 0.5, "dB", label_width=0)
            gain.valueChanged.connect(
                lambda value, i=index: self._on_band(i, "gain_db", value))
            grid.addWidget(gain, index + 1, 3)

            q = ValueSlider("", 0.1, 8, 0.05, "", label_width=0, decimals=2)
            q.valueChanged.connect(lambda value, i=index: self._on_band(i, "q", value))
            grid.addWidget(q, index + 1, 4)

            rows.append({"type": type_combo, "freq": freq, "gain_db": gain, "q": q})

        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 2)
        grid.setColumnStretch(4, 1)
        layout.addLayout(grid)
        return rows

    # ------------------------------------------------------------- presets
    def reload_presets(self) -> None:
        self.preset_list.clear()
        for name in presets_mod.BUILTIN:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, ("builtin", name))
            self.preset_list.addItem(item)
        user = presets_mod.user_preset_names()
        if user:
            separator = QListWidgetItem("- my presets -")
            separator.setFlags(Qt.NoItemFlags)
            separator.setForeground(Qt.gray)
            self.preset_list.addItem(separator)
            for name in user:
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, ("user", name))
                self.preset_list.addItem(item)

        current = self.cfg.voice.preset_name
        for i in range(self.preset_list.count()):
            if self.preset_list.item(i).text() == current:
                self.preset_list.setCurrentRow(i)
                break

    def _apply_preset(self) -> None:
        item = self.preset_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            return
        _kind, name = item.data(Qt.UserRole)
        if presets_mod.apply_preset(self.cfg, name):
            self.load_from_config()
            self.app.apply_voice()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:",
                                        text=self.cfg.voice.preset_name)
        if not ok or not name.strip():
            return
        presets_mod.save_user(name.strip(), self.cfg)
        self.cfg.voice.preset_name = name.strip()
        self.app.save(0.1)
        self.reload_presets()

    def _delete_preset(self) -> None:
        item = self.preset_list.currentItem()
        if item is None or not item.data(Qt.UserRole):
            return
        kind, name = item.data(Qt.UserRole)
        if kind != "user":
            QMessageBox.information(self, "Built-in preset",
                                    "Built-in presets cannot be deleted. Save your own "
                                    "version with Save as... instead.")
            return
        if QMessageBox.question(self, "Delete preset",
                                f"Delete the preset '{name}'?") == QMessageBox.Yes:
            presets_mod.delete_user(name)
            self.reload_presets()

    # --------------------------------------------------------------- state
    def load_from_config(self) -> None:
        self._loading = True
        voice = self.cfg.voice
        self.enable_toggle.setChecked(voice.enabled)
        self.wet.set_value(voice.dry_wet)
        self.capture_fx.setChecked(voice.apply_to_capture)

        for kind, widgets in self._stage_widgets.items():
            params = self.cfg.fx(kind)
            widgets["enabled"].setChecked(bool(params.get("enabled")))
            widgets["body"].setEnabled(bool(params.get("enabled")))
            for key, widget in widgets.items():
                if key in ("enabled", "gr", "body"):
                    continue
                if isinstance(widget, list):
                    for index, row in enumerate(widget):
                        bands = params.get("bands") or []
                        if index >= len(bands):
                            continue
                        band = bands[index]
                        row["type"].blockSignals(True)
                        row["type"].setCurrentText(str(band.get("type", "peaking")))
                        row["type"].blockSignals(False)
                        row["freq"].set_value(float(band.get("freq", 1000)))
                        row["gain_db"].set_value(float(band.get("gain_db", 0)))
                        row["q"].set_value(float(band.get("q", 1.0)))
                elif isinstance(widget, ChoiceRow):
                    widget.set_value(str(params.get(key, "")))
                else:
                    widget.set_value(float(params.get(key, 0.0)))
        self._loading = False
        self._update_active_label()

    def _update_active_label(self) -> None:
        active = [chain_mod.STAGE_META[k][0] for k in self.cfg.voice.order
                  if k in chain_mod.STAGE_META and self.cfg.fx(k).get("enabled")]
        if active:
            self.active_label.setText("Active: " + "  ->  ".join(active))
            self.active_label.setStyleSheet(f"color: {theme.GOOD};")
        else:
            self.active_label.setText("Nothing enabled - your voice passes through "
                                      "untouched.")
            self.active_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")

    # ------------------------------------------------------------ handlers
    def _on_enable(self, checked: bool) -> None:
        if self._loading:
            return
        self.cfg.voice.enabled = bool(checked)
        self.app.apply_voice()

    def _set_bypass(self, on: bool) -> None:
        self.app.engine.chain.bypassed = bool(on)

    def _on_wet(self, value: float) -> None:
        if self._loading:
            return
        self.cfg.voice.dry_wet = float(value)
        self.app.apply_voice()

    def _on_capture_fx(self, checked: bool) -> None:
        if self._loading:
            return
        self.cfg.voice.apply_to_capture = bool(checked)
        self.app.apply_voice()

    def _on_stage_toggle(self, kind: str, checked: bool) -> None:
        if self._loading:
            return
        self.cfg.fx(kind)["enabled"] = bool(checked)
        self._stage_widgets[kind]["body"].setEnabled(bool(checked))
        self.cfg.voice.preset_name = self.cfg.voice.preset_name or "Custom"
        self._update_active_label()
        self.app.apply_voice()

    def _on_param(self, kind: str, key: str, value) -> None:
        if self._loading:
            return
        params = self.cfg.fx(kind)
        params[key] = value
        self.app.apply_voice()

    def _on_band(self, index: int, key: str, value) -> None:
        if self._loading:
            return
        bands = self.cfg.fx("eq").get("bands") or []
        if index < len(bands):
            bands[index][key] = value
            self.app.apply_voice()

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        levels = self.app.engine.levels
        self.meter.set_level(levels.mic_out)
        latency = self.app.engine.chain.latency
        ms = 1000.0 * latency / max(self.app.engine.samplerate, 1)
        self.latency_label.setText(f"chain adds {ms:.1f} ms")

        gate = self._stage_widgets.get("gate", {}).get("gr")
        if gate is not None:
            gate.setText(f"{levels.gate_gr_db:+.1f} dB" if levels.gate_gr_db < -0.1 else "")
        comp = self._stage_widgets.get("compressor", {}).get("gr")
        if comp is not None:
            comp.setText(f"{levels.comp_gr_db:+.1f} dB" if levels.comp_gr_db < -0.1 else "")
