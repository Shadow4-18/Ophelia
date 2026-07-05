"""Kokoro TTS server lifecycle — reachability checks and autostart for `koko`."""

from __future__ import annotations

import os
import shutil
import subprocess
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

_KOKORO_IN_PROOT = "/root/Kokoros/target/release/koko"
_KOKORO_CWD_PROOT = "/root/Kokoros"


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


def _proot_distro_names() -> list[str]:
    if not shutil.which("proot-distro"):
        return []
    try:
        r = subprocess.run(
            ["proot-distro", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return ["ubuntu"]
    names: list[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("supported"):
            continue
        token = line.split()[0]
        if token and token not in names:
            names.append(token)
    return names or ["ubuntu"]


def _proot_rootfs_bases() -> list[Path]:
    bases: list[Path] = []
    prefix = _termux_prefix()
    parent = prefix / "var/lib/proot-distro/installed-rootfs"
    if parent.is_dir():
        for child in sorted(parent.iterdir()):
            if child.is_dir():
                bases.append(child)
    legacy = parent / "ubuntu"
    if legacy not in bases:
        bases.append(legacy)
    home_link = Path.home() / "ubuntu"
    if home_link.is_dir() and home_link not in bases:
        bases.append(home_link)
    return bases


def _koko_exists_inside_proot(distro: str) -> bool:
    try:
        r = subprocess.run(
            [
                "proot-distro",
                "login",
                distro,
                "--",
                "test",
                "-f",
                _KOKORO_IN_PROOT,
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def _candidate_koko_bins() -> list[tuple[Path, Path | None, str, str | None]]:
    """(binary, cwd, mode, proot_distro) — mode is direct, proot, or shell."""
    home = Path.home()
    candidates: list[tuple[Path, Path | None, str, str | None]] = []

    direct_paths = [
        home / "Kokoros/target/release/koko",
        Path("/root/Kokoros/target/release/koko"),
    ]
    for path in direct_paths:
        if path.is_file():
            candidates.append((path, path.parent.parent, "direct", None))

    for base in _proot_rootfs_bases():
        proot_koko = base / "root/Kokoros/target/release/koko"
        if proot_koko.is_file():
            candidates.append((proot_koko, proot_koko.parent.parent, "proot", "ubuntu"))

    if is_termux():
        for distro in _proot_distro_names():
            if _koko_exists_inside_proot(distro):
                candidates.append(
                    (Path(_KOKORO_IN_PROOT), Path(_KOKORO_CWD_PROOT), "proot", distro)
                )
                break

    which = shutil.which("koko")
    if which:
        path = Path(which)
        cwd = path.parent.parent if path.parent.name == "release" else None
        candidates.append((path, cwd, "direct", None))

    return candidates


def _proot_autostart_argv(distro: str, port: int) -> list[str]:
    inner = (
        f"cd {_KOKORO_CWD_PROOT} && exec ./target/release/koko openai --port {port}"
    )
    return ["proot-distro", "login", distro, "--", "bash", "-lc", inner]


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

    for koko, cwd, mode, distro in _candidate_koko_bins():
        if mode == "proot":
            use_distro = distro or "ubuntu"
            if not shutil.which("proot-distro"):
                continue
            return (_proot_autostart_argv(use_distro, port), None, "proot")
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
        "Set KOKORO_AUTOSTART_CMD in ~/.ophelia/.env, e.g. "
        f"proot-distro login ubuntu -- bash -lc "
        f"'cd {_KOKORO_CWD_PROOT} && ./target/release/koko openai --port {port}'"
    )
