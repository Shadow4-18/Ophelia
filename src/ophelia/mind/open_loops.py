"""Open loops — things she said she'd come back to.

Neuro-style presence is mostly *remembering to return to things*: "I'll look
that up", "good luck at the interview tomorrow", "remind me to call mom".
An open loop is a small row with a due time. They are created by:

- the ``follow_up`` tool (she or the owner asks for it explicitly), and
- a cheap regex over her own owner-chat replies that catches promises
  ("I'll look into it", "let me get back to you") — no LLM call.

Due loops surface in the consciousness tick as a reason to act (like a due
goal). A loop closes when she acts/messages on a tick that surfaced it, when
she calls ``follow_up_done``, or it expires after a few ignored surfacings /
two days overdue, so nothing nags forever. Pure SQLite; phone-friendly.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import aiosqlite

DEFAULT_DUE_HOURS = 3.0
MAX_SURFACINGS = 3
EXPIRE_AFTER_S = 48 * 3600

_PROMISE_RE = re.compile(
    r"\b(?:i'?ll|i will|let me|i'?m gonna|i'?m going to)\s+"
    r"(?:look (?:that|this|it) up|look into (?:it|that|this)|check (?:on|back|in on|into)|"
    r"get back to you|find out|follow up|circle back|remind you|ask you (?:about|later)|"
    r"think about it and)\b[^.!?\n]*",
    re.IGNORECASE,
)


def find_promises(text: str) -> list[str]:
    """Sentences in her reply where she committed to coming back to something."""
    out: list[str] = []
    for m in _PROMISE_RE.finditer(text or ""):
        # Widen to the whole sentence for context.
        start = max(text.rfind(".", 0, m.start()), text.rfind("!", 0, m.start()),
                    text.rfind("?", 0, m.start()), text.rfind("\n", 0, m.start()))
        sentence = text[start + 1 : m.end()].strip()
        if sentence and sentence not in out:
            out.append(sentence[:240])
    return out


def promise_due_hours(sentence: str) -> float:
    """When a promise should come back to mind, from how it was said, with
    some spread so they don't all resurface exactly three hours later."""
    import random

    low = (sentence or "").lower()
    if "tomorrow" in low:
        return random.uniform(16, 24)
    if any(w in low for w in ("tonight", "this evening", "later today")):
        return random.uniform(3, 7)
    if any(w in low for w in ("in a bit", "in a minute", "shortly", "soon", "in a sec")):
        return random.uniform(0.3, 1.5)
    if any(w in low for w in ("this week", "some time", "sometime", "one day")):
        return random.uniform(24, 72)
    return random.uniform(1.5, 6)


class OpenLoops:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def _db(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS open_loops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'tool',
                due_at REAL NOT NULL,
                created_at REAL NOT NULL,
                surfaced INTEGER NOT NULL DEFAULT 0,
                closed_at REAL,
                outcome TEXT
            )
            """
        )
        return db

    async def add(
        self,
        channel: str,
        text: str,
        *,
        due_in_hours: float = DEFAULT_DUE_HOURS,
        source: str = "tool",
    ) -> int:
        text = (text or "").strip()[:400]
        if not text:
            return 0
        now = time.time()
        db = await self._db()
        try:
            cursor = await db.execute(
                "SELECT id FROM open_loops WHERE closed_at IS NULL AND channel = ? "
                "AND lower(text) = lower(?)",
                (channel, text),
            )
            existing = await cursor.fetchone()
            if existing:
                return int(existing["id"])
            cursor = await db.execute(
                "INSERT INTO open_loops (channel, text, source, due_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel, text, source, now + max(0.0, due_in_hours) * 3600, now),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)
        finally:
            await db.close()

    async def capture_promises(self, channel: str, reply: str) -> list[int]:
        ids = []
        for sentence in find_promises(reply):
            ids.append(await self.add(channel, sentence, source="promise",
                                      due_in_hours=promise_due_hours(sentence)))
        return [i for i in ids if i]

    async def open(self, channel: str | None = None, limit: int = 5) -> list[dict]:
        db = await self._db()
        try:
            if channel:
                cursor = await db.execute(
                    "SELECT * FROM open_loops WHERE closed_at IS NULL AND channel = ? "
                    "ORDER BY due_at LIMIT ?",
                    (channel, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM open_loops WHERE closed_at IS NULL "
                    "ORDER BY due_at LIMIT ?",
                    (limit,),
                )
            return [dict(r) for r in await cursor.fetchall()]
        finally:
            await db.close()

    async def due(self, limit: int = 3, now: float | None = None) -> list[dict]:
        now = now or time.time()
        await self.expire(now)
        db = await self._db()
        try:
            cursor = await db.execute(
                "SELECT * FROM open_loops WHERE closed_at IS NULL AND due_at <= ? "
                "ORDER BY due_at LIMIT ?",
                (now, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]
        finally:
            await db.close()

    async def mark_surfaced(self, ids: list[int]) -> None:
        if not ids:
            return
        db = await self._db()
        try:
            await db.executemany(
                "UPDATE open_loops SET surfaced = surfaced + 1 WHERE id = ?",
                [(i,) for i in ids],
            )
            await db.commit()
        finally:
            await db.close()

    async def close(self, loop_id: int, outcome: str = "done") -> bool:
        db = await self._db()
        try:
            cursor = await db.execute(
                "UPDATE open_loops SET closed_at = ?, outcome = ? "
                "WHERE id = ? AND closed_at IS NULL",
                (time.time(), (outcome or "done")[:200], int(loop_id)),
            )
            await db.commit()
            return (cursor.rowcount or 0) > 0
        finally:
            await db.close()

    async def expire(self, now: float | None = None) -> int:
        now = now or time.time()
        db = await self._db()
        try:
            cursor = await db.execute(
                "UPDATE open_loops SET closed_at = ?, outcome = 'expired' "
                "WHERE closed_at IS NULL AND (surfaced >= ? OR due_at < ?)",
                (now, MAX_SURFACINGS, now - EXPIRE_AFTER_S),
            )
            await db.commit()
            return cursor.rowcount or 0
        finally:
            await db.close()


def _ago(ts: float, now: float) -> str:
    delta = now - ts
    if delta < 0:
        return f"due in {(-delta) / 3600:.1f}h"
    if delta < 3600:
        return f"{int(delta / 60)}m overdue"
    return f"{delta / 3600:.1f}h overdue"


def loops_block(loops: list[dict], *, title: str, now: float | None = None) -> str:
    if not loops:
        return ""
    now = now or time.time()
    lines = [title]
    for lp in loops:
        lines.append(f"- [{lp['id']}] {lp['text']} ({_ago(lp['due_at'], now)})")
    return "\n".join(lines)
