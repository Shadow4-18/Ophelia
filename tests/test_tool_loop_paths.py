"""The endings of the model+tools loop that other tests don't pin. Written
before _complete was split into steps; run against both."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.test_tool_tiers import _agent, _resp, _tc


def _run(agent, text="hi there"):
    return agent._complete([{"role": "user", "content": text}], store_channel="telegram:1")


@pytest.mark.asyncio
async def test_plain_answer_is_stored_and_clears_resume():
    agent = _agent("deepseek")
    agent._pending_resume = {}
    agent._continuation_count = {"telegram:1": 2}
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(content="  sure thing  ")])
    assert await _run(agent) == "sure thing"
    agent._store.assert_awaited_once()
    assert agent._store.await_args.args[:3] == ("telegram:1", "assistant", "sure thing")
    assert "telegram:1" not in agent._continuation_count


@pytest.mark.asyncio
async def test_same_tool_call_three_times_is_a_stuck_loop():
    agent = _agent("deepseek")
    same = _resp(tool_calls=[_tc("web_search", '{"query": "x"}')])
    agent._call_with_fallback = AsyncMock(side_effect=[same, same, same, same])
    agent._finalize_after_stuck = AsyncMock(return_value="here's what I found")
    assert await _run(agent) == "here's what I found"
    assert agent._call_with_fallback.await_count == 2  # repeat spotted on round 2
    agent._finalize_after_stuck.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_out_of_rounds_queues_a_resume():
    agent = _agent("deepseek")
    agent.settings.max_tool_rounds = 3
    agent.settings.tool_loop_resume = True
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(tool_calls=[_tc("web_search", f'{{"query": "q{i}"}}', f"c{i}")])
        for i in range(5)
    ])
    out = await _run(agent)
    assert "pick up where I left off" in out
    assert agent._pending_resume["telegram:1"]["rounds"] == 3
    roles = {m["role"] for m in agent._pending_resume["telegram:1"]["messages"]}
    assert roles <= {"assistant", "tool"}


@pytest.mark.asyncio
async def test_api_failure_raises_and_drops_resume_state():
    agent = _agent("deepseek")
    agent._pending_resume = {}
    agent._continuation_count = {"telegram:1": 1}
    agent._call_with_fallback = AsyncMock(side_effect=RuntimeError("503 overloaded"))
    with pytest.raises(RuntimeError, match="LLM call failed"):
        await _run(agent)
    assert "telegram:1" not in agent._continuation_count


@pytest.mark.asyncio
async def test_leaked_tick_json_never_becomes_a_chat_reply():
    agent = _agent("deepseek")
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(
        content='{"internal_thought": "x", "action": "silent", "mood": {"label": "calm"}}')])
    assert await _run(agent) == ""
    agent._store.assert_not_awaited()


@pytest.mark.asyncio
async def test_consciousness_turn_keeps_its_raw_json():
    agent = _agent("deepseek")
    raw = '{"internal_thought": "x", "action": "silent"}'
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(content=raw)])
    out = await agent._complete([{"role": "user", "content": "check in"}],
                                store_channel="consciousness", role="consciousness")
    assert out == raw
    assert agent._store.await_args.kwargs["metadata"] == {"type": "consciousness_json"}


@pytest.mark.asyncio
async def test_tool_result_reaches_the_next_round():
    agent = _agent("deepseek")
    agent.tools.dispatch = AsyncMock(return_value="it's sunny")
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(content="one sec", tool_calls=[_tc("web_search", '{"query": "weather"}')]),
        _resp(content="sunny out"),
    ])
    agent._deliver_mid_turn_preamble = AsyncMock()
    assert await _run(agent) == "sunny out"
    agent._deliver_mid_turn_preamble.assert_awaited_with("one sec")
    second = agent._call_with_fallback.await_args_list[1].kwargs["messages"]
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "it's sunny"}
