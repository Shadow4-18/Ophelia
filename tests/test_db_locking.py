"""A long query of hers made every message save fail with "database is
locked" (no WAL; and stopping her turn didn't stop SQLite's thread)."""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from ophelia.memory.store import MemoryStore
from ophelia.tools import sqlite_tools

_SLOW = """WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n)
SELECT count(*) FROM n"""  # never finishes on its own


@pytest.mark.asyncio
async def test_memory_db_uses_wal_so_a_long_read_cant_lock_out_writes(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    await store.init()
    async with aiosqlite.connect(store.db_path) as db:
        mode = (await (await db.execute("PRAGMA journal_mode")).fetchone())[0]
    assert mode == "wal"
    # A reader holding an open read transaction...
    reader = await aiosqlite.connect(store.db_path)
    await reader.execute("BEGIN")
    await (await reader.execute("SELECT count(*) FROM messages")).fetchone()
    # ...doesn't stop a message being saved.
    await store.append_message("telegram:1", "user", "I went 152-2")
    await reader.close()


@pytest.mark.asyncio
async def test_her_queries_have_a_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_tools, "OPHELIA_HOME", tmp_path)
    t0 = time.monotonic()
    out = await sqlite_tools.run_sqlite("data/x.db", _SLOW, limit_s=0.5)
    assert time.monotonic() - t0 < 5
    assert "ran past 0s" in out or "ran past 1s" in out


@pytest.mark.asyncio
async def test_stopping_her_turn_stops_the_query(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_tools, "OPHELIA_HOME", tmp_path)
    task = asyncio.create_task(sqlite_tools.run_sqlite("data/x.db", _SLOW, limit_s=600))
    await asyncio.sleep(0.3)
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 5  # the SQLite thread let go, not 10 minutes later


@pytest.mark.asyncio
async def test_normal_queries_still_work(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_tools, "OPHELIA_HOME", tmp_path)
    await sqlite_tools.run_sqlite("data/x.db", "CREATE TABLE t (a); INSERT INTO t VALUES (1);")
    out = await sqlite_tools.run_sqlite("data/x.db", "SELECT a FROM t")
    assert '"a": 1' in out
