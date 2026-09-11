"""Main window: header, tabs, tray icon and the repaint timer."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSystemTrayIcon, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import log
from . import theme
from .tab_discord import DiscordTab
from .tab_mix import MixTab
from .tab_settings import SettingsTab
from .tab_soundboard import SoundboardTab
from .tab_voice import VoiceTab
from .widgets import LevelMeter

_log = log.get("ui")


def make_icon(accent: str = theme.ACCENT, size: int = 64) -> QIcon:
    """Draw the tray/window icon rather than shipping a binary asset."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    painter.setBrush(QColor(theme.PANEL_ALT))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)

    painter.setBrush(QColor(accent))
    capsule_w = size * 0.28
    capsule_h = size * 0.42
    x = (size - capsule_w) / 2
    y = size * 0.16
    painter.drawRoundedRect(int(x), int(y), int(capsule_w), int(capsule_h),
                            int(capsule_w / 2), int(capsule_w / 2))

    pen = QPen(QColor(accent))
    pen.setWidth(max(2, int(size * 0.055)))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    arc_size = int(size * 0.46)
    painter.drawArc(int((size - arc_size) / 2), int(size * 0.34), arc_size,
                    arc_size, 0, -180 * 16)
    painter.drawLine(int(size / 2), int(size * 0.72), int(size / 2), int(size * 0.85))
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cfg = app.cfg
        self._quitting = False

        self.setWindowTitle("MicForge")
        self.setMinimumSize(1020, 720)
        self.setWindowIcon(make_icon(self.cfg.ui.accent))

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(self._build_hints())

        self.tabs = QTabWidget()
        self.mix_tab = MixTab(app)
        self.voice_tab = VoiceTab(app)
        self.soundboard_tab = SoundboardTab(app)
        self.discord_tab = DiscordTab(app)
        self.settings_tab = SettingsTab(app)
        self.tabs.addTab(self.mix_tab, "Mix")
        self.tabs.addTab(self.voice_tab, "Voice")
        self.tabs.addTab(self.soundboard_tab, "Soundboard")
        self.tabs.addTab(self.discord_tab, "Discord")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.setCurrentIndex(min(self.cfg.ui.last_tab, self.tabs.count() - 1))
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status.showMessage("Starting...")

        self._build_tray()

        interval = max(20, int(1000 / max(self.cfg.ui.meter_fps, 1)))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(interval)

        self._slow = QTimer(self)
        self._slow.timeout.connect(self._slow_tick)
        self._slow.start(1000)

        if self.cfg.ui.window_geometry:
            try:
                from PySide6.QtCore import QByteArray

                self.restoreGeometry(QByteArray.fromBase64(
                    self.cfg.ui.window_geometry.encode()))
            except Exception:
                pass

    # -------------------------------------------------------------- header
    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet(f"background: {theme.PANEL}; "
                          f"border-bottom: 1px solid {theme.BORDER};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(18, 12, 18, 12)
        row.setSpacing(14)

        title = QLabel("MicForge")
        font = QFont()
        font.setPointSize(13)
        font.setWeight(QFont.DemiBold)
        title.setFont(font)
        row.addWidget(title)

        self.engine_button = QPushButton("Engine on")
        self.engine_button.setCheckable(True)
        self.engine_button.setChecked(True)
        self.engine_button.clicked.connect(self._toggle_engine)
        row.addWidget(self.engine_button)

        self.mute_button = QPushButton("Mute mic")
        self.mute_button.setCheckable(True)
        self.mute_button.clicked.connect(self._toggle_mute)
        row.addWidget(self.mute_button)

        panic = QPushButton("Stop sounds")
        panic.setObjectName("Danger")
        panic.clicked.connect(lambda: self.app.soundboard.stop_all())
        row.addWidget(panic)

        row.addStretch(1)

        out_label = QLabel("To Discord")
        out_label.setObjectName("CardHint")
        row.addWidget(out_label)
        self.header_meter = LevelMeter()
        self.header_meter.setFixedWidth(170)
        row.addWidget(self.header_meter)

        self.header_status = QLabel("")
        self.header_status.setObjectName("CardHint")
        row.addWidget(self.header_status)
        return bar

    def _build_hints(self) -> QWidget:
        self.hint_bar = QWidget()
        self.hint_bar.setStyleSheet(
            f"background: {theme.PANEL_ALT}; border-bottom: 1px solid {theme.BORDER};")
        row = QHBoxLayout(self.hint_bar)
        row.setContentsMargins(18, 8, 18, 8)
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet(f"color: {theme.WARN};")
        row.addWidget(self.hint_label, 1)
        dismiss = QPushButton("Hide")
        dismiss.setObjectName("Ghost")
        dismiss.setFixedWidth(56)
        dismiss.clicked.connect(lambda: self.hint_bar.setVisible(False))
        row.addWidget(dismiss)
        self.hint_bar.setVisible(False)
        return self.hint_bar

    # ---------------------------------------------------------------- tray
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(self.cfg.ui.accent), self)
        self.tray.setToolTip("MicForge")
        menu = QMenu()

        show = QAction("Show MicForge", self)
        show.triggered.connect(self._restore)
        menu.addAction(show)

        self.tray_mute = QAction("Mute microphone", self, checkable=True)
        self.tray_mute.triggered.connect(self._toggle_mute)
        menu.addAction(self.tray_mute)

        stop = QAction("Stop all sounds", self)
        stop.triggered.connect(lambda: self.app.soundboard.stop_all())
        menu.addAction(stop)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._restore()

    def _restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ handlers
    def _toggle_engine(self, checked: bool) -> None:
        if checked:
            self.app.engine.start()
            self.engine_button.setText("Engine on")
        else:
            self.app.engine.stop()
            self.engine_button.setText("Engine off")

    def _toggle_mute(self, *_args) -> None:
        muted = not self.cfg.mixer.mic_muted
        self.cfg.mixer.mic_muted = muted
        self.mute_button.setChecked(muted)
        self.mute_button.setText("Mic muted" if muted else "Mute mic")
        self.tray_mute.setChecked(muted)
        self.app.save()

    def _on_tab_changed(self, index: int) -> None:
        self.cfg.ui.last_tab = index
        self.app.save(2.0)
        if self.tabs.widget(index) is self.discord_tab:
            self.discord_tab.refresh_sound_choices()
            self.discord_tab.refresh_rules()

    # ---------------------------------------------------------------- tick
    def _tick(self) -> None:
        self.header_meter.set_level(self.app.engine.levels.output)
        current = self.tabs.currentWidget()
        tick = getattr(current, "tick", None)
        if tick is not None:
            try:
                tick()
            except Exception:
                _log.exception("tab tick failed")

    def _slow_tick(self) -> None:
        self.status.showMessage(self.app.status_line())
        st = self.app.engine.status
        if st.errors:
            self.header_status.setText("audio problem")
            self.header_status.setStyleSheet(f"color: {theme.BAD};")
        elif not st.running:
            self.header_status.setText("stopped")
            self.header_status.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        else:
            self.header_status.setText("live")
            self.header_status.setStyleSheet(f"color: {theme.GOOD};")

        hints = self.app.first_run_hints()
        if hints and not self.hint_bar.isVisible():
            self.hint_label.setText(hints[0])
            self.hint_bar.setVisible(True)
        elif hints:
            self.hint_label.setText(hints[0])
        elif not hints and self.hint_bar.isVisible():
            self.hint_bar.setVisible(False)

        self.engine_button.setChecked(st.running)
        self.engine_button.setText("Engine on" if st.running else "Engine off")
        self.mute_button.setChecked(self.cfg.mixer.mic_muted)
        self.mute_button.setText("Mic muted" if self.cfg.mixer.mic_muted else "Mute mic")

    # --------------------------------------------------------------- close
    def closeEvent(self, event):  # noqa: N802
        if self._quitting or not self.cfg.ui.minimise_to_tray:
            self._save_geometry()
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "MicForge is still running",
            "Your microphone routing stays active. Quit from the tray icon to stop.",
            QSystemTrayIcon.Information, 4000)

    def _save_geometry(self) -> None:
        try:
            self.cfg.ui.window_geometry = bytes(
                self.saveGeometry().toBase64()).decode()
        except Exception:
            pass

    def _quit(self) -> None:
        if self.cfg.ui.confirm_exit:
            answer = QMessageBox.question(self, "Quit MicForge",
                                          "Stop routing audio and quit?")
            if answer != QMessageBox.Yes:
                return
        self._quitting = True
        self._save_geometry()
        from PySide6.QtWidgets import QApplication

        self.tray.hide()
        QApplication.instance().quit()
