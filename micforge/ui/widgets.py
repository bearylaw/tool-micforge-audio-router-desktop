"""Reusable widgets: cards, meters, labelled sliders, hotkey capture."""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import theme


class Card(QFrame):
    """A titled panel. Everything in the app lives in one of these."""

    def __init__(self, title: str = "", hint: str = "", parent=None, alt: bool = False):
        super().__init__(parent)
        self.setObjectName("CardAlt" if alt else "Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(10)

        if title:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("CardTitle")
            self._layout.addWidget(self.title_label)
        if hint:
            self.hint_label = QLabel(hint)
            self.hint_label.setObjectName("CardHint")
            self.hint_label.setWordWrap(True)
            self._layout.addWidget(self.hint_label)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget) -> QWidget:
        self._layout.addWidget(widget)
        return widget

    def add_layout(self, layout):
        self._layout.addLayout(layout)
        return layout


class LevelMeter(QWidget):
    """Horizontal peak meter with a slow-decay peak-hold marker.

    Decay is deliberate: a bar that tracks the signal exactly is unreadable at
    30 fps, and the hold line is what tells you whether you are clipping.
    """

    def __init__(self, parent=None, horizontal: bool = True, show_scale: bool = False):
        super().__init__(parent)
        self.horizontal = horizontal
        self.show_scale = show_scale
        self._level = 0.0
        self._display = 0.0
        self._peak = 0.0
        self._peak_age = 0
        self._clip_age = 0
        if horizontal:
            self.setMinimumHeight(10)
            self.setMaximumHeight(14 if not show_scale else 22)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.setMinimumWidth(10)
            self.setMaximumWidth(14)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def set_level(self, value: float) -> None:
        self._level = max(0.0, min(1.5, float(value)))
        if self._level >= 0.995:
            self._clip_age = 45
        # Attack instantly, release slowly - standard meter ballistics.
        if self._level > self._display:
            self._display = self._level
        else:
            self._display += (self._level - self._display) * 0.25
        if self._display > self._peak:
            self._peak = self._display
            self._peak_age = 0
        else:
            self._peak_age += 1
            if self._peak_age > 25:
                self._peak = max(self._display, self._peak - 0.02)
        if self._clip_age:
            self._clip_age -= 1
        self.update()

    @staticmethod
    def _to_position(level: float) -> float:
        """Map linear amplitude onto the bar using a dB-ish scale (-60..0)."""
        if level <= 0.0:
            return 0.0
        import math

        db = 20.0 * math.log10(max(level, 1e-6))
        return max(0.0, min(1.0, (db + 60.0) / 60.0))

    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.BORDER))

        pos = self._to_position(self._display)
        if self.horizontal:
            width = int(rect.width() * pos)
            grad = QLinearGradient(QPointF(0, 0), QPointF(rect.width(), 0))
        else:
            width = int(rect.height() * pos)
            grad = QLinearGradient(QPointF(0, rect.height()), QPointF(0, 0))
        grad.setColorAt(0.0, QColor(theme.METER_LOW))
        grad.setColorAt(0.72, QColor(theme.METER_LOW))
        grad.setColorAt(0.88, QColor(theme.METER_MID))
        grad.setColorAt(1.0, QColor(theme.METER_HIGH))

        if width > 0:
            if self.horizontal:
                painter.fillRect(0, 0, width, rect.height(), grad)
            else:
                painter.fillRect(0, rect.height() - width, rect.width(), width, grad)

        peak_pos = self._to_position(self._peak)
        if peak_pos > 0.01:
            pen = QPen(QColor(theme.TEXT))
            pen.setWidth(2)
            painter.setPen(pen)
            if self.horizontal:
                x = int(rect.width() * peak_pos) - 1
                painter.drawLine(x, 0, x, rect.height())
            else:
                y = rect.height() - int(rect.height() * peak_pos)
                painter.drawLine(0, y, rect.width(), y)

        if self._clip_age:
            painter.fillRect(rect.width() - 4, 0, 4, rect.height(),
                             QColor(theme.BAD))
        painter.end()


class ValueSlider(QWidget):
    """Label + slider + readout, working in float units.

    Qt sliders are integer-only, so everything is scaled by ``1/step``.
    """

    valueChanged = Signal(float)

    def __init__(self, label: str, minimum: float, maximum: float, step: float = 0.1,
                 suffix: str = "", value: float = 0.0, parent=None,
                 label_width: int = 110, decimals: int | None = None):
        super().__init__(parent)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.step = float(step) if step else 0.1
        self.suffix = suffix
        if decimals is None:
            decimals = 0 if self.step >= 1 else (1 if self.step >= 0.1 else 2)
        self.decimals = decimals
        self._emit = True

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.name = QLabel(label)
        self.name.setObjectName("CardHint")
        self.name.setMinimumWidth(label_width)
        row.addWidget(self.name)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(int(round(self.minimum / self.step)))
        self.slider.setMaximum(int(round(self.maximum / self.step)))
        self.slider.valueChanged.connect(self._on_slider)
        row.addWidget(self.slider, 1)

        self.readout = QLabel("")
        self.readout.setObjectName("Value")
        self.readout.setMinimumWidth(64)
        self.readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.readout)

        self.set_value(value)

    def _on_slider(self, raw: int) -> None:
        value = raw * self.step
        self.readout.setText(self._format(value))
        if self._emit:
            self.valueChanged.emit(value)

    def _format(self, value: float) -> str:
        text = f"{value:.{self.decimals}f}"
        return f"{text} {self.suffix}".strip()

    def value(self) -> float:
        return self.slider.value() * self.step

    def set_value(self, value: float) -> None:
        self._emit = False
        raw = int(round(float(value) / self.step))
        raw = max(self.slider.minimum(), min(self.slider.maximum(), raw))
        self.slider.setValue(raw)
        self.readout.setText(self._format(raw * self.step))
        self._emit = True

    def set_enabled(self, on: bool) -> None:
        self.slider.setEnabled(on)
        self.name.setEnabled(on)
        self.readout.setEnabled(on)


class ChoiceRow(QWidget):
    """Label + combo box over a fixed set of string options."""

    valueChanged = Signal(str)

    def __init__(self, label: str, options: list[str], value: str = "",
                 parent=None, label_width: int = 110):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        name = QLabel(label)
        name.setObjectName("CardHint")
        name.setMinimumWidth(label_width)
        row.addWidget(name)
        self.combo = QComboBox()
        self.combo.addItems(options)
        if value in options:
            self.combo.setCurrentText(value)
        self.combo.currentTextChanged.connect(self.valueChanged.emit)
        row.addWidget(self.combo, 1)

    def value(self) -> str:
        return self.combo.currentText()

    def set_value(self, value: str) -> None:
        self.combo.blockSignals(True)
        idx = self.combo.findText(value)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)


class Toggle(QCheckBox):
    def __init__(self, text: str, checked: bool = False, hint: str = "", parent=None):
        super().__init__(text, parent)
        self.setChecked(bool(checked))
        if hint:
            self.setToolTip(hint)


class HotkeyEdit(QWidget):
    """Click, then press a key combination. Needs the global listener running."""

    changed = Signal(str)

    def __init__(self, manager, value: str = "", parent=None):
        super().__init__(parent)
        self.manager = manager
        self._value = value
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.button = QPushButton(value or "Click to set")
        self.button.setCheckable(True)
        self.button.clicked.connect(self._begin)
        row.addWidget(self.button, 1)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("Ghost")
        self.clear_button.setFixedWidth(58)
        self.clear_button.clicked.connect(self._clear)
        row.addWidget(self.clear_button)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._cancel)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value or ""
        self.button.setText(self._value or "Click to set")

    def _begin(self) -> None:
        if not self.button.isChecked():
            self._cancel()
            return
        if self.manager is None or not self.manager.available:
            self.button.setChecked(False)
            self.button.setText("Hotkeys unavailable")
            return
        if not self.manager.running:
            self.manager.start()
        self.button.setText("Press keys...")
        self.manager.capture_next(self._captured)
        self._timeout.start(6000)

    def _captured(self, spec: str) -> None:
        # Arrives on the listener thread; bounce to the UI thread.
        QTimer.singleShot(0, lambda: self._apply(spec))

    def _apply(self, spec: str) -> None:
        self._timeout.stop()
        self.button.setChecked(False)
        self.set_value(spec)
        self.changed.emit(spec)

    def _cancel(self) -> None:
        self._timeout.stop()
        if self.manager is not None:
            self.manager.cancel_capture()
        self.button.setChecked(False)
        self.button.setText(self._value or "Click to set")

    def _clear(self) -> None:
        self._cancel()
        self.set_value("")
        self.changed.emit("")


def section(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("SectionLabel")
    return label


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("CardHint")
    label.setWordWrap(True)
    return label


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color: {theme.BORDER}; background: {theme.BORDER};")
    line.setFixedHeight(1)
    return line


def meter_row(label: str, label_width: int = 110) -> tuple[QWidget, LevelMeter]:
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    name = QLabel(label)
    name.setObjectName("CardHint")
    name.setMinimumWidth(label_width)
    row.addWidget(name)
    meter = LevelMeter()
    row.addWidget(meter, 1)
    return holder, meter
