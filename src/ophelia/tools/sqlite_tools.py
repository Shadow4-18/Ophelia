"""SQLite access under ~/.ophelia — read, write, create tables and databases."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import aiosqlite

from ophelia.config import OPHELIA_HOME

_READ_PREFIX = re.compile(r"^\s*(SELECT|PRAGMA|WITH)\b", re.IGNORECASE | re.DOTALL)
# Longest one sqlite_exec may run. A self-join over her message history ran
# for 8-9 minutes and, without WAL, blocked every message save meanwhile
# ("database is locked" to the owner).
QUERY_LIMIT_S = 20.0
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


async def run_sqlite(database: str, sql: str, *, max_rows: int = 200,
                     limit_s: float = QUERY_LIMIT_S) -> str:
    """Run her SQL with a time limit; a stopped turn stops the query too
    (SQLite keeps running in its thread otherwise, holding its lock)."""
    stop = threading.Event()
    deadline = time.monotonic() + limit_s

    def abort() -> int:  # SQLite calls this every few thousand steps
        return 1 if stop.is_set() or time.monotonic() > deadline else 0

    # Shielded so a cancel reaches us first: flag the query to abort, let
    # SQLite unwind and the connection close, then pass the cancel on. (A
    # plain cancel queues the close behind the still-running query.)
    job = asyncio.ensure_future(_run_sqlite(database, sql, max_rows=max_rows, abort=abort))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        stop.set()
        try:
            await asyncio.wait_for(job, 10)
        except BaseException:
            pass
        raise
    except Exception as e:
        if "interrupted" in str(e).lower():
            return (f"sqlite_exec stopped: the query ran past {limit_s:.0f}s. Make it "
                    "cheaper: LIMIT, a narrower WHERE (recent rows by created_at), no "
                    "self-joins over the whole table, or count in steps.")
        raise


async def _run_sqlite(database: str, sql: str, *, max_rows: int, abort) -> str:
    if not sql.strip():
        return "Empty SQL."
    if _FORBIDDEN.search(sql):
        return "ATTACH/DETACH/LOAD_EXTENSION are not allowed."

    path = resolve_ophelia_db(database)
    is_read = bool(_READ_PREFIX.match(sql))

    async with aiosqlite.connect(path) as db:
        await db.set_progress_handler(abort, 10_000)
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
