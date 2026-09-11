"""Global hotkeys, including hold-to-talk.

pynput ships a ``GlobalHotKeys`` helper, but it only reports presses. Push-to-
talk needs the release too, so this tracks the pressed-key set itself and
matches combinations on both edges.

Hotkeys are written the obvious way -- ``ctrl+shift+f5``, ``alt+1``, ``f9`` --
and parsed leniently, so ``CTRL + Shift + F5`` works as well.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from . import log

_log = log.get("hotkeys")

try:
    from pynput import keyboard
except Exception as exc:  # pragma: no cover
    keyboard = None  # type: ignore
    _log.warning("pynput unavailable, global hotkeys are disabled: %s", exc)

MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "strg": "ctrl",
    "shift": "shift", "umschalt": "shift",
    "alt": "alt", "option": "alt", "alt_gr": "alt", "altgr": "alt",
    "cmd": "cmd", "win": "cmd", "super": "cmd", "meta": "cmd", "windows": "cmd",
}

KEY_ALIASES = {
    "esc": "escape", "return": "enter", "del": "delete", "ins": "insert",
    "pgup": "page_up", "pgdn": "page_down", "pagedown": "page_down",
    "pageup": "page_up", "spacebar": "space", " ": "space",
}


@dataclass(frozen=True)
class Combo:
    key: str
    modifiers: frozenset

    def __str__(self) -> str:
        order = ["ctrl", "shift", "alt", "cmd"]
        parts = [m for m in order if m in self.modifiers]
        parts.append(self.key)
        return "+".join(parts)


def parse(spec: str) -> Combo | None:
    """``"Ctrl + Shift + F5"`` -> :class:`Combo`, or ``None`` if unparseable."""
    if not spec:
        return None
    tokens = [t.strip().lower() for t in str(spec).replace("<", "").replace(">", "")
              .split("+") if t.strip()]
    if not tokens:
        return None
    mods, main = set(), ""
    for token in tokens:
        if token in MODIFIER_ALIASES:
            mods.add(MODIFIER_ALIASES[token])
        else:
            main = KEY_ALIASES.get(token, token)
    if not main:
        return None
    return Combo(key=main, modifiers=frozenset(mods))


def _normalise(key) -> tuple[str, str]:
    """pynput key -> ``(name, modifier_or_empty)``."""
    if keyboard is None:
        return "", ""
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower(), ""
        if key.vk is not None:
            # Numpad and other keys without a char still have a virtual code.
            return f"vk{key.vk}", ""
        return "", ""
    name = getattr(key, "name", "") or ""
    base = name.replace("_l", "").replace("_r", "").replace("_gr", "")
    if base in ("ctrl", "shift", "alt", "cmd"):
        return name, base
    return KEY_ALIASES.get(name, name), ""


class HotkeyManager:
    """Owns one global keyboard listener for the whole app."""

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self._listener = None
        self._bindings: dict[Combo, str] = {}
        self._held_actions: dict[str, Combo] = {}
        self._pressed: set[str] = set()
        self._active_mods: set[str] = set()
        self._lock = threading.Lock()
        self._capture_cb = None
        self.enabled = True
        self.last_error = ""

    @property
    def available(self) -> bool:
        return keyboard is not None

    # ------------------------------------------------------------- bindings
    def set_bindings(self, bindings: dict[str, str]) -> list[str]:
        """``{action_id: "ctrl+f5"}``. Returns the specs that would not parse."""
        bad: list[str] = []
        parsed: dict[Combo, str] = {}
        for action, spec in bindings.items():
            if not spec:
                continue
            combo = parse(spec)
            if combo is None:
                bad.append(spec)
                continue
            if combo in parsed:
                _log.warning("hotkey %s is bound twice; %s wins", combo, action)
            parsed[combo] = action
        with self._lock:
            self._bindings = parsed
        _log.info("%d hotkeys bound", len(parsed))
        return bad

    def bound_specs(self) -> dict[str, str]:
        with self._lock:
            return {action: str(combo) for combo, action in self._bindings.items()}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if not self.available:
            self.last_error = "pynput is not installed"
            return False
        if self._listener is not None:
            return True
        try:
            self._listener = keyboard.Listener(on_press=self._handle_press,
                                               on_release=self._handle_release)
            self._listener.daemon = True
            self._listener.start()
            _log.info("global hotkey listener running")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self._listener = None
            _log.error("cannot start the hotkey listener: %s", exc)
            return False

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        self._pressed.clear()
        self._active_mods.clear()

    @property
    def running(self) -> bool:
        return self._listener is not None

    # --------------------------------------------------------------- capture
    def capture_next(self, callback) -> None:
        """Record the next combination the user presses, for the UI."""
        self._capture_cb = callback

    def cancel_capture(self) -> None:
        self._capture_cb = None

    # ---------------------------------------------------------------- events
    def _handle_press(self, key) -> None:
        name, modifier = _normalise(key)
        if not name:
            return
        if modifier:
            self._active_mods.add(modifier)
            return
        if name in self._pressed:
            return  # key repeat
        self._pressed.add(name)

        combo = Combo(key=name, modifiers=frozenset(self._active_mods))

        capture = self._capture_cb
        if capture is not None:
            self._capture_cb = None
            try:
                capture(str(combo))
            except Exception:
                _log.exception("hotkey capture callback raised")
            return

        if not self.enabled:
            return
        with self._lock:
            action = self._bindings.get(combo)
            if action is None:
                # Allow a bare key to fire even while an unrelated modifier is
                # held, but never let a bare binding swallow a combo binding.
                bare = Combo(key=name, modifiers=frozenset())
                if bare not in self._bindings or any(
                        c.key == name and c.modifiers for c in self._bindings):
                    return
                action, combo = self._bindings[bare], bare
        self._held_actions[action] = combo
        cb = self.on_press
        if cb is not None:
            try:
                cb(action)
            except Exception:
                _log.exception("hotkey handler raised for %s", action)

    def _handle_release(self, key) -> None:
        name, modifier = _normalise(key)
        if modifier:
            self._active_mods.discard(modifier)
            return
        if not name:
            return
        self._pressed.discard(name)

        released = [action for action, combo in self._held_actions.items()
                    if combo.key == name]
        for action in released:
            self._held_actions.pop(action, None)
            cb = self.on_release
            if cb is not None:
                try:
                    cb(action)
                except Exception:
                    _log.exception("hotkey release handler raised for %s", action)


def describe_availability() -> str:
    if keyboard is None:
        return ("Global hotkeys need the pynput package "
                "(pip install pynput).")
    import sys

    if sys.platform.startswith("linux"):
        return ("Global hotkeys work on X11. Under Wayland the compositor blocks "
                "them, so bind the keys in your desktop settings to run "
                "'micforge --play <sound id>' instead.")
    return "Global hotkeys are active."
