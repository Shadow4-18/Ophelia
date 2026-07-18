"""Tests for text-before-tool round termination fix.

When the model emits prose before (or instead of) structured tool_calls,
``AgentLoop._complete`` used to finalize the turn immediately — tools never
ran. These tests lock in:

1. Content-embedded tool markup is recovered and dispatched.
2. Structured content+tool_calls still runs tools AND delivers the preamble.
3. Pure narration ("let me try") gets one recovery nudge instead of ending.
4. Parser helpers recognize common provider leak formats.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — break circular import
from ophelia.core.tool_call_parse import (
    extract_tool_calls_from_content,
    looks_like_tool_narration,
)


KNOWN = {"generate_image", "generate_video", "web_search", "send_message"}


def test_extract_json_fence_tool_call():
    text = (
        "okay one sec\n"
        '```json\n{"name": "generate_image", "parameters": {"prompt": "a cat"}}\n```'
    )
    calls, remaining = extract_tool_calls_from_content(text, known_tools=KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "generate_image"
    assert "cat" in calls[0].arguments
    assert "okay one sec" in remaining
    assert "generate_image" not in remaining


def test_extract_xml_tool_call():
    text = (
        'let me try\n<tool_call>{"name": "web_search", "arguments": '
        '{"query": "news"}}</tool_call>'
    )
    calls, remaining = extract_tool_calls_from_content(text, known_tools=KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "web_search"
    assert "let me try" in remaining


def test_extract_function_eq_markup():
    text = 'Tool called\n<function=generate_image>{"prompt": "fox"}</function>'
    calls, remaining = extract_tool_calls_from_content(text, known_tools=KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "generate_image"
    assert "fox" in calls[0].arguments


def test_extract_ignores_unknown_tool_names():
    text = '```json\n{"name": "not_a_real_tool", "parameters": {}}\n```'
    calls, _ = extract_tool_calls_from_content(text, known_tools=KNOWN)
    assert calls == []


def test_looks_like_tool_narration_positive():
    assert looks_like_tool_narration("okay, let me try")
    assert looks_like_tool_narration("I'll generate that now")
    assert looks_like_tool_narration("one sec")
    assert looks_like_tool_narration("*fires the tool*")
    assert looks_like_tool_narration("Tool called")
    assert looks_like_tool_narration(
        "Sent. This time I used Illustrious with explicit dark-gothic details."
    )
    assert looks_like_tool_narration("I just sent a new image")
    assert looks_like_tool_narration("used Pony V6 NSFW backend")


def test_looks_like_tool_narration_negative():
    assert not looks_like_tool_narration("here's a thought about cats")
    assert not looks_like_tool_narration("")
    assert not looks_like_tool_narration("the weather looks nice today")


def _msg(*, content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _choice(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tc(name: str, arguments: str = "{}", call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _agent_for_complete():
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.settings = MagicMock()
    agent.settings.max_tool_rounds = 6
    agent.settings.tool_loop_resume = False
    agent.use_tools = True
    agent.tools = MagicMock()
    agent.tools.tool_definitions = AsyncMock(
        return_value=[
            {
                "type": "function",
                "function": {"name": "generate_image", "parameters": {}},
            }
        ]
    )
    agent.tools.dispatch = AsyncMock(return_value="Image saved to /tmp/x.png")
    agent.tools._message_sender = AsyncMock()
    agent.tools.proactive_sender = None
    agent.stack = MagicMock()
    agent.stack.name.return_value = "test"
    agent._pending_resume = {}
    agent._continuation_count = {}
    agent.honcho = None
    agent._client = AsyncMock(return_value=MagicMock())
    agent._model = MagicMock(return_value="test-model")
    agent._store = AsyncMock()
    agent.search_past = AsyncMock(return_value="[]")
    return agent


@pytest.mark.asyncio
async def test_complete_runs_tools_when_text_and_structured_tool_calls():
    """Pass path: content + tool_calls in the same message must dispatch tools
    and deliver the preamble mid-turn (speak-then-act)."""
    agent = _agent_for_complete()
    delivered: list[str] = []

    async def _sender(chunk: str) -> None:
        delivered.append(chunk)

    agent.tools._message_sender = _sender

    responses = [
        _choice(
            _msg(
                content="okay, one sec",
                tool_calls=[
                    _tc("generate_image", '{"prompt": "a fox"}', "call_a"),
                ],
            )
        ),
        _choice(_msg(content="done — sent the fox", tool_calls=None)),
    ]
    agent._call_with_fallback = AsyncMock(side_effect=responses)

    out = await agent._complete(
        [{"role": "user", "content": "make a fox"}],
        store_channel="telegram:1",
        role="chat",
        is_owner=True,
    )

    assert out == "done — sent the fox"
    agent.tools.dispatch.assert_awaited_once()
    assert agent.tools.dispatch.await_args.args[0] == "generate_image"
    assert delivered == ["okay, one sec"]


@pytest.mark.asyncio
async def test_complete_recovers_tool_call_embedded_in_content():
    """Fail→fix path: text + JSON tool markup, empty tool_calls — must still
    dispatch instead of terminating after the prose."""
    agent = _agent_for_complete()
    responses = [
        _choice(
            _msg(
                content=(
                    "let me try\n"
                    '```json\n{"name": "generate_image", '
                    '"parameters": {"prompt": "a cat"}}\n```'
                ),
                tool_calls=None,
            )
        ),
        _choice(_msg(content="here's your cat", tool_calls=None)),
    ]
    agent._call_with_fallback = AsyncMock(side_effect=responses)

    out = await agent._complete(
        [{"role": "user", "content": "make a cat"}],
        store_channel="telegram:1",
        role="chat",
        is_owner=True,
    )

    assert out == "here's your cat"
    agent.tools.dispatch.assert_awaited_once()
    assert agent.tools.dispatch.await_args.args[0] == "generate_image"
    assert "cat" in agent.tools.dispatch.await_args.args[1]


@pytest.mark.asyncio
async def test_complete_narration_gets_one_recovery_then_tool():
    """Fail→fix path: pure narration with no markup — nudge once, then
    accept the subsequent structured tool call."""
    agent = _agent_for_complete()
    responses = [
        _choice(_msg(content="okay, let me try", tool_calls=None)),
        _choice(
            _msg(
                content="",
                tool_calls=[_tc("generate_image", '{"prompt": "fox"}', "call_b")],
            )
        ),
        _choice(_msg(content="sent it", tool_calls=None)),
    ]
    agent._call_with_fallback = AsyncMock(side_effect=responses)

    out = await agent._complete(
        [{"role": "user", "content": "fox please"}],
        store_channel="telegram:1",
        role="chat",
        is_owner=True,
    )

    assert out == "sent it"
    assert agent._call_with_fallback.await_count == 3
    agent.tools.dispatch.assert_awaited_once()
    # The narration nudge must have been injected into the message list for
    # the second LLM call.
    second_call_messages = agent._call_with_fallback.await_args_list[1].kwargs[
        "messages"
    ]
    assert any(
        m.get("role") == "system"
        and "did NOT emit a real tool call" in (m.get("content") or "")
        for m in second_call_messages
    )


@pytest.mark.asyncio
async def test_complete_plain_text_still_terminates_without_false_recovery():
    """Ordinary chat replies must not be forced into a tool-recovery loop."""
    agent = _agent_for_complete()
    agent._call_with_fallback = AsyncMock(
        return_value=_choice(_msg(content="hey, how's it going?", tool_calls=None))
    )

    out = await agent._complete(
        [{"role": "user", "content": "hi"}],
        store_channel="telegram:1",
        role="chat",
        is_owner=True,
    )

    assert out == "hey, how's it going?"
    agent.tools.dispatch.assert_not_awaited()
    assert agent._call_with_fallback.await_count == 1


@pytest.mark.asyncio
async def test_complete_narration_recovery_only_once():
    """If the model keeps narrating after the nudge, finalize — don't loop."""
    agent = _agent_for_complete()
    agent._call_with_fallback = AsyncMock(
        side_effect=[
            _choice(_msg(content="I'll generate that", tool_calls=None)),
            _choice(_msg(content="one sec, firing the tool", tool_calls=None)),
        ]
    )

    out = await agent._complete(
        [{"role": "user", "content": "image please"}],
        store_channel="telegram:1",
        role="chat",
        is_owner=True,
    )

    assert out == "one sec, firing the tool"
    assert agent._call_with_fallback.await_count == 2
    agent.tools.dispatch.assert_not_awaited()
