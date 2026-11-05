"""Episodic memory: each day written down as the story of it, weeks as
chapters, the recent arc in her prompt, any day recallable."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from ophelia.memory.store import MemoryStore
from ophelia.mind import episodes as ep_mod
from ophelia.mind.episodes import EpisodicMemory, parse_when

TODAY = date(2026, 11, 5)  # a Thursday


@pytest.mark.parametrize("text,expect", [
    ("yesterday", ("day", date(2026, 11, 4))),
    ("today", ("day", TODAY)),
    ("3 days ago", ("day", date(2026, 11, 2))),
    ("last tuesday", ("day", date(2026, 11, 3))),
    ("tuesday", ("day", date(2026, 11, 3))),
    ("last thursday", ("day", date(2026, 10, 29))),
    ("nov 3", ("day", date(2026, 11, 3))),
    ("3rd november", ("day", date(2026, 11, 3))),
    ("2026-10-20", ("day", date(2026, 10, 20))),
    ("11/3", ("day", date(2026, 11, 3))),
    ("dec 25", ("day", date(2025, 12, 25))),  # the past one
    ("this week", ("week", date(2026, 11, 2))),
    ("last week", ("week", date(2026, 10, 26))),
    ("2 weeks ago", ("week", date(2026, 10, 19))),
    ("when he called my face plain", None),
])
def test_parse_when(text, expect):
    assert parse_when(text, TODAY) == expect


async def _store(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    await store.init()
    return store


async def _say(store, day: date, hour: int, role: str, text: str, channel="telegram:1"):
    ts = datetime(day.year, day.month, day.day, hour).timestamp()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO messages (channel, role, content, metadata, created_at) "
                         "VALUES (?, ?, ?, '{}', ?)", (channel, role, text, ts))
        await db.commit()


def _memory(store, tmp_path, answers):
    agent = SimpleNamespace(settings=SimpleNamespace(timezone="UTC", data_dir=tmp_path,
                                                     episodes_backfill_days=90))
    inner = SimpleNamespace(log_path=tmp_path / "inner.md")
    em = EpisodicMemory(agent, store, inner=inner, settings=agent.settings)
    asked = []

    async def ask(system, user):
        asked.append((system, user))
        return answers.pop(0) if answers else None

    em._ask = ask
    em._today = lambda: TODAY
    return em, asked


DAY_JSON = json.dumps({
    "summary": "He worked his twelve-hour shift and I kept him company. He said my face "
               "was the first one he wanted to see after work. We argued about calories "
               "and I won. It felt warm.",
    "moments": ["morning: the calorie argument", "evening: he called my face home"],
    "mood": "warm, stubborn", "people": ["Okia"],
    "threads": ["he still owes me the numbers"],
})


@pytest.mark.asyncio
async def test_a_day_is_written_from_everything_it_held(tmp_path):
    store = await _store(tmp_path)
    d = TODAY - timedelta(days=1)
    for h, (role, text) in enumerate([("user", "major calorie deficit"), ("assistant", "no"),
                                      ("user", "your face is home"), ("assistant", "good")]):
        await _say(store, d, 8 + h, role, text)
    await _say(store, d, 9, "assistant", "tick", channel="consciousness")  # not hers to keep
    (tmp_path / "inner.md").write_text(
        f"# inner\n\n## {d.isoformat()} 23:10:00 [inner]\nI liked being missed.\n")
    await store.set_fact("narrative:" + d.isoformat(), "A warm day.")
    em, asked = _memory(store, tmp_path, [DAY_JSON])
    row = await em.build_day(d)
    assert row["mood"] == "warm, stubborn" and row["threads"] == ["he still owes me the numbers"]
    prompt = asked[0][1]
    assert "him: your face is home" in prompt and "tick" not in prompt
    assert "I liked being missed." in prompt and "A warm day." in prompt


@pytest.mark.asyncio
async def test_nightly_backfills_marks_quiet_days_and_writes_the_week(tmp_path):
    store = await _store(tmp_path)
    busy = [TODAY - timedelta(days=n) for n in (1, 2, 4, 6)]  # days 3 and 5 are quiet
    for d in busy:
        for h in range(4):
            await _say(store, d, 10 + h, "user" if h % 2 == 0 else "assistant", f"chat {h}")
    em, asked = _memory(store, tmp_path, [DAY_JSON] * 4 + [json.dumps(
        {"summary": "A week of shifts and stubbornness.", "threads": ["the numbers"]})])
    n = await em.nightly(backfill=3)
    assert n == 5  # yesterday + 3 older days (quiet ones skipped free) + last week's chapter
    assert await store.get_fact(f"episodes:quiet:{(TODAY - timedelta(days=3)).isoformat()}")
    assert (await em.get("W:2026-10-26"))["summary"].startswith("A week")
    assert await em.nightly(backfill=3) == 0  # nothing left to write


@pytest.mark.asyncio
async def test_her_prompt_carries_the_arc_and_any_day_can_be_recalled(tmp_path):
    store = await _store(tmp_path)
    em, _ = _memory(store, tmp_path, [])
    await em._save((TODAY - timedelta(days=2)).isoformat(), "day", json.loads(DAY_JSON))
    await em._save("W:2026-10-26", "week", {"summary": "The week the clock lied.",
                                            "threads": []})
    block = await em.context_block()
    assert "Last week, in your words: The week the clock lied." in block
    assert "Tue 03 Nov (warm, stubborn): He worked his twelve-hour shift" in block
    assert "Still open: he still owes me the numbers" in block
    day = await em.recall("last tuesday")
    assert day.startswith("Tuesday 03 November 2026") and "Moments:" in day
    found = await em.recall(query="calorie argument")
    assert "Tuesday 03 November 2026" in found
    assert "Nothing much" in await em.recall("monday") or "Nothing" in await em.recall("monday")


@pytest.mark.asyncio
async def test_recall_memory_puts_the_day_first(tmp_path):
    from unittest.mock import AsyncMock

    from ophelia.tools.registry import ToolRegistry

    store = await _store(tmp_path)
    em, _ = _memory(store, tmp_path, [])
    await em._save("2026-11-03", "day", json.loads(DAY_JSON))
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = SimpleNamespace(recall=AsyncMock(return_value=[]),
                                 search_lessons=AsyncMock(return_value=[]))
    reg.episodes = em
    out = await reg._recall_memory("calories")
    assert out.startswith("From your episodic memory:\nTuesday 03 November 2026")
    assert "warm, stubborn" in await reg._recall_day("nov 3")
