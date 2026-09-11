"""Colour palette and stylesheet.

One dark theme, tuned for a window that sits next to a game at 2am: low
contrast background, bright accents only where something is live.
"""
from __future__ import annotations

BG = "#12141a"
PANEL = "#191d26"
PANEL_ALT = "#1f2430"
BORDER = "#2a3040"
BORDER_LIGHT = "#39415a"
TEXT = "#e7eaf2"
TEXT_MUTED = "#8b93a7"
TEXT_DIM = "#626b80"

ACCENT = "#3d7dff"
ACCENT_DIM = "#2c5ec4"
GOOD = "#35c07d"
WARN = "#e8a33d"
BAD = "#e05563"
LIVE = "#ff5c7a"

METER_LOW = "#35c07d"
METER_MID = "#d8c24a"
METER_HIGH = "#e05563"

RADIUS = "8px"


def stylesheet(accent: str = ACCENT) -> str:
    return f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", "Cantarell", "Helvetica Neue", sans-serif;
    font-size: 13px;
}}

/* Labels and check boxes must not paint the window colour, or every one of
   them shows as a darker strip against the lighter card behind it. */
QLabel, QCheckBox, QRadioButton, QGroupBox, QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QToolTip {{
    background: {PANEL_ALT};
    color: {TEXT};
    border: 1px solid {BORDER_LIGHT};
    padding: 6px 8px;
    border-radius: 6px;
}}

/* ---------------------------------------------------------------- tabs */
QTabWidget::pane {{
    border: none;
    background: {BG};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 10px 18px;
    margin-right: 2px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {accent};
}}

/* -------------------------------------------------------------- panels */
QFrame#Card {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
}}
QFrame#CardAlt {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
}}
QLabel#CardTitle {{
    color: {TEXT};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#CardHint, QLabel#Hint {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel#SectionLabel {{
    color: {TEXT_DIM};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#Value {{
    color: {TEXT};
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QLabel#Good {{ color: {GOOD}; }}
QLabel#Warn {{ color: {WARN}; }}
QLabel#Bad  {{ color: {BAD}; }}

/* ------------------------------------------------------------- buttons */
QPushButton {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 6px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: #262c3a; border-color: {accent}; }}
QPushButton:pressed {{ background: #2f3648; }}
QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BORDER}; }}
QPushButton:checked {{
    background: {accent};
    border-color: {accent};
    color: #ffffff;
}}
QPushButton#Primary {{
    background: {accent};
    border-color: {accent};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {ACCENT_DIM}; }}
QPushButton#Danger {{
    background: transparent;
    border-color: {BAD};
    color: {BAD};
}}
QPushButton#Danger:hover {{ background: {BAD}; color: #ffffff; }}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QPushButton#Ghost:hover {{ color: {TEXT}; border-color: {BORDER_LIGHT}; }}
QPushButton#Link {{
    background: transparent;
    border: none;
    color: {accent};
    text-align: left;
    padding: 2px;
}}

/* --------------------------------------------------------------- inputs */
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {accent};
}}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {accent};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER_LIGHT};
    selection-background-color: {accent};
    outline: none;
}}

/* -------------------------------------------------------------- sliders */
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {accent};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 13px;
    height: 13px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::groove:vertical {{
    width: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::add-page:vertical {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:vertical {{
    background: {TEXT};
    height: 13px;
    margin: 0 -5px;
    border-radius: 7px;
}}

/* ------------------------------------------------------------ checkboxes */
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_LIGHT};
    border-radius: 4px;
    background: {PANEL_ALT};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {accent};
    border-color: {accent};
}}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; }}

/* ----------------------------------------------------------------- lists */
QListWidget, QTreeWidget, QTableWidget {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    outline: none;
}}
QListWidget::item, QTreeWidget::item {{ padding: 6px; border-radius: 4px; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {accent};
    color: #ffffff;
}}
QHeaderView::section {{
    background: {PANEL_ALT};
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px;
}}

/* -------------------------------------------------------------- scrollbar */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_LIGHT}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_LIGHT}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------------------------------------------------------------- misc */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    margin-top: 14px;
    padding-top: 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {TEXT_MUTED};
}}
QStatusBar {{ background: {PANEL}; color: {TEXT_MUTED}; }}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background: {BORDER}; }}
QScrollArea {{ border: none; background: transparent; }}
QMenu {{
    background: {PANEL_ALT};
    border: 1px solid {BORDER_LIGHT};
    padding: 4px;
}}
QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background: {accent}; }}
"""
