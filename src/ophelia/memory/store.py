from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite


async def _safe_add_column(db: aiosqlite.Connection, table: str, column: str, decl: str) -> None:
    """Add a column if it doesn't already exist. Idempotent migration helper."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    for row in rows:
        # row schema: (cid, name, type, notnull, dflt_value, pk)
        if len(row) >= 2 and row[1] == column:
            return
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS humor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outbound TEXT NOT NULL,
                    user_reply TEXT,
                    score REAL NOT NULL DEFAULT 0,
                    latency_s REAL,
                    created_at REAL NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'outreach',
                    tags TEXT
                )
                """
            )
            # Tier B #8: extend existing humor_events with kind/tags columns
            # for tracking jokes in normal chat, sticker/reaction signals, and
            # bit callbacks — not just outreach reactions. Idempotent.
            await _safe_add_column(db, "humor_events", "kind", "TEXT NOT NULL DEFAULT 'outreach'")
            await _safe_add_column(db, "humor_events", "tags", "TEXT")
            # Tier B #6: learned owner schedule from observed Telegram activity.
            # (dow, hour) -> count, last_seen. Lets LifeContext sharpen "is he
            # home / awake / at work" beyond the static .env schedule.
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS owner_activity (
                    dow INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    last_seen REAL,
                    PRIMARY KEY (dow, hour)
                )
                """
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

    async def get_fact_with_ts(self, key: str) -> tuple[str | None, float | None]:
        """Return (value, updated_at_epoch) for a fact, or (None, None) if absent."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value, updated_at FROM facts WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
        if not row:
            return None, None
        try:
            ts = float(row[1]) if row[1] is not None else None
        except (TypeError, ValueError):
            ts = None
        return row[0], ts

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

    async def recent_guest_activity(
        self, channels: list[str], per_channel: int = 4
    ) -> dict[str, list[dict]]:
        """Recent messages from each guest channel, for the owner's activity digest.

        Returns {channel: [{role, content, created_at}, ...]} for each channel
        that has any messages. This is the bridge that lets the owner's Ophelia
        know what she's been talking about with each guest — without exposing
        guest content to guests themselves.
        """
        if not channels:
            return {}
        out: dict[str, list[dict]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            for channel in channels:
                cursor = await db.execute(
                    """
                    SELECT role, content, created_at
                    FROM guest_messages
                    WHERE channel = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (channel, per_channel),
                )
                rows = await cursor.fetchall()
                if rows:
                    out[channel] = [
                        {
                            "role": row["role"],
                            "content": row["content"],
                            "created_at": row["created_at"],
                        }
                        for row in reversed(rows)
                    ]
        return out

    async def search_guest_messages(
        self,
        query: str,
        *,
        channel: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search quarantined guest chat history (owner-facing recall).

        Guest content lives in guest_messages, not the main messages FTS index.
        """
        if not query.strip() and not channel:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if channel and query.strip():
                cursor = await db.execute(
                    """
                    SELECT channel, role, content, created_at
                    FROM guest_messages
                    WHERE channel = ? AND content LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (channel, f"%{query.strip()}%", limit),
                )
            elif channel:
                cursor = await db.execute(
                    """
                    SELECT channel, role, content, created_at
                    FROM guest_messages
                    WHERE channel = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (channel, limit),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT channel, role, content, created_at
                    FROM guest_messages
                    WHERE content LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (f"%{query.strip()}%", limit),
                )
            rows = await cursor.fetchall()
        return [
            {
                "channel": row["channel"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    async def record_humor_outbound(self, text: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO humor_events (outbound, score, created_at) VALUES (?, 0, ?)",
                (text, time.time()),
            )
            await db.commit()

    async def record_humor_reaction(
        self,
        outbound: str,
        *,
        user_reply: str,
        score: float,
        latency_s: float,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE humor_events
                SET user_reply = ?, score = ?, latency_s = ?
                WHERE id = (
                    SELECT id FROM humor_events
                    WHERE outbound = ? AND user_reply IS NULL
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (user_reply, score, latency_s, outbound),
            )
            await db.commit()

    async def humor_hints(self, limit: int = 4) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT outbound, user_reply, score
                FROM humor_events
                WHERE user_reply IS NOT NULL
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "outbound": row["outbound"],
                "user_reply": row["user_reply"],
                "score": row["score"],
            }
            for row in rows
        ]

    async def record_humor_event(
        self,
        *,
        outbound: str,
        user_reply: str | None = None,
        score: float = 0.0,
        latency_s: float | None = None,
        kind: str = "outreach",
        tags: list[str] | None = None,
    ) -> None:
        """Tier B #8: general humor event with a kind tag.

        Kinds:
          - outreach    — spontaneous outreach she sent (existing behavior)
          - chat-joke   — a joke/quip she made in normal chat reply
          - sticker     — owner reacted with a sticker/emoji to something she said
          - callback    — a 'bit' from a prior conversation was referenced again
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO humor_events
                    (outbound, user_reply, score, latency_s, created_at, kind, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outbound[:500],
                    (user_reply or "")[:400] or None,
                    float(score),
                    latency_s,
                    time.time(),
                    kind,
                    json.dumps(tags or []),
                ),
            )
            await db.commit()

    async def humor_top_bits(self, *, kind: str | None = None, limit: int = 6) -> list[dict]:
        """Bits that landed repeatedly — grouped by outbound text, average score.

        Used to auto-feed save_lesson when a pattern reliably lands, and to
        surface 'callbacks' in the prompt.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if kind:
                cursor = await db.execute(
                    """
                    SELECT outbound, AVG(score) AS avg_score, COUNT(*) AS n
                    FROM humor_events
                    WHERE kind = ? AND score IS NOT NULL
                    GROUP BY outbound
                    HAVING n >= 2
                    ORDER BY avg_score DESC
                    LIMIT ?
                    """,
                    (kind, limit),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT outbound, AVG(score) AS avg_score, COUNT(*) AS n
                    FROM humor_events
                    WHERE score IS NOT NULL
                    GROUP BY outbound
                    HAVING n >= 2
                    ORDER BY avg_score DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = await cursor.fetchall()
        return [
            {
                "outbound": row["outbound"],
                "avg_score": float(row["avg_score"] or 0.0),
                "count": int(row["n"]),
            }
            for row in rows
        ]
