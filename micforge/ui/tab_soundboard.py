"""Soundboard tab: a grid of pads, plus a full editor for the selected one."""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import config, log
from ..sound import decode
from . import theme
from .widgets import Card, HotkeyEdit, Toggle, ValueSlider, hint, hline, section

_log = log.get("ui.soundboard")


class SoundPad(QPushButton):
    """One soundboard button."""

    def __init__(self, entry: config.SoundEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.playing = False
        self.refresh()

    def refresh(self) -> None:
        label = self.entry.name or Path(self.entry.path).stem or "(empty)"
        if self.entry.hotkey:
            label += f"\n{self.entry.hotkey}"
        self.setText(label)
        colour = QColor(self.entry.color or theme.ACCENT)
        border = colour.name()
        background = theme.PANEL_ALT if not self.playing else colour.darker(160).name()
        self.setStyleSheet(
            f"QPushButton {{ background: {background}; border: 1px solid {border};"
            f" border-radius: 8px; padding: 8px; text-align: center;"
            f" color: {theme.TEXT}; }}"
            f"QPushButton:hover {{ background: {colour.darker(200).name()}; }}"
            f"QPushButton:pressed {{ background: {colour.darker(130).name()}; }}")

    def set_playing(self, playing: bool) -> None:
        if playing != self.playing:
            self.playing = playing
            self.refresh()


class SoundboardTab(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.cfg = app.cfg
        self._loading = False
        self._pads: dict[str, SoundPad] = {}
        self._selected: str = ""
        self.setAcceptDrops(True)

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)
        root.addWidget(self._build_grid(), 1)
        root.addWidget(self._build_editor(), 0)

        self.rebuild_grid()

    # ---------------------------------------------------------------- grid
    def _build_grid(self) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)

        toolbar = QHBoxLayout()
        add = QPushButton("Add sounds...")
        add.setObjectName("Primary")
        add.clicked.connect(self._add_files)
        toolbar.addWidget(add)

        stop = QPushButton("Stop all")
        stop.setObjectName("Danger")
        stop.clicked.connect(lambda: self.app.soundboard.stop_all())
        toolbar.addWidget(stop)

        toolbar.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("CardHint")
        toolbar.addWidget(self.count_label)
        column.addLayout(toolbar)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self.grid = QGridLayout(inner)
        self.grid.setContentsMargins(0, 0, 8, 0)
        self.grid.setSpacing(10)
        area.setWidget(inner)
        column.addWidget(area, 1)

        self.drop_hint = QLabel("Drag MP3 or WAV files anywhere on this tab to add them.")
        self.drop_hint.setObjectName("CardHint")
        self.drop_hint.setAlignment(Qt.AlignCenter)
        column.addWidget(self.drop_hint)
        return holder

    def rebuild_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._pads.clear()

        columns = max(1, int(self.cfg.soundboard.grid_columns))
        for index, entry in enumerate(self.cfg.soundboard.sounds):
            pad = SoundPad(entry)
            pad.clicked.connect(lambda _c=False, e=entry: self._play(e))
            pad.customContextMenuRequested.connect(
                lambda pos, e=entry, p=pad: self._pad_menu(e, p, pos))
            self.grid.addWidget(pad, index // columns, index % columns)
            self._pads[entry.id] = pad
        self.grid.setRowStretch(self.grid.rowCount(), 1)
        self.count_label.setText(f"{len(self.cfg.soundboard.sounds)} sounds")
        if self.cfg.soundboard.sounds and not self._selected:
            self._select(self.cfg.soundboard.sounds[0].id)
        elif not self.cfg.soundboard.sounds:
            self._select("")

    def _play(self, entry: config.SoundEntry) -> None:
        self._select(entry.id)
        if not self.app.soundboard.play(entry):
            QMessageBox.warning(self, "Cannot play",
                                self.app.soundboard.last_error or "Unknown error")

    def _pad_menu(self, entry: config.SoundEntry, pad: SoundPad, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play(entry))
        menu.addAction("Stop", lambda: self.app.soundboard.stop_entry(entry.id))
        menu.addSeparator()
        menu.addAction("Edit", lambda: self._select(entry.id))
        menu.addAction("Pick colour", lambda: self._pick_colour(entry))
        menu.addSeparator()
        menu.addAction("Move left", lambda: self._move(entry, -1))
        menu.addAction("Move right", lambda: self._move(entry, 1))
        menu.addSeparator()
        menu.addAction("Remove", lambda: self._remove(entry))
        menu.exec(pad.mapToGlobal(pos))

    def _move(self, entry: config.SoundEntry, delta: int) -> None:
        sounds = self.cfg.soundboard.sounds
        try:
            index = sounds.index(entry)
        except ValueError:
            return
        target = max(0, min(len(sounds) - 1, index + delta))
        if target == index:
            return
        sounds.insert(target, sounds.pop(index))
        self.app.save(0.2)
        self.rebuild_grid()

    def _remove(self, entry: config.SoundEntry) -> None:
        if QMessageBox.question(
                self, "Remove sound",
                f"Remove '{entry.name}' from the board?\n\n"
                "The file on disk is not deleted.") != QMessageBox.Yes:
            return
        self.cfg.soundboard.sounds = [s for s in self.cfg.soundboard.sounds
                                      if s.id != entry.id]
        self.cfg.discord.triggers = [t for t in self.cfg.discord.triggers
                                     if t.sound_id != entry.id]
        if self._selected == entry.id:
            self._selected = ""
        self.app.save(0.2)
        self.app.rebuild_hotkeys()
        self.rebuild_grid()

    def _pick_colour(self, entry: config.SoundEntry) -> None:
        colour = QColorDialog.getColor(QColor(entry.color or theme.ACCENT), self,
                                       "Pad colour")
        if colour.isValid():
            entry.color = colour.name()
            self.app.save(0.2)
            pad = self._pads.get(entry.id)
            if pad:
                pad.refresh()

    # -------------------------------------------------------------- editor
    def _build_editor(self) -> QWidget:
        holder = QWidget()
        holder.setFixedWidth(340)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(12)

        self.editor = Card("Selected sound")
        self.file_label = QLabel("Nothing selected")
        self.file_label.setObjectName("CardHint")
        self.file_label.setWordWrap(True)
        self.editor.add(self.file_label)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name shown on the pad")
        self.name_edit.textEdited.connect(self._on_name)
        self.editor.add(self.name_edit)

        self.hotkey = HotkeyEdit(self.app.hotkeys)
        self.hotkey.changed.connect(self._on_hotkey)
        self.editor.add(self.hotkey)

        self.gain = ValueSlider("Volume", -40, 24, 0.5, "dB", 0, label_width=84)
        self.gain.valueChanged.connect(self._on_field)
        self.editor.add(self.gain)

        self.pitch = ValueSlider("Pitch", -24, 24, 0.5, "st", 0, label_width=84)
        self.pitch.valueChanged.connect(self._on_field)
        self.editor.add(self.pitch)
        self.editor.add(hint("Pitch on a clip works like tape speed - it changes the "
                             "length too."))

        self.speed = ValueSlider("Speed", 0.25, 3.0, 0.05, "x", 1.0, label_width=84,
                                 decimals=2)
        self.speed.valueChanged.connect(self._on_field)
        self.editor.add(self.speed)

        self.editor.add(hline())
        self.editor.add(section("Trim and fades"))
        self.start_ms = ValueSlider("Start", 0, 60000, 50, "ms", 0, label_width=84)
        self.end_ms = ValueSlider("End", 0, 60000, 50, "ms", 0, label_width=84)
        self.fade_in = ValueSlider("Fade in", 0, 5000, 10, "ms", 0, label_width=84)
        self.fade_out = ValueSlider("Fade out", 0, 5000, 10, "ms", 30, label_width=84)
        for slider in (self.start_ms, self.end_ms, self.fade_in, self.fade_out):
            slider.valueChanged.connect(self._on_field)
            self.editor.add(slider)
        self.editor.add(hint("End at 0 means play to the end of the file."))

        self.editor.add(hline())
        self.editor.add(section("Routing"))
        self.to_mic = Toggle("Send to Discord", True)
        self.to_monitor = Toggle("I hear it too", True)
        self.loop = Toggle("Loop", False)
        self.stop_others = Toggle("Stop other sounds first", False)
        for toggle in (self.to_mic, self.to_monitor, self.loop, self.stop_others):
            toggle.toggled.connect(self._on_field)
            self.editor.add(toggle)

        preview = QHBoxLayout()
        play = QPushButton("Preview")
        play.clicked.connect(self._preview)
        preview.addWidget(play)
        stop = QPushButton("Stop")
        stop.setObjectName("Ghost")
        stop.clicked.connect(self._stop_selected)
        preview.addWidget(stop)
        self.editor.add_layout(preview)
        column.addWidget(self.editor)

        board = Card("Board settings")
        self.columns = ValueSlider("Columns", 1, 8, 1, "", 4, label_width=96)
        self.columns.valueChanged.connect(self._on_board)
        board.add(self.columns)
        self.max_voices = ValueSlider("Max at once", 1, 16, 1, "", 8, label_width=96)
        self.max_voices.valueChanged.connect(self._on_board)
        board.add(self.max_voices)
        self.global_gain = ValueSlider("Board volume", -24, 12, 0.5, "dB", 0,
                                       label_width=96)
        self.global_gain.valueChanged.connect(self._on_board)
        board.add(self.global_gain)

        self.overlap = Toggle("Allow overlapping clips", True)
        self.restart = Toggle("Restart a clip if pressed again", True)
        self.hotkeys_on = Toggle("Global hotkeys", True)
        for toggle in (self.overlap, self.restart, self.hotkeys_on):
            toggle.toggled.connect(self._on_board)
            board.add(toggle)

        stop_row = QLabel("Stop-all hotkey")
        stop_row.setObjectName("CardHint")
        board.add(stop_row)
        self.stop_hotkey = HotkeyEdit(self.app.hotkeys)
        self.stop_hotkey.changed.connect(self._on_board)
        board.add(self.stop_hotkey)

        self.hotkey_note = QLabel("")
        self.hotkey_note.setObjectName("CardHint")
        self.hotkey_note.setWordWrap(True)
        board.add(self.hotkey_note)
        column.addWidget(board)

        column.addStretch(1)
        area.setWidget(inner)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(area)
        self._load_board_settings()
        return holder

    def _load_board_settings(self) -> None:
        self._loading = True
        sb = self.cfg.soundboard
        self.columns.set_value(sb.grid_columns)
        self.max_voices.set_value(sb.max_voices)
        self.global_gain.set_value(sb.global_gain_db)
        self.overlap.setChecked(sb.overlap)
        self.restart.setChecked(sb.restart_on_retrigger)
        self.hotkeys_on.setChecked(sb.hotkeys_enabled)
        self.stop_hotkey.set_value(sb.stop_all_hotkey)
        from .. import hotkeys as hk

        self.hotkey_note.setText(hk.describe_availability())
        self._loading = False

    def _select(self, sound_id: str) -> None:
        self._selected = sound_id
        entry = self.cfg.sound_by_id(sound_id) if sound_id else None
        self._loading = True
        enabled = entry is not None
        for widget in (self.name_edit, self.gain, self.pitch, self.speed,
                       self.start_ms, self.end_ms, self.fade_in, self.fade_out,
                       self.to_mic, self.to_monitor, self.loop, self.stop_others,
                       self.hotkey):
            widget.setEnabled(enabled)

        if entry is None:
            self.file_label.setText("Nothing selected")
            self.name_edit.setText("")
            self._loading = False
            return

        path = Path(entry.path)
        duration, samplerate, channels = decode.probe(entry.path)
        details = f"{path.name}"
        if duration:
            details += f"  -  {duration:.1f}s, {samplerate} Hz, {channels} ch"
        if not path.exists():
            details += "  -  FILE MISSING"
        self.file_label.setText(details)
        self.file_label.setStyleSheet(
            f"color: {theme.BAD};" if not path.exists() else f"color: {theme.TEXT_MUTED};")

        self.name_edit.setText(entry.name)
        self.hotkey.set_value(entry.hotkey)
        self.gain.set_value(entry.gain_db)
        self.pitch.set_value(entry.pitch_semitones)
        self.speed.set_value(entry.speed)
        self.start_ms.set_value(entry.start_ms)
        self.end_ms.set_value(entry.end_ms)
        self.fade_in.set_value(entry.fade_in_ms)
        self.fade_out.set_value(entry.fade_out_ms)
        self.to_mic.setChecked(entry.to_mic)
        self.to_monitor.setChecked(entry.to_monitor)
        self.loop.setChecked(entry.loop)
        self.stop_others.setChecked(entry.stop_others)
        self._loading = False

    # ------------------------------------------------------------ handlers
    def _current(self) -> config.SoundEntry | None:
        return self.cfg.sound_by_id(self._selected) if self._selected else None

    def _on_name(self, text: str) -> None:
        entry = self._current()
        if entry is None or self._loading:
            return
        entry.name = text
        pad = self._pads.get(entry.id)
        if pad:
            pad.refresh()
        self.app.save()

    def _on_hotkey(self, spec: str) -> None:
        entry = self._current()
        if entry is None or self._loading:
            return
        entry.hotkey = spec
        pad = self._pads.get(entry.id)
        if pad:
            pad.refresh()
        self.app.rebuild_hotkeys()
        self.app.save(0.2)

    def _on_field(self, *_args) -> None:
        entry = self._current()
        if entry is None or self._loading:
            return
        entry.gain_db = self.gain.value()
        entry.pitch_semitones = self.pitch.value()
        entry.speed = self.speed.value()
        entry.start_ms = self.start_ms.value()
        entry.end_ms = self.end_ms.value()
        entry.fade_in_ms = self.fade_in.value()
        entry.fade_out_ms = self.fade_out.value()
        entry.to_mic = self.to_mic.isChecked()
        entry.to_monitor = self.to_monitor.isChecked()
        entry.loop = self.loop.isChecked()
        entry.stop_others = self.stop_others.isChecked()
        self.app.save()

    def _on_board(self, *_args) -> None:
        if self._loading:
            return
        sb = self.cfg.soundboard
        old_columns = sb.grid_columns
        sb.grid_columns = int(self.columns.value())
        sb.max_voices = int(self.max_voices.value())
        sb.global_gain_db = self.global_gain.value()
        sb.overlap = self.overlap.isChecked()
        sb.restart_on_retrigger = self.restart.isChecked()
        sb.hotkeys_enabled = self.hotkeys_on.isChecked()
        sb.stop_all_hotkey = self.stop_hotkey.value()
        self.app.rebuild_hotkeys()
        if sb.hotkeys_enabled and not self.app.hotkeys.running:
            self.app.hotkeys.start()
        self.app.save(0.2)
        if sb.grid_columns != old_columns:
            self.rebuild_grid()

    def _preview(self) -> None:
        entry = self._current()
        if entry is not None:
            self._play(entry)

    def _stop_selected(self) -> None:
        if self._selected:
            self.app.soundboard.stop_entry(self._selected)

    # ----------------------------------------------------------- adding
    def _add_files(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(decode.SUPPORTED_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add sounds", "", f"Audio files ({patterns});;All files (*)")
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths) -> int:
        added = 0
        errors = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    if decode.is_supported(child):
                        if self._add_one(child, errors):
                            added += 1
                continue
            if not decode.is_supported(path):
                errors.append(f"{path.name}: unsupported format")
                continue
            if self._add_one(path, errors):
                added += 1

        if added:
            self.app.save(0.2)
            self.rebuild_grid()
        if errors:
            QMessageBox.warning(self, "Some files were skipped", "\n".join(errors[:10]))
        return added

    def _add_one(self, path: Path, errors: list) -> bool:
        try:
            decode.probe(path)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            return False
        entry = config.SoundEntry(name=path.stem, path=str(path))
        self.cfg.soundboard.sounds.append(entry)
        return True

    # ------------------------------------------------------- drag and drop
    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_hint.setText("Drop to add")
            self.drop_hint.setStyleSheet(f"color: {theme.ACCENT};")

    def dragLeaveEvent(self, event):  # noqa: N802
        self._reset_drop_hint()

    def dropEvent(self, event):  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile()]
        self._reset_drop_hint()
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def _reset_drop_hint(self) -> None:
        self.drop_hint.setText("Drag MP3 or WAV files anywhere on this tab to add them.")
        self.drop_hint.setStyleSheet(f"color: {theme.TEXT_MUTED};")

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        active = {sound_id for sound_id, _name, _progress in self.app.soundboard.active()}
        for sound_id, pad in self._pads.items():
            pad.set_playing(sound_id in active)

    def copy_into_library(self, path: Path) -> Path:
        """Optional helper: keep a copy next to the config so the pad never breaks."""
        target_dir = config.sounds_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if not target.exists():
            shutil.copy2(path, target)
        return target
