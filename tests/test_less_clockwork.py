"""Leftovers from the aliveness audit: less clockwork, more memory of what
works, less repetition."""

from __future__ import annotations

import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.mind.ambient_commentary import similarity
from ophelia.mind.goals import Goal, GoalStore
from ophelia.mind.humor_tracker import group_bits


def test_goal_timing_varies_per_cycle_but_is_stable():
    stretches = set()
    for i in range(40):
        g = Goal(id=f"g{i}", description="x", cadence_hours=8, last_done_at=1_700_000_000 + i)
        s = g._stretch()
        assert 0.75 <= s <= 1.5
        assert g._stretch() == s  # same cycle, same answer: no flickering due state
        stretches.add(round(s, 2))
    assert len(stretches) > 10  # not clockwork


def test_goal_due_respects_its_cycle():
    g = Goal(id="check-in", description="x", cadence_hours=1)
    g.last_done_at = time.time() - 3600 * 0.7
    assert not g.due()  # below the 0.75x minimum
    g.last_done_at = time.time() - 3600 * 1.6
    assert g.due()  # past the 1.5x maximum


def test_pick_is_weighted_not_always_top():
    store = GoalStore(goals=[
        Goal(id="big", description="x", priority=0.9, last_done_at=0),
        Goal(id="small", description="x", priority=0.4, last_done_at=0),
    ])
    picks = Counter(store.pick_for_tick().id for _ in range(400))
    assert picks["big"] > picks["small"] > 0


@pytest.mark.asyncio
async def test_schedule_learner_fades_old_activity(tmp_path):
    import aiosqlite

    from ophelia.mind import schedule_learner as sl

    db = tmp_path / "m.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE owner_activity (dow INTEGER, hour INTEGER, count "
                           "INTEGER, last_seen REAL, PRIMARY KEY (dow, hour))")
        old = time.time() - 42 * 86400  # two half-lives ago
        await conn.execute("INSERT INTO owner_activity VALUES (0, 9, 100, ?)", (old,))
        await conn.commit()
    learner = sl.ScheduleLearner(SimpleNamespace(db_path=db), SimpleNamespace(timezone="UTC"))
    counts = await learner._load_counts()
    assert counts[0][9] == pytest.approx(25, rel=0.02)
    from datetime import datetime, timezone

    await learner.record_owner_activity(datetime(2026, 10, 19, 9, tzinfo=timezone.utc))  # a Monday
    learner._cached = None
    assert (await learner._load_counts())[0][9] == pytest.approx(26, rel=0.02)


def test_jokes_told_different_ways_are_one_bit():
    events = [
        {"outbound": "you and your goblin energy at 3am again", "score": 1.0},
        {"outbound": "goblin energy detected, it's past midnight", "score": 0.8},
        {"outbound": "lol the toaster is plotting against you", "score": 0.9},
        {"outbound": "midnight goblin energy hours have begun", "score": 1.0},
    ]
    bits = group_bits(events)
    assert bits[0]["count"] == 3 and "goblin" in bits[0]["outbound"]
    assert bits[0]["avg_score"] == pytest.approx(0.933, rel=0.01)
    assert any(b["count"] == 1 and "toaster" in b["outbound"] for b in bits)


@pytest.mark.asyncio
async def test_running_bits_show_up_in_her_hints():
    from ophelia.mind.humor_tracker import HumorTracker

    tracker = HumorTracker.__new__(HumorTracker)
    tracker.memory = SimpleNamespace(
        humor_hints=AsyncMock(return_value=[{"outbound": "goblin energy", "user_reply": "lmao",
                                             "score": 1.0}]),
        humor_scored_events=AsyncMock(return_value=[
            {"outbound": "goblin energy at 3am", "score": 1.0},
            {"outbound": "midnight goblin energy again", "score": 0.8}]))
    hints = await tracker.hints_for_prompt()
    assert "running bit with him (landed 2x)" in hints


def test_ambient_similarity():
    a = "Discord open, #general channel, someone posted a cat meme"
    assert similarity(a, "Discord is open on #general, a cat meme was posted") > 0.4
    assert similarity(a, "Balatro, blind select screen, ante 2") < 0.1
