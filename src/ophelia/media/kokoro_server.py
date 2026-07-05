"""Kokoro TTS server lifecycle — reachability checks and autostart for `koko`."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import structlog

from ophelia.media.voice import kokoro_base_url, resolve_tts_provider
from ophelia.platform import is_termux

if TYPE_CHECKING:
    from ophelia.config import Settings

log = structlog.get_logger()


def kokoro_listen_port(settings: Settings) -> int:
    if settings.kokoro_listen_port is not None:
        return int(settings.kokoro_listen_port)
    parsed = urlparse(kokoro_base_url(settings))
    return parsed.port or 8880


def kokoro_wanted(settings: Settings) -> bool:
    provider = resolve_tts_provider(settings)
    if provider == "kokoro":
        return bool(settings.kokoro_tts_url)
    return False


async def kokoro_reachable(settings: Settings) -> bool:
    url = f"{kokoro_base_url(settings)}/audio/voices"
    try:
        async with httpx.AsyncClient(timeout=4.0) as http:
            r = await http.get(url)
            return r.status_code < 500
    except Exception:
        return False


def _termux_prefix() -> Path:
    return Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))


def _candidate_koko_bins() -> list[tuple[Path, Path | None, str]]:
    """(binary, cwd, mode) — mode is 'direct' or 'proot'."""
    home = Path.home()
    candidates: list[tuple[Path, Path | None, str]] = []

    direct_paths = [
        home / "Kokoros/target/release/koko",
        Path("/root/Kokoros/target/release/koko"),
    ]
    for path in direct_paths:
        if path.is_file():
            candidates.append((path, path.parent.parent, "direct"))

    if is_termux():
        rootfs = _termux_prefix() / "var/lib/proot-distro/installed-rootfs/ubuntu"
        proot_koko = rootfs / "root/Kokoros/target/release/koko"
        if proot_koko.is_file():
            candidates.append((proot_koko, proot_koko.parent.parent, "proot"))

    which = shutil.which("koko")
    if which:
        path = Path(which)
        cwd = path.parent.parent if path.parent.name == "release" else None
        candidates.append((path, cwd, "direct"))

    return candidates


def resolve_kokoro_autostart(
    settings: Settings,
) -> tuple[list[str], str | None, str] | None:
    """Return (argv, cwd, mode) to spawn Kokoro, or None if unknown."""
    port = kokoro_listen_port(settings)

    if settings.kokoro_autostart_cmd:
        cmd = settings.kokoro_autostart_cmd.strip()
        if not cmd:
            return None
        return (["/bin/sh", "-c", cmd], None, "shell")

    bin_override = (settings.kokoro_koko_bin or "").strip()
    cwd_override = (settings.kokoro_koko_cwd or "").strip() or None

    if bin_override:
        koko = Path(bin_override).expanduser()
        if not koko.is_file():
            log.warning("kokoro.autostart_bin_missing", path=str(koko))
            return None
        cwd = Path(cwd_override).expanduser() if cwd_override else koko.parent.parent
        return (
            [str(koko), "openai", "--port", str(port)],
            str(cwd) if cwd.is_dir() else None,
            "direct",
        )

    for koko, cwd, mode in _candidate_koko_bins():
        if mode == "proot":
            if not shutil.which("proot-distro"):
                continue
            inner = f"cd /root/Kokoros && exec ./target/release/koko openai --port {port}"
            return (
                ["proot-distro", "login", "ubuntu", "--", "bash", "-lc", inner],
                None,
                "proot",
            )
        argv = [str(koko), "openai", "--port", str(port)]
        return (
            argv,
            str(cwd) if cwd and cwd.is_dir() else None,
            "direct",
        )

    return None


def describe_kokoro_autostart_hint(settings: Settings) -> str:
    port = kokoro_listen_port(settings)
    return (
        "Set KOKORO_KOKO_BIN to your koko binary, or KOKORO_AUTOSTART_CMD for a "
        f"custom launcher. Example (proot): "
        f"proot-distro login ubuntu -- bash -lc "
        f"'cd /root/Kokoros && ./target/release/koko openai --port {port}'"
    )
