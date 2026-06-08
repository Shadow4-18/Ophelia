"""Build Android body for Termux (Shizuku) or PC (ADB)."""

from __future__ import annotations

from pathlib import Path

from ophelia.android.shizuku import AndroidBody
from ophelia.config import Settings


def build_android_body(settings: Settings) -> AndroidBody | None:
    if not settings.android_enabled:
        return None
    return AndroidBody(
        phone_control=Path(str(settings.phone_control_path)).expanduser(),
        adb_device=settings.adb_device,
        adb_root=settings.adb_root,
    )
