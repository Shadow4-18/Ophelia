"""Runtime platform detection — PC vs Termux vs other."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def is_termux() -> bool:
    if os.environ.get("TERMUX_VERSION"):
        return True
    termux_prefix = Path("/data/data/com.termux/files/usr")
    return termux_prefix.is_dir()


def is_windows() -> bool:
    return sys.platform == "win32"


def runtime_label() -> str:
    if is_termux():
        return "termux"
    if is_windows():
        return "windows"
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    return system or "unknown"


def platform_summary() -> str:
    label = runtime_label()
    machine = platform.machine()
    py = platform.python_version()
    return f"{label} ({machine}, Python {py})"
