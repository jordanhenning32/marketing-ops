"""Console I/O helpers for Windows scheduled-task entry points."""
from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """Prevent UnicodeEncodeError when scheduled tasks redirect stdout/stderr."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
