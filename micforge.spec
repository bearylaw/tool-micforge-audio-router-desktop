# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec.

    pip install pyinstaller
    pyinstaller micforge.spec

Produces a single folder under dist/MicForge. One-file mode works too but starts
noticeably slower, and an audio app that takes six seconds to appear feels broken.

The Qt modules MicForge does not use are excluded; without that the bundle picks
up WebEngine and friends and triples in size.
"""
import sys

block_cipher = None

hidden = [
    "micforge.audio.wasapi",
    "micforge.audio.pulse",
    "sounddevice",
    "soundfile",
    "av",
    "pynput.keyboard._win32" if sys.platform == "win32" else "pynput.keyboard._xorg",
]
if sys.platform == "win32":
    hidden += ["pycaw", "comtypes"]

excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick",
    "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
    "matplotlib", "tkinter", "IPython", "pytest", "setuptools",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MicForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # The window is the interface, but keep a console on Linux where people
    # routinely run --headless and --check from a terminal.
    console=sys.platform != "win32",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MicForge",
)
