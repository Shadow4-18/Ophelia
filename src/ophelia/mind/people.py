"""People in the owner's life — short notes she keeps per person.

USER.md describes the owner; guests have rapport rows. Nobody tracked the
*third parties* that come up in conversation ("my sister Maya", "my boss").
She saves notes with the ``remember_person`` tool; when a known name shows up
in a message, that person's notes are injected into the prompt (a cheap
word match — no LLM call, works the same on Termux).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import aiosqlite

MAX_NOTES_CHARS = 600
MAX_PEOPLE_IN_PROMPT = 3


class PeopleBook:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def _db(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                notes TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        return db

    async def remember(self, name: str, note: str) -> str:
        name = " ".join((name or "").split())[:60]
        note = (note or "").strip()
        if not name or not note:
            return "Need both a name and a note."
        db = await self._db()
        try:
            cursor = await db.execute("SELECT notes FROM people WHERE name = ?", (name,))
            row = await cursor.fetchone()
            notes = note if not row else f"{row['notes']}; {note}"
            if len(notes) > MAX_NOTES_CHARS:
                # Keep the most recent notes when it grows too long.
                notes = "…" + notes[-(MAX_NOTES_CHARS - 1):]
            await db.execute(
                "INSERT INTO people (name, notes, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET notes = excluded.notes, "
                "updated_at = excluded.updated_at",
                (name, notes, time.time()),
            )
            await db.commit()
        finally:
            await db.close()
        return f"Noted about {name}: {note}"

    async def all(self) -> list[dict]:
        db = await self._db()
        try:
            cursor = await db.execute("SELECT name, notes, updated_at FROM people")
            return [dict(r) for r in await cursor.fetchall()]
        finally:
            await db.close()

    async def mentioned_in(self, text: str) -> list[dict]:
        if not text:
            return []
        low = text.lower()
        hits = []
        for p in await self.all():
            if re.search(r"\b" + re.escape(p["name"].lower()) + r"\b", low):
                hits.append(p)
        hits.sort(key=lambda p: p["updated_at"], reverse=True)
        return hits[:MAX_PEOPLE_IN_PROMPT]


def people_block(people: list[dict]) -> str:
    if not people:
        return ""
    lines = ["# People you know (mentioned just now)"]
    for p in people:
        lines.append(f"- {p['name']}: {p['notes']}")
    return "\n".join(lines)
