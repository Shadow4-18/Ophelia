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
            # Self-authored lessons (semantic memory).
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson TEXT NOT NULL,
                    context TEXT,
                    tags TEXT,
                    created_at REAL NOT NULL,
                    recalled_at REAL
                )
                """
            )
            # Full-text search over all messages for recall_memory.
            try:
                await db.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, channel, role, content='messages', content_rowid='id')
                    """
                )
                await db.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid, content, channel, role)
                        VALUES (new.id, new.content, new.channel, new.role);
                    END
                    """
                )
            except aiosqlite.OperationalError:
                pass
            await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel)")
            # Quarantined store for GUEST conversations. Deliberately separate from
            # `messages` so curator / dream / reflect (which read `messages` and
            # `recent_global`) never ingest guest content into her identity. Guests
            # get continuity within their own thread, but it never touches her.
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guest_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_guest_channel ON guest_messages(channel)"
            )
            await db.commit()

    async def search_messages(self, query: str, limit: int = 8) -> list[dict]:
        """FTS5 semantic search across all channels for recall_memory."""
        if not query.strip():
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            try:
                cursor = await db.execute(
                    """
                    SELECT m.channel, m.role, m.content, m.created_at,
                           bm25(messages_fts) AS rank
                    FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
                    WHERE messages_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (query, limit),
                )
                rows = await cursor.fetchall()
            except aiosqlite.OperationalError:
                cursor = await db.execute(
                    """
                    SELECT channel, role, content, created_at
                    FROM messages
                    WHERE content LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (f"%{query}%", limit),
                )
                rows = await cursor.fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "channel": row["channel"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                }
            )
        return out

    async def add_lesson(
        self, lesson: str, context: str = "", tags: list[str] | None = None
    ) -> int:
        import json as _json

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO lessons (lesson, context, tags, created_at) VALUES (?, ?, ?, ?)",
                (lesson, context, _json.dumps(tags or []), time.time()),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def search_lessons(self, query: str, limit: int = 5) -> list[dict]:
        if not query.strip():
            return []
        import json as _json

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT lesson, context, tags, created_at
                FROM lessons
                WHERE lesson LIKE ? OR context LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", limit),
            )
            rows = await cursor.fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "lesson": row["lesson"],
                    "context": row["context"],
                    "tags": _json.loads(row["tags"] or "[]"),
                    "created_at": row["created_at"],
                }
            )
        return out

    async def recent_lessons(self, limit: int = 10) -> list[dict]:
        import json as _json

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT lesson, context, tags, created_at FROM lessons ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "lesson": row["lesson"],
                    "context": row["context"],
                    "tags": _json.loads(row["tags"] or "[]"),
                    "created_at": row["created_at"],
                }
            )
        return out

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

    async def recent_inner_thoughts(self, limit: int = 3) -> list[str]:
        """Recent [inner] ... lines from the consciousness channel for prompt injection."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT content FROM messages
                WHERE channel = 'consciousness' AND role = 'assistant'
                  AND content LIKE '[inner]%'
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [row[0].replace("[inner] ", "").strip()[:200] for row in reversed(rows)]

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

    # --- Guest (sandboxed) conversation store -------------------------------
    # Completely separate from `messages` so background identity loops can't
    # reach guest content. She can hold a conversation with a guest (with
    # per-guest continuity) but it never becomes part of her.

    async def append_guest_message(
        self, channel: str, role: str, content: str
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO guest_messages (channel, role, content, created_at) VALUES (?, ?, ?, ?)",
                (channel, role, content, time.time()),
            )
            await db.commit()

    async def recent_guest(self, channel: str, limit: int = 35) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT role, content, created_at
                FROM guest_messages
                WHERE channel = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (channel, limit),
            )
            rows = await cursor.fetchall()
        return [
            {"role": row["role"], "content": row["content"], "channel": channel}
            for row in reversed(rows)
        ]
