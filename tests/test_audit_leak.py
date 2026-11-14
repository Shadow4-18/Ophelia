"""Her own unfinished work kept turning into her replies to him (the
'audit' repeats, 2026-11-13). One test per cause."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from tests.test_tool_tiers import _agent, _resp, _tc


# 1. Her capped act turn is not resumed by his next message ------------------


@pytest.mark.asyncio
async def test_her_unfinished_work_waits_for_her_own_time():
    agent = _agent("deepseek")
    agent.settings.max_tool_rounds = 2
    agent.settings.tool_loop_resume = True
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(tool_calls=[_tc("run_code", f'{{"code": "print({i})"}}', f"c{i}")])
        for i in range(2)
    ])
    with agent.autonomous():
        await agent._complete([{"role": "user", "content": "[Autonomous act] audit"}],
                              store_channel="telegram:1")
    assert agent._pending_resume["telegram:1"]["autonomous"] is True

    # His message: answered fresh, her audit chain not glued onto it.
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(content="NExT, got it")])
    out = await agent._complete([{"role": "user", "content": "Pretty sure default is NExT"}],
                                store_channel="telegram:1")
    assert out == "NExT, got it"
    sent = agent._call_with_fallback.await_args.kwargs["messages"]
    assert not any(m.get("role") == "tool" for m in sent)
    assert "telegram:1" in agent._pending_resume  # still there for her
    assert agent.pending_resume_for("telegram:1") is not None


@pytest.mark.asyncio
async def test_his_own_unfinished_chain_still_resumes_on_his_next_message():
    agent = _agent("deepseek")
    agent.settings.max_tool_rounds = 1
    agent.settings.tool_loop_resume = True
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(tool_calls=[_tc("web_search", '{"query": "chip"}')])])
    await agent._complete([{"role": "user", "content": "look it up"}],
                          store_channel="telegram:1")
    assert agent._pending_resume["telegram:1"]["autonomous"] is False
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(content="found it")])
    await agent._complete([{"role": "user", "content": "go on"}], store_channel="telegram:1")
    sent = agent._call_with_fallback.await_args.kwargs["messages"]
    assert any(m.get("role") == "tool" for m in sent)  # picked up as before


# 2. The phantom check only fires when he asked for something ---------------


def test_phantom_check_needs_a_request():
    from ophelia.mind import follow_through as ft

    assert not ft.delivery_expected("It works, it plays when it recognizes my tag")
    assert not ft.delivery_expected("Okay")
    for asked in ("send me a pic", "another one", "where is it?", "didn't get it",
                  "make me a voice note"):
        assert ft.delivery_expected(asked), asked


@pytest.mark.asyncio
async def test_a_reply_to_it_works_is_never_redone():
    from tests.test_follow_through_now import _phantom_session

    s = _phantom_session("unused", redo_sends=False)
    s.agent.run_turn = AsyncMock()
    out = await s._catch_phantom("telegram:1", "It works, it plays when it recognizes my tag",
                                 "Here it is, your name out loud every time.")
    assert out.startswith("Here it is")
    s.agent.run_turn.assert_not_awaited()


# 3. Her thoughts reach a chat as private background, without repeats -------


@pytest.mark.asyncio
async def test_thoughts_render_as_private_notes_not_her_lines():
    from ophelia.core.agent_loop import AgentLoop

    rows = {
        "telegram:1": [{"channel": "telegram:1", "role": "user", "content": "Okay",
                        "metadata": {}, "created_at": 50.0}],
        "consciousness": [
            {"channel": "consciousness", "role": "assistant", "metadata": {"type": "inner"},
             "content": f"[inner] {t}", "created_at": 10.0 + i}
            for i, t in enumerate([
                "I should run the stub audit properly tonight",
                "I should run the stub audit properly tonight",
                "I should really run the stub audit properly tonight",
                "the chip page wants a memory of his taps"])
        ],
    }

    async def recent(channels, limit=35):
        return sorted((r for c in channels for r in rows.get(c, [])),
                      key=lambda r: r["created_at"])[-limit:]

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    agent.memory.recent_across_channels = AsyncMock(side_effect=recent)
    agent._system_prompt = AsyncMock(return_value="")
    agent.settings = MagicMock()
    msgs = await agent._build_messages("telegram:1", "Okay", is_owner=True)
    inner = [m for m in msgs if (m.get("content") or "").startswith("[INNER]")]
    assert all(m["role"] == "system" for m in inner)
    assert all("he never saw it" in m["content"] for m in inner)
    assert not any(m["role"] == "assistant" and "audit" in m["content"] for m in msgs)
    audit = [m for m in inner if "audit" in m["content"]]
    assert len(audit) == 1  # three near-identical nags collapse to one
    assert any("chip page" in m["content"] for m in inner)


# 4. A due goal overrides the director's "not now" once, then waits -----------


@pytest.mark.asyncio
async def test_due_goal_override_has_a_cooldown(tmp_path, monkeypatch):
    from ophelia.memory.store import MemoryStore
    from ophelia.mind import consciousness as c
    from ophelia.mind.director import DirectorDecision
    from tests.test_consciousness_paths import _loop

    store = MemoryStore(tmp_path / "m.db")
    await store.init()
    tick = json.dumps({"action": "silent", "internal_thought": "",
                       "mood": {"valence": 0, "arousal": 0.3, "label": "itchy"}})
    loop, agent, _ = _loop(store, raw=tick, pressure=0.33)
    loop.initiative_threshold = 0.6  # 0.33 is "well below threshold", as in the log
    loop.goals.pick_for_tick.return_value = MagicMock(id="self-reflect", description="x")
    loop.director = SimpleNamespace(available=lambda: True, decide=AsyncMock(
        return_value=DirectorDecision(action="defer", reason="not now")))
    now = [1000.0]
    monkeypatch.setattr(c.time, "monotonic", lambda: now[0])
    for _ in range(5):  # once a minute for five minutes
        await loop._tick(idle_seconds=4000)
        now[0] += 60
    assert agent.run_consciousness_tick.await_count == 1
    now[0] += c.GOAL_OVERRIDE_COOLDOWN_S
    await loop._tick(idle_seconds=4000)
    assert agent.run_consciousness_tick.await_count == 2
    # Real pressure still overrides every time.
    loop.drives.initiative_pressure = lambda: 0.95
    await loop._tick(idle_seconds=4000)
    await loop._tick(idle_seconds=4000)
    assert agent.run_consciousness_tick.await_count == 4
