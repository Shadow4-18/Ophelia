"""Keep ~/.ophelia/data from eating the phone.

Two folders only ever grew:
- data/screenshots — every screen capture vision takes (``screen_*.png``),
  kept forever. They're her scratch eyesight, not anyone's content, so
  captures older than OPHELIA_SCREENSHOT_KEEP_HOURS (default 24) are
  removed automatically. Calibration shots and anything else are kept.
- data/logs/media — the chat log copied every file sent or received. New
  entries are hard links (no extra space); old copies are only pruned when
  you choose a retention: OPHELIA_CHAT_LOG_MEDIA_DAYS (default 0 = keep
  forever) or ``ophelia cleanup --chat-media-days N``. Text log rows stay.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

SCREENSHOT_PATTERN = "screen_*"


@dataclass
class PruneResult:
    files: int = 0
    bytes: int = 0
    by_dir: dict[str, tuple[int, int]] = field(default_factory=dict)

    def add(self, label: str, files: int, size: int) -> None:
        self.files += files
        self.bytes += size
        f, b = self.by_dir.get(label, (0, 0))
        self.by_dir[label] = (f + files, b + size)


def _prune(
    root: Path, pattern: str, older_than_s: float, *, dry_run: bool, now: float
) -> tuple[int, int]:
    if not root.is_dir() or older_than_s <= 0:
        return 0, 0
    cutoff = now - older_than_s
    files = size = 0
    for p in root.glob(pattern):
        try:
            st = p.stat()
            if not p.is_file() or p.is_symlink() or st.st_mtime >= cutoff:
                continue
            if not dry_run:
                p.unlink()
            files += 1
            size += st.st_size
        except OSError:
            continue
    return files, size


def housekeep(
    data_dir: Path,
    *,
    screenshot_hours: float,
    chat_media_days: float,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneResult:
    now = now or time.time()
    result = PruneResult()
    f, b = _prune(
        data_dir / "screenshots", SCREENSHOT_PATTERN, screenshot_hours * 3600,
        dry_run=dry_run, now=now,
    )
    result.add("screenshots", f, b)
    f, b = _prune(
        data_dir / "logs" / "media", "*", chat_media_days * 86400,
        dry_run=dry_run, now=now,
    )
    result.add("chat-log media", f, b)
    return result


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


class HousekeepingLoop:
    """Runs housekeep() with the configured retention once a day."""

    INTERVAL_S = 24 * 3600

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._stop = False

    def run_once(self) -> PruneResult:
        s = self.settings
        result = housekeep(
            Path(s.data_dir),
            screenshot_hours=float(getattr(s, "screenshot_keep_hours", 24) or 0),
            chat_media_days=float(getattr(s, "chat_log_media_days", 0) or 0),
        )
        if result.files:
            log.info(
                "housekeeping.pruned", files=result.files, freed=human_bytes(result.bytes)
            )
        return result

    async def run(self) -> None:
        await asyncio.sleep(600)  # let startup and the first backup finish
        while not self._stop:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as e:
                log.warning("housekeeping.failed", error=str(e))
            await asyncio.sleep(self.INTERVAL_S)

    def stop(self) -> None:
        self._stop = True
