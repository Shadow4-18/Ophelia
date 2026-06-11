"""SQLite access under ~/.ophelia — read, write, create tables and databases."""

from __future__ import annotations

import json
import re
from pathlib import Path

import aiosqlite

from ophelia.config import OPHELIA_HOME

_READ_PREFIX = re.compile(r"^\s*(SELECT|PRAGMA|WITH)\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN = re.compile(
    r"\b(ATTACH|DETACH|LOAD_EXTENSION)\b",
    re.IGNORECASE,
)


def resolve_ophelia_db(name: str) -> Path:
    """Resolve a db name under ~/.ophelia (creates parent dirs)."""
    raw = (name or "data/memory.db").strip().replace("\\", "/")
    if raw.startswith("/") or ".." in raw.split("/"):
        raise ValueError("database must be a relative path under ~/.ophelia")
    path = (OPHELIA_HOME / raw).resolve()
    root = OPHELIA_HOME.resolve()
    if not str(path).startswith(str(root)):
        raise ValueError("database path must stay inside ~/.ophelia")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def list_ophelia_databases() -> list[str]:
    root = OPHELIA_HOME.resolve()
    found: list[str] = []
    if not root.is_dir():
        return found
    for p in sorted(root.rglob("*.db")):
        try:
            found.append(str(p.relative_to(root)).replace("\\", "/"))
        except ValueError:
            continue
    for p in sorted(root.rglob("*.sqlite")):
        try:
            found.append(str(p.relative_to(root)).replace("\\", "/"))
        except ValueError:
            continue
    return found


async def run_sqlite(database: str, sql: str, *, max_rows: int = 200) -> str:
    if not sql.strip():
        return "Empty SQL."
    if _FORBIDDEN.search(sql):
        return "ATTACH/DETACH/LOAD_EXTENSION are not allowed."

    path = resolve_ophelia_db(database)
    is_read = bool(_READ_PREFIX.match(sql))

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        if is_read:
            cursor = await db.execute(sql)
            rows = await cursor.fetchmany(max_rows + 1)
            cols = [d[0] for d in (cursor.description or [])]
            payload = [dict(zip(cols, row)) for row in rows[:max_rows]]
            truncated = len(rows) > max_rows
            return json.dumps(
                {
                    "database": str(path),
                    "rows": payload,
                    "truncated": truncated,
                    "count": len(payload),
                },
                indent=2,
                default=str,
            )[:12000]

        await db.executescript(sql)
        await db.commit()
        return json.dumps(
            {
                "database": str(path),
                "ok": True,
                "message": "SQL executed and committed.",
            },
            indent=2,
        )
