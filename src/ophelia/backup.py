"""Backups of who she is — so a dead or wiped phone doesn't take her with it.

Everything that makes her *her* lives in ~/.ophelia: SOUL, growth, prompter,
goals, memories, skills, persona version history, her site, and the SQLite
databases (memory, chat log, …). This module packs those into a dated
``ophelia-backup-*.tar.gz``:

- databases are snapshotted with SQLite's online backup API, so a backup
  taken while she's running is consistent;
- bulky, replaceable files are skipped (generated media, received photos,
  screenshots, avatar models, logs);
- secrets (.env, OAuth tokens) are excluded unless
  OPHELIA_BACKUP_INCLUDE_SECRETS=true.

Where backups go:
- locally, keeping the newest OPHELIA_BACKUP_KEEP. On Termux the default is
  ~/storage/shared/Ophelia/backups (phone storage, survives clearing or
  uninstalling Termux; run `termux-setup-storage` once), else
  ~/.ophelia/backups;
- optionally off the phone (OPHELIA_BACKUP_OFFSITE=owner): the archive is
  sent to the owner's Telegram/Discord DM every
  OPHELIA_BACKUP_OFFSITE_DAYS. That's what saves her if the phone is lost.

Stdlib only (tarfile, sqlite3) — nothing new to install on Termux.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

import structlog

from ophelia.config import OPHELIA_HOME

log = structlog.get_logger()

ARCHIVE_PREFIX = "ophelia-backup-"
MANIFEST = "ophelia-backup.json"
FORMAT_VERSION = 1

IDENTITY_FILES = (
    "SOUL.md",
    "GROWTH.md",
    "PROMPTER.md",
    "goals.yaml",
    "games.yaml",
    "mcp.json",
    "comfyui_workflow.json",
)
IDENTITY_DIRS = ("memories", "skills", "versions", "site", "voices", "evals", "data")
SECRET_FILES = (".env", "xai_oauth.json", "hermes_auth.json", "auth.json")

# Inside included dirs: bulky / replaceable / transient things to skip.
SKIP_DIRS = {
    "artifacts",
    "media",
    "screenshots",
    "telegram_media",
    "discord_media",
    "listen",
    "wake",
    "voice",
    "__pycache__",
    "backups",
    "dist",
}
SKIP_SUFFIXES = (".log", ".lock", "-wal", "-shm", "-journal", ".mp3", ".wav", ".ogg", ".mp4")
MAX_FILE_BYTES = 20 * 1024 * 1024  # non-database files
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
TELEGRAM_MAX_BYTES = 49 * 1024 * 1024
STATE_FILE = "backup_state.json"


def default_backup_dir() -> Path:
    from ophelia.platform import is_termux

    shared = Path.home() / "storage" / "shared"
    if is_termux() and shared.is_dir():
        return shared / "Ophelia" / "backups"
    return OPHELIA_HOME / "backups"


def backup_dir(settings: Any) -> Path:
    raw = (getattr(settings, "backup_dir", "") or "").strip()
    return Path(raw).expanduser() if raw else default_backup_dir()


def _iter_sources(home: Path, *, include_secrets: bool) -> list[Path]:
    out: list[Path] = []
    names = list(IDENTITY_FILES) + (list(SECRET_FILES) if include_secrets else [])
    for name in names:
        p = home / name
        if p.is_file():
            out.append(p)
    for d in IDENTITY_DIRS:
        root = home / d
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [n for n in dirnames if n not in SKIP_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if fn.endswith(SKIP_SUFFIXES) or p.is_symlink():
                    continue
                if not fn.endswith(DB_SUFFIXES):
                    try:
                        if p.stat().st_size > MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                out.append(p)
    return out


def _snapshot_db(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite database."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source:
        with sqlite3.connect(dst) as target:
            source.backup(target)


def create_backup(
    dest_dir: Path,
    *,
    home: Path = OPHELIA_HOME,
    include_secrets: bool = False,
    keep: int = 7,
) -> Path:
    """Write a dated archive into dest_dir and prune older ones. Sync."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest_dir / f"{ARCHIVE_PREFIX}{stamp}.tar.gz"
    files: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ophelia-backup-") as tmp:
        staging = Path(tmp)
        for src in _iter_sources(home, include_secrets=include_secrets):
            rel = src.relative_to(home)
            dst = staging / rel
            try:
                if src.name.endswith(DB_SUFFIXES):
                    _snapshot_db(src, dst)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            except (OSError, sqlite3.Error) as e:
                log.warning("backup.skip_file", path=str(rel), error=str(e))
                continue
            files.append({"path": rel.as_posix(), "bytes": dst.stat().st_size})
        if not files:
            raise RuntimeError(f"Nothing to back up under {home}")
        manifest = {
            "format": FORMAT_VERSION,
            "created_at": time.time(),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "includes_secrets": include_secrets,
            "files": files,
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        partial = out.with_suffix(".partial")
        with tarfile.open(partial, "w:gz") as tar:
            for entry in sorted(staging.iterdir()):
                tar.add(entry, arcname=entry.name)
        partial.replace(out)
    prune(dest_dir, keep=keep)
    log.info("backup.created", path=str(out), files=len(files), bytes=out.stat().st_size)
    return out


def list_backups(dest_dir: Path) -> list[Path]:
    if not dest_dir.is_dir():
        return []
    return sorted(dest_dir.glob(f"{ARCHIVE_PREFIX}*.tar.gz"), reverse=True)


def prune(dest_dir: Path, *, keep: int) -> list[Path]:
    removed = []
    for old in list_backups(dest_dir)[max(1, keep):]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    safe = []
    for m in tar.getmembers():
        parts = PurePosixPath(m.name).parts
        if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
            raise ValueError(f"Refusing unsafe archive entry: {m.name}")
        safe.append(m)
    return safe


def read_manifest(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tar:
        f = tar.extractfile(MANIFEST)
        if f is None:
            raise ValueError("Not an Ophelia backup (no manifest).")
        return json.loads(f.read().decode("utf-8"))


def restore_backup(
    archive: Path,
    *,
    home: Path = OPHELIA_HOME,
    safety_dir: Path | None = None,
) -> str:
    """Restore an archive over ~/.ophelia, after saving a safety backup of
    the current state. Ophelia must not be running."""
    archive = archive.expanduser()
    manifest = read_manifest(archive)
    safety = create_backup(
        safety_dir or (home / "backups" / "pre-restore"), home=home, include_secrets=True, keep=3
    ) if home.is_dir() else None
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in _safe_members(tar) if m.name != MANIFEST]
        for m in members:
            target = home / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                continue
            tmp = target.with_name(target.name + ".restoring")
            with open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            for suffix in ("-wal", "-shm"):
                stale = target.with_name(target.name + suffix)
                if stale.exists():
                    stale.unlink()
            tmp.replace(target)
    note = f" Your previous state was saved to {safety}." if safety else ""
    return (
        f"Restored {len(manifest.get('files', []))} file(s) from backup made "
        f"{manifest.get('created', '?')}.{note}"
    )


def _load_state(home: Path) -> dict[str, float]:
    try:
        return json.loads((home / "data" / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(home: Path, state: dict[str, float]) -> None:
    path = home / "data" / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


OffsiteSender = Callable[[Path, str], Awaitable[bool]]


class BackupLoop:
    """Daily local backup; periodic off-phone copy to the owner's DMs."""

    CHECK_EVERY_S = 3600

    def __init__(
        self,
        settings: Any,
        *,
        home: Path = OPHELIA_HOME,
        offsite_sender: OffsiteSender | None = None,
    ) -> None:
        self.settings = settings
        self.home = home
        self.offsite_sender = offsite_sender
        self._stop = False

    async def run_once(self, now: float | None = None) -> Path | None:
        now = now or time.time()
        s = self.settings
        state = _load_state(self.home)
        interval = max(1.0, float(getattr(s, "backup_interval_hours", 24))) * 3600
        if now - state.get("last_local", 0) < interval:
            return None
        path = await asyncio.to_thread(
            create_backup,
            backup_dir(s),
            home=self.home,
            include_secrets=bool(getattr(s, "backup_include_secrets", False)),
            keep=int(getattr(s, "backup_keep", 7)),
        )
        state["last_local"] = now
        offsite = (getattr(s, "backup_offsite", "none") or "none").lower()
        offsite_every = max(1.0, float(getattr(s, "backup_offsite_days", 7))) * 86400
        if (
            offsite == "owner"
            and self.offsite_sender is not None
            and now - state.get("last_offsite", 0) >= offsite_every
        ):
            if path.stat().st_size > TELEGRAM_MAX_BYTES:
                log.warning(
                    "backup.offsite_too_large",
                    bytes=path.stat().st_size,
                    hint="Too big to DM; copy it off the phone manually.",
                )
            else:
                caption = f"Ophelia backup {time.strftime('%Y-%m-%d')} — keep this safe."
                try:
                    if await self.offsite_sender(path, caption):
                        state["last_offsite"] = now
                except Exception as e:
                    log.warning("backup.offsite_failed", error=str(e))
        _save_state(self.home, state)
        return path

    async def run(self) -> None:
        await asyncio.sleep(300)  # don't compete with startup
        while not self._stop:
            try:
                await self.run_once()
            except Exception as e:
                log.warning("backup.failed", error=str(e))
            await asyncio.sleep(self.CHECK_EVERY_S)

    def stop(self) -> None:
        self._stop = True
