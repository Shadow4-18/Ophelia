from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.commit()

    async def append_message(
        self,
        channel: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages (channel, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    channel,
                    role,
                    content,
                    json.dumps(metadata or {}),
                    time.time(),
                ),
            )
            await db.commit()

    async def recent_messages(self, channel: str, limit: int = 40) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT role, content, metadata, created_at
                FROM messages
                WHERE channel = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (channel, limit),
            )
            rows = await cursor.fetchall()
        out = []
        for row in reversed(rows):
            out.append(
                {
                    "role": row["role"],
                    "content": row["content"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "created_at": row["created_at"],
                }
            )
        return out

    async def set_fact(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, time.time()),
            )
            await db.commit()

    async def get_fact(self, key: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM facts WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return row[0] if row else None

    async def save_psyche(self, psyche: object) -> None:
        from ophelia.mind.psyche import PsycheState

        if isinstance(psyche, PsycheState):
            await self.set_fact("psyche:state", psyche.to_json())

    async def load_psyche(self) -> "PsycheState":
        from ophelia.mind.psyche import PsycheState

        raw = await self.get_fact("psyche:state")
        if raw:
            try:
                return PsycheState.from_json(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return PsycheState()

    async def save_drives(self, drives: object) -> None:
        from ophelia.mind.drives import DriveState

        if isinstance(drives, DriveState):
            await self.set_fact("drives:state", drives.to_json())

    async def load_drives(self) -> "DriveState":
        from ophelia.mind.drives import DriveState

        raw = await self.get_fact("drives:state")
        if raw:
            try:
                return DriveState.from_json(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return DriveState()

    async def recent_across_channels(
        self, channels: list[str], limit: int = 25
    ) -> list[dict]:
        if not channels:
            return []
        placeholders = ",".join("?" for _ in channels)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT channel, role, content, metadata, created_at
                FROM messages
                WHERE channel IN ({placeholders})
                ORDER BY id DESC
                LIMIT ?
                """,
                (*channels, limit),
            )
            rows = await cursor.fetchall()
        out = []
        for row in reversed(rows):
            out.append(
                {
                    "channel": row["channel"],
                    "role": row["role"],
                    "content": row["content"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                }
            )
        return out

    async def recent_global(self, limit: int = 40) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT channel, role, content, metadata, created_at
                FROM messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        out = []
        for row in reversed(rows):
            out.append(
                {
                    "channel": row["channel"],
                    "role": row["role"],
                    "content": row["content"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "created_at": row["created_at"],
                }
            )
        return out
