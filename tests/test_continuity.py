"""Inner-life continuity: open loops, people notes, outreach feedback, diary."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.memory.store import MemoryStore
from ophelia.mind.open_loops import OpenLoops, find_promises, loops_block
from ophelia.mind.outreach_feedback import MAX_FORWARD, MAX_QUIETER, OutreachFeedback
from ophelia.mind.people import PeopleBook, people_block


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    await s.init()
    return s


# --- open loops ---------------------------------------------------------------


def test_find_promises():
    reply = "Ooh, good question. I'll look that up tonight! Anyway how was work?"
    assert find_promises(reply) == ["I'll look that up tonight"]
    assert find_promises("Let me get back to you on the recipe.") == [
        "Let me get back to you on the recipe"
    ]
    assert find_promises("I looked it up yesterday, it's 42.") == []


@pytest.mark.asyncio
async def test_open_loop_lifecycle(store):
    loops = OpenLoops(store.db_path)
    a = await loops.add("telegram:1", "ask how the interview went", due_in_hours=0)
    assert await loops.add("telegram:1", "Ask how the interview went") == a  # dedupe
    b = await loops.add("telegram:1", "send the playlist", due_in_hours=5)

    due = await loops.due()
    assert [lp["id"] for lp in due] == [a]  # b isn't due yet
    assert "interview" in loops_block(due, title="DUE")

    # Ignored surfacings eventually expire it so it doesn't nag forever.
    for _ in range(3):
        await loops.mark_surfaced([a])
    assert await loops.due() == []
    assert [lp["id"] for lp in await loops.open("telegram:1")] == [b]

    assert await loops.close(b, "sent it") is True
    assert await loops.close(b) is False
    assert await loops.open("telegram:1") == []


@pytest.mark.asyncio
async def test_long_overdue_loops_expire(store):
    loops = OpenLoops(store.db_path)
    await loops.add("telegram:1", "old thing", due_in_hours=0)
    assert await loops.due(now=time.time() + 72 * 3600) == []


@pytest.mark.asyncio
async def test_promises_captured_from_owner_reply(store):
    loops = OpenLoops(store.db_path)
    ids = await loops.capture_promises("telegram:1", "Hmm I'll check on that for you.")
    assert len(ids) == 1
    assert (await loops.open("telegram:1"))[0]["source"] == "promise"


@pytest.mark.asyncio
async def test_tick_settles_loops(store):
    from ophelia.mind.consciousness import ConsciousnessLoop

    loops = OpenLoops(store.db_path)
    a = await loops.add("telegram:1", "ask about the dentist", due_in_hours=0)
    loop = ConsciousnessLoop.__new__(ConsciousnessLoop)
    loop.memory = store

    due = await loop._due_loops()
    await loop._settle_loops(due, "silent")
    assert (await loops.open())[0]["surfaced"] == 1
    await loop._settle_loops(due, "message")
    assert await loops.open() == []
    async with aiosqlite.connect(store.db_path) as db:
        rows = await db.execute_fetchall(
            "SELECT outcome FROM open_loops WHERE id = ?", (a,)
        )
    assert rows[0][0] == "tick:message"


# --- people -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_people_notes_surface_on_mention(store):
    book = PeopleBook(store.db_path)
    await book.remember("Maya", "owner's sister")
    await book.remember("maya", "started at a bakery downtown")
    await book.remember("Al", "owner's coworker")

    hits = await book.mentioned_in("did Maya like her first week?")
    assert [p["name"] for p in hits] == ["Maya"]
    block = people_block(hits)
    assert "sister" in block and "bakery" in block
    # Word-boundary match: "Al" must not fire on "always".
    assert await book.mentioned_in("I always forget") == []


# --- outreach feedback --------------------------------------------------------


async def _outreach(store, ts, *, replied):
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO humor_events (outbound, score, created_at, kind) "
            "VALUES ('hey', 0, ?, 'outreach')",
            (ts,),
        )
        if replied:
            await db.execute(
                "INSERT INTO messages (channel, role, content, metadata, created_at) "
                "VALUES ('telegram:1', 'user', 'hi!', '{}', ?)",
                (ts + 600,),
            )
        await db.commit()


@pytest.mark.asyncio
async def test_outreach_feedback_needs_evidence(store):
    await _outreach(store, time.time() - 5 * 3600, replied=False)
    assert await OutreachFeedback(store.db_path).threshold_delta() == 0.0


@pytest.mark.asyncio
async def test_ignored_outreach_makes_her_quieter(store):
    base = time.time() - 30 * 3600
    for i in range(6):
        await _outreach(store, base + i * 3 * 3600, replied=False)
    delta = await OutreachFeedback(store.db_path).threshold_delta()
    assert delta == pytest.approx(MAX_QUIETER)


@pytest.mark.asyncio
async def test_welcomed_outreach_makes_her_bolder(store):
    base = time.time() - 30 * 3600
    for i in range(6):
        await _outreach(store, base + i * 3 * 3600, replied=True)
    delta = await OutreachFeedback(store.db_path).threshold_delta()
    assert delta == pytest.approx(-MAX_FORWARD)


# --- diary ------------------------------------------------------------------


def _narrative(store, generated="Yesterday I talked with him about Maya's bakery."):
    from ophelia.mind.narrative import DailyNarrative

    agent = SimpleNamespace(settings=SimpleNamespace(timezone="UTC"))
    n = DailyNarrative(agent, store)
    n._generate = AsyncMock(return_value=generated)
    return n


@pytest.mark.asyncio
async def test_diary_written_once_for_a_busy_day(store):
    from datetime import datetime, timedelta, timezone

    yday = datetime.now(timezone.utc).replace(hour=12) - timedelta(days=1)
    async with aiosqlite.connect(store.db_path) as db:
        for i in range(5):
            await db.execute(
                "INSERT INTO messages (channel, role, content, metadata, created_at) "
                "VALUES ('telegram:1', 'user', ?, '{}', ?)",
                (f"message {i}", yday.timestamp() + i),
            )
        await db.commit()
    n = _narrative(store)
    assert "bakery" in await n.maybe_write()
    assert await n.maybe_write() is None  # already written
    n._generate.assert_awaited_once()
    assert "your diary" in await n.recent_block()


@pytest.mark.asyncio
async def test_quiet_day_skips_llm(store):
    n = _narrative(store)
    assert await n.maybe_write() is None
    n._generate.assert_not_awaited()
    assert await n.recent_block() == ""


# --- prompt wiring ------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuity_block_in_owner_prompt(store):
    from ophelia.core.agent_loop import AgentLoop

    await PeopleBook(store.db_path).remember("Maya", "owner's sister")
    await OpenLoops(store.db_path).add("telegram:1", "ask how the move went")
    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = store
    agent.narrative = None
    block = await agent._continuity_block("how's maya?", "telegram:1")
    assert "owner's sister" in block
    assert "ask how the move went" in block
    # The follow-up list belongs to that person's channel only.
    other = await agent._continuity_block("hello", "discord:2")
    assert "move" not in other


@pytest.mark.asyncio
async def test_follow_up_tools(store):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = store
    reg.settings = MagicMock()
    reg.settings.primary_user_channel.return_value = "telegram:1"
    out = await reg._follow_up("ask about the exam", in_hours=0)
    assert "Follow-up #1" in out
    assert "closed" in await reg._follow_up_done(1, "asked")
    assert "Noted about Maya" in await reg._remember_person("Maya", "sister")
