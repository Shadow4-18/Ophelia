"""Every path through one consciousness check-in, end to end. Written to
pin behaviour before _tick was split into steps; run against both."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.memory.store import MemoryStore
from tests.test_autonomous_turns import _agent, _wire


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    await s.init()
    return s


def _loop(store, raw=None, pressure=0.9):
    from ophelia.mind.consciousness import ConsciousnessLoop

    loop = ConsciousnessLoop.__new__(ConsciousnessLoop)
    agent = _agent(store)
    agent.run_turn = AsyncMock(return_value="private note")
    agent.run_consciousness_tick = AsyncMock(return_value=raw or "")
    agent.run_autonomous_continuation = AsyncMock()
    agent.pending_resume_for = MagicMock(return_value=None)
    agent.tools = MagicMock()
    agent.tools.consume_pending_artifacts = MagicMock(return_value=[])
    agent.tools.audio_delivered_this_turn = MagicMock(return_value=False)
    notify = AsyncMock()
    _wire(loop, agent, store, notify)
    loop.drives.initiative_pressure = lambda: pressure
    loop._wakes = []
    loop.request_wake = lambda reason, urgent=False: loop._wakes.append(reason)
    return loop, agent, notify


def _tick(**kw):
    base = {"action": "silent", "internal_thought": "", "mood": {"valence": 0.1,
            "arousal": 0.4, "label": "calm"}}
    base.update(kw)
    return json.dumps(base)


@pytest.mark.asyncio
async def test_nothing_pushing_skips_the_model(store):
    loop, agent, notify = _loop(store, pressure=0.13)
    await loop._tick(idle_seconds=4000)
    agent.run_consciousness_tick.assert_not_awaited()
    assert loop._wakes == ["drive_rising"]  # rising: stir again soon
    loop, agent, _ = _loop(store, pressure=0.05)
    await loop._tick(idle_seconds=4000)
    agent.run_consciousness_tick.assert_not_awaited()
    assert loop._wakes == []


@pytest.mark.asyncio
async def test_a_wake_reason_always_thinks(store):
    loop, agent, _ = _loop(store, raw=_tick(), pressure=0.05)
    await loop._tick(idle_seconds=4000, wake_reason="chat_ended")
    agent.run_consciousness_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_director_skip_with_low_pressure_skips(store):
    from ophelia.mind.director import DirectorDecision

    loop, agent, _ = _loop(store, raw=_tick(), pressure=0.14)
    loop.goals.pick_for_tick.return_value = None
    loop.director = SimpleNamespace(available=lambda: True,
                                    decide=AsyncMock(return_value=DirectorDecision(
                                        action="skip", reason="meh")))
    await loop._tick(idle_seconds=4000)
    agent.run_consciousness_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_director_defer_stops_unless_something_is_due(store):
    from ophelia.mind.director import DirectorDecision

    loop, agent, _ = _loop(store, raw=_tick(), pressure=0.2)
    loop.director = SimpleNamespace(available=lambda: True,
                                    decide=AsyncMock(return_value=DirectorDecision(
                                        action="defer", reason="later")))
    await loop._tick(idle_seconds=4000)
    agent.run_consciousness_tick.assert_not_awaited()
    loop.goals.pick_for_tick.return_value = MagicMock(id="g1", description="write a poem")
    await loop._tick(idle_seconds=4000)
    agent.run_consciousness_tick.assert_awaited_once()  # due goal overrides


@pytest.mark.asyncio
async def test_message_reaches_him_and_is_remembered(store):
    loop, agent, notify = _loop(store, raw=_tick(
        action="message", internal_thought="I miss him",
        outward_message="hey. you alive over there?"))
    await loop._tick(idle_seconds=4000)
    notify.assert_awaited_once_with("hey. you alive over there?")
    rows = await store.recent_messages("telegram:1", limit=5)
    assert rows[-1]["content"] == "hey. you alive over there?"
    assert rows[-1]["metadata"]["type"] == "spontaneous"
    inner = await store.recent_messages("consciousness", limit=5)
    assert any(r["content"] == "[inner] I miss him" for r in inner)
    loop.governor.record_outreach.assert_called_once()
    loop.signals.mark_action.assert_awaited()


@pytest.mark.asyncio
async def test_blocked_outreach_is_noted_not_sent(store):
    loop, agent, notify = _loop(store, raw=_tick(action="message",
                                                 outward_message="still up?"))
    loop.governor.allow_outreach.return_value = (False, "too_soon")
    await loop._tick(idle_seconds=4000)
    notify.assert_not_awaited()
    inner = await store.recent_messages("consciousness", limit=5)
    assert any(r["content"].startswith("[wanted to say, blocked: too_soon]") for r in inner)


@pytest.mark.asyncio
async def test_long_outreach_is_trimmed_to_the_burst_cap(store):
    long = "This is a sentence. " * 60
    loop, agent, notify = _loop(store, raw=_tick(action="message", outward_message=long))
    await loop._tick(idle_seconds=4000)
    sent = notify.await_args.args[0]
    assert len(sent) < len(long.strip()) and sent.endswith(".")


@pytest.mark.asyncio
async def test_unreadable_answer_under_pressure_becomes_a_reflection(store):
    loop, agent, notify = _loop(store, raw="I keep thinking about the rain on the window.",
                                pressure=0.9)
    await loop._tick(idle_seconds=4000)
    inner = await store.recent_messages("consciousness", limit=5)
    assert any("rain on the window" in r["content"] for r in inner)
    notify.assert_not_awaited()
    loop, agent, _ = _loop(store, raw="not json at all", pressure=0.2)
    loop.goals.pick_for_tick.return_value = MagicMock(id="g", description="x")
    await loop._tick(idle_seconds=4000)
    agent.run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_act_closes_the_due_goal_and_runs_a_private_turn(store):
    goal = MagicMock(id="g1", description="draw something")
    loop, agent, notify = _loop(store, raw=_tick(action="act", internal_thought="draw",
                                                 tool_intent="draw a moth"))
    loop.goals.pick_for_tick.return_value = goal
    loop.goals.goals = [goal]
    await loop._tick(idle_seconds=4000)
    assert "[Autonomous act] draw a moth" in agent.run_turn.await_args.args[1]
    goal.mark_done.assert_called_once()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_unfinished_chain_is_resumed_instead_of_a_fresh_act(store):
    loop, agent, _ = _loop(store, raw=_tick(action="act", tool_intent="keep playing"))
    agent.pending_resume_for = MagicMock(return_value={"rounds": 3})
    await loop._tick(idle_seconds=4000)
    agent.run_autonomous_continuation.assert_awaited_once_with("telegram:1")
    agent.run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dream_is_offered_once_after_waking(store, monkeypatch):
    from ophelia.mind import morning

    marked = AsyncMock()
    monkeypatch.setattr(morning.DreamContinuity, "mark_surfaced", marked)
    loop, agent, _ = _loop(store, raw=_tick())
    loop._pending_dream_ref = "a library made of water"
    await loop._tick(idle_seconds=4000)
    assert "a library made of water" in agent.run_consciousness_tick.await_args.kwargs[
        "system_extra"]
    assert loop._pending_dream_ref is None
    marked.assert_awaited_once()


@pytest.mark.asyncio
async def test_mid_conversation_without_director_waits(store):
    loop, agent, _ = _loop(store, raw=_tick(action="message", outward_message="hi"))
    await loop._tick(idle_seconds=20)
    agent.run_consciousness_tick.assert_not_awaited()
