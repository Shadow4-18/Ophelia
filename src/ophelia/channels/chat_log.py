"""Universal chat log — every message sent to/from Ophelia, plus all media.

This is the owner's oversight layer: a complete record of who said what to her
and what she said back, including every photo sent to her and every image/video/
audio she sent back. Logging is universal (owner + guests) and lives in
~/.ophelia/data/logs/ — a SQLite index (`chat_log.db`) plus an organized media
folder (`media/`) with stable filenames so the owner can browse everything.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import aiosqlite
import structlog

log = structlog.get_logger()


def _safe_name(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s) or "anon"


class ChatLogger:
    def __init__(self, *, db_path: Path, media_dir: Path) -> None:
        self.db_path = db_path
        self.media_dir = media_dir
        self._ready = False

    @classmethod
    def from_settings(cls, settings) -> "ChatLogger":
        base = settings.data_dir / "logs"
        return cls(db_path=base / "chat_log.db", media_dir=base / "media")

    async def _init(self) -> None:
        if self._ready:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    channel TEXT NOT NULL,
                    sender_id TEXT,
                    direction TEXT NOT NULL,      -- 'in' (to her) or 'out' (from her)
                    role TEXT,                    -- 'user' / 'assistant' / 'media'
                    text TEXT,
                    media_path TEXT,
                    media_kind TEXT,
                    is_owner INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chatlog_ts ON chat_log(ts)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chatlog_channel ON chat_log(channel)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chatlog_direction ON chat_log(direction)"
            )
            await db.commit()
        self._ready = True

    def _media_dest(self, direction: str, channel: str, src: Path) -> Path:
        ext = src.suffix or ".bin"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        uniq = f"_{time.time_ns() % 100000:05d}"
        name = f"{direction}_{stamp}{uniq}_{_safe_name(channel)}{ext}"
        return self.media_dir / name

    async def log(
        self,
        *,
        channel: str,
        direction: str,
        text: str = "",
        media_path: Path | str | None = None,
        media_kind: str | None = None,
        sender_id: str | None = None,
        is_owner: bool = True,
        role: str | None = None,
    ) -> None:
        try:
            await self._init()
            media_store: str | None = None
            if media_path:
                src = Path(media_path)
                if src.is_file():
                    dst = self._media_dest(direction, channel, src)
                    try:
                        shutil.copy2(src, dst)
                        media_store = str(dst)
                    except Exception as e:
                        log.warning("chatlog.media_copy_failed", src=str(src), error=str(e))
                        media_store = str(src)
                else:
                    media_store = str(src)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO chat_log
                    (ts, channel, sender_id, direction, role, text, media_path, media_kind, is_owner)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        channel,
                        sender_id,
                        direction,
                        role,
                        text or None,
                        media_store,
                        media_kind,
                        1 if is_owner else 0,
                    ),
                )
                await db.commit()
        except Exception as e:
            log.warning("chatlog.write_failed", error=str(e), channel=channel)

    async def query(
        self,
        *,
        channel: str | None = None,
        direction: str | None = None,
        media_only: bool = False,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        await self._init()
        where = []
        params: list = []
        if channel:
            where.append("channel = ?")
            params.append(channel)
        if direction:
            where.append("direction = ?")
            params.append(direction)
        if media_only:
            where.append("media_path IS NOT NULL")
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts <= ?")
            params.append(until)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT id, ts, channel, sender_id, direction, role, text,
                       media_path, media_kind, is_owner
                FROM chat_log {clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
