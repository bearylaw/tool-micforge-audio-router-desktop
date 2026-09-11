#!/usr/bin/env python3
"""Double-clickable launcher.

Equivalent to ``python -m micforge``; it exists so the app can be started from a
file manager or a desktop shortcut without knowing about ``-m``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from micforge.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
