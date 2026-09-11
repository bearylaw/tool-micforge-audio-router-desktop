"""Entry point.

``python -m micforge``               launch the window
``python -m micforge --headless``    run the router with no UI
``python -m micforge --list-devices``print the device list and exit
``python -m micforge --play <id>``   trigger one soundboard sound in a running copy
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import APP_NAME, __version__, config, log


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="micforge",
        description="Route your microphone, any application's audio and a soundboard "
                    "into a virtual microphone that Discord can use.")
    parser.add_argument("--headless", action="store_true",
                        help="run without the window (audio routing and triggers only)")
    parser.add_argument("--list-devices", action="store_true",
                        help="print every audio device and exit")
    parser.add_argument("--list-sources", action="store_true",
                        help="print capturable applications and exit")
    parser.add_argument("--list-sounds", action="store_true",
                        help="print the configured soundboard entries and exit")
    parser.add_argument("--play", metavar="SOUND",
                        help="play one sound by id or name, then exit (headless)")
    parser.add_argument("--check", action="store_true",
                        help="run a self-test of the audio path and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def _print_devices() -> None:
    from .audio import devices

    print("Input devices")
    for dev in devices.input_devices():
        print(f"  {dev.label}")
    print("\nOutput devices")
    for dev in devices.output_devices():
        mark = "  <- virtual cable" if dev.is_virtual_output else ""
        print(f"  {dev.label}{mark}")
    status = devices.virtual_cable_status()
    print(f"\nVirtual microphone: "
          f"{'found - ' + status.output_name if status.installed else 'not found'}")
    if not status.installed:
        print(f"  {status.install_hint}")


def _print_sources() -> None:
    from .audio import sources

    print(f"{'PID':>8}  {'KIND':<8} {'PLAYING':<8} NAME")
    for src in sources.list_sources():
        print(f"{src.pid:>8}  {src.kind:<8} {'yes' if src.playing else '-':<8} "
              f"{src.label}")


def _self_check(cfg: config.Config) -> int:
    """Verify the pieces without needing a window or a virtual cable."""
    import numpy as np

    from .audio import devices
    from .dsp import chain as chain_mod

    ok = True
    print(f"{APP_NAME} {__version__} self-check\n")

    chain = chain_mod.VoiceChain(cfg.devices.samplerate)
    chain.configure(cfg.voice, cfg.fx)
    block = np.zeros(cfg.devices.blocksize, dtype=np.float32)
    out = chain.process(block)
    good = out.shape == block.shape and np.all(np.isfinite(out))
    print(f"  voice chain            : {'ok' if good else 'FAILED'}")
    ok &= good

    mic = devices.default_device("input")
    print(f"  default microphone     : {devices.describe_device(mic)}")
    ok &= mic is not None

    status = devices.virtual_cable_status()
    print(f"  virtual microphone     : "
          f"{status.output_name if status.installed else 'not installed'}")

    if sys.platform == "win32":
        from .audio import wasapi

        print(f"  per-app capture API    : "
              f"{'available' if wasapi.is_process_loopback_supported() else 'missing'}")
        endpoints = wasapi.list_render_endpoints()
        print(f"  loopback endpoints     : {len(endpoints)}")
        ok &= bool(endpoints)
    else:
        from .audio import pulse

        print(f"  pactl                  : "
              f"{'available' if pulse.available() else 'missing'}")

    from .discordlink import fallback, ipc

    running, in_call = fallback.quick_check(cfg)
    print(f"  discord process        : {'running' if running else 'not running'}")
    print(f"  discord rpc socket     : {'found' if ipc.discord_is_running() else 'none'}")
    print(f"  currently in a call    : {'yes' if in_call else 'no'}")

    from . import hotkeys

    print(f"  global hotkeys         : {hotkeys.describe_availability()}")
    print(f"\n  config file            : {config.config_path()}")
    print(f"\n{'PASS' if ok else 'PROBLEMS FOUND'}")
    return 0 if ok else 1


def _run_headless(app) -> int:
    stop = {"flag": False}

    def handler(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass

    app.start()
    print(f"{APP_NAME} running headless. Press Ctrl+C to stop.")
    print(f"  {app.status_line()}")
    for warning in app.engine.status.warnings:
        print(f"  ! {warning}")
    for error in app.engine.status.errors:
        print(f"  X {error}")

    last = ""
    while not stop["flag"]:
        time.sleep(1.0)
        line = app.status_line()
        if line != last:
            print(f"  {line}")
            last = line
    app.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log.setup(logging.DEBUG if args.verbose else logging.INFO)

    if args.list_devices:
        _print_devices()
        return 0
    if args.list_sources:
        _print_sources()
        return 0

    cfg = config.load()

    if args.list_sounds:
        for entry in cfg.soundboard.sounds:
            print(f"  {entry.id}  {entry.name:<28} {entry.hotkey or '-':<16} "
                  f"{entry.path}")
        if not cfg.soundboard.sounds:
            print("  (no sounds configured yet)")
        return 0
    if args.check:
        return _self_check(cfg)

    from .app import MicForgeApp

    app = MicForgeApp(cfg)

    if args.play:
        needle = args.play.strip().lower()
        target = None
        for entry in cfg.soundboard.sounds:
            if entry.id.lower() == needle or entry.name.lower() == needle:
                target = entry
                break
        if target is None:
            print(f"No sound matching {args.play!r}. Try --list-sounds.")
            return 1
        app.engine.start()
        if not app.soundboard.play(target):
            print(f"Could not play: {app.soundboard.last_error}")
            return 1
        while app.soundboard.active_count:
            time.sleep(0.05)
        time.sleep(0.2)
        app.shutdown()
        return 0

    if args.headless:
        return _run_headless(app)

    return _run_gui(app, cfg)


def _run_gui(app, cfg: config.Config) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:
        print(f"The window needs PySide6 ({exc}).\n"
              f"Install it with:  pip install PySide6-Essentials\n"
              f"Or run without a window:  python -m micforge --headless")
        return 1

    from .ui import theme
    from .ui.main_window import MainWindow

    qt_app = QApplication(sys.argv[:1])
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationDisplayName(APP_NAME)
    qt_app.setStyleSheet(theme.stylesheet(cfg.ui.accent))
    qt_app.setQuitOnLastWindowClosed(False)

    if cfg.ui.autostart_engine:
        app.start()
    else:
        app.rebuild_hotkeys()
        if cfg.soundboard.hotkeys_enabled:
            app.hotkeys.start()
        app.start_discord()

    window = MainWindow(app)
    if cfg.ui.start_minimised and cfg.ui.minimise_to_tray:
        window.hide()
    else:
        window.show()

    try:
        code = qt_app.exec()
    finally:
        app.shutdown()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
