"""Her own database clean-up dropped messages_fts but left the trigger that
feeds it, so every message save failed with "no such table:
main.messages_fts" until she was fixed by hand."""

from __future__ import annotations

import aiosqlite
import pytest

from ophelia.memory.store import MemoryStore
from ophelia.tools import sqlite_tools


async def _break(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DROP TABLE messages_fts")  # what her clean-up did
        await db.commit()


@pytest.mark.asyncio
async def test_saving_a_message_repairs_a_missing_search_index(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    await store.init()
    await store.append_message("telegram:1", "user", "the old pomegranate story")
    await _break(store.db_path)
    # Without a restart: the save repairs the index and goes through.
    await store.append_message("telegram:1", "user", "Im feeling stuck")
    async with aiosqlite.connect(store.db_path) as db:
        hits = await (await db.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'pomegranate'")).fetchone()
        n = await (await db.execute("SELECT count(*) FROM messages")).fetchone()
    assert n[0] == 2 and hits[0] == 1  # refilled from the old messages too


@pytest.mark.asyncio
async def test_startup_repairs_it_too(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    await store.init()
    await store.append_message("telegram:1", "user", "remember the lighthouse")
    await _break(store.db_path)
    await MemoryStore(store.db_path).init()
    async with aiosqlite.connect(store.db_path) as db:
        hits = await (await db.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'lighthouse'")).fetchone()
    assert hits[0] == 1


@pytest.mark.asyncio
async def test_her_sql_cant_take_her_memory_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_tools, "OPHELIA_HOME", tmp_path)
    store = MemoryStore(tmp_path / "data" / "memory.db")
    await store.init()
    out = await sqlite_tools.run_sqlite("data/memory.db", "DROP TABLE messages_fts")
    assert out.startswith("Refused")
    out = await sqlite_tools.run_sqlite("data/logs/chat_log.db", "alter table chat_log add x")
    assert out.startswith("Refused")
    # Reading and her own scratch databases are fine.
    assert '"count"' in await sqlite_tools.run_sqlite(
        "data/memory.db", "SELECT count(*) AS n FROM messages")
    await sqlite_tools.run_sqlite("data/scratch.db", "CREATE TABLE t (a); DROP TABLE t;")
