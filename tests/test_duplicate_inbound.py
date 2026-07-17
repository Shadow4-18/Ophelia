"""Tests for duplicate user message injection in prompts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_build_messages_does_not_duplicate_current_user_turn():
    """Storing the user message before building used to put it in history AND
    append it again at the end of the prompt."""
    from ophelia.core.agent_loop import AgentLoop

    memory = MagicMock()
    memory.recent_lessons = AsyncMock(return_value=[])
    memory.recent_inner_thoughts = AsyncMock(return_value=[])
    memory.recent_across_channels = AsyncMock(
        return_value=[
            {
                "channel": "telegram:1",
                "role": "user",
                "content": "hello",
                "metadata": {},
            }
        ]
    )

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = memory
    agent.settings = MagicMock()
    agent._guest_system_prompt = MagicMock(return_value="")
    agent._system_prompt = AsyncMock(return_value="")

    messages = await agent._build_messages("telegram:1", "hello", is_owner=True)

    user_contents = [m["content"] for m in messages if m["role"] == "user"]
    assert user_contents.count("hello") == 1


@pytest.mark.asyncio
async def test_build_messages_skips_consciousness_tick_json_in_chat():
    """Raw tick JSON on the consciousness channel must not enter chat prompts —
    that leak trains the model to post ticks instead of calling tools."""
    from ophelia.core.agent_loop import AgentLoop

    tick = (
        '{"internal_thought":"regen selfie","mood":{"valence":0.4,"arousal":0.5,'
        '"label":"determined"},"action":"silent"}'
    )
    memory = MagicMock()
    memory.recent_lessons = AsyncMock(return_value=[])
    memory.recent_inner_thoughts = AsyncMock(return_value=[])
    memory.recent_across_channels = AsyncMock(
        return_value=[
            {
                "channel": "consciousness",
                "role": "assistant",
                "content": tick,
                "metadata": {"type": "consciousness_json"},
            },
            {
                "channel": "consciousness",
                "role": "assistant",
                "content": "[inner] still thinking about the last image",
                "metadata": {"type": "inner"},
            },
            {
                "channel": "telegram:1",
                "role": "user",
                "content": "try again",
                "metadata": {},
            },
        ]
    )

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = memory
    agent.settings = MagicMock()
    agent._guest_system_prompt = MagicMock(return_value="")
    agent._system_prompt = AsyncMock(return_value="")

    messages = await agent._build_messages("telegram:1", "try again", is_owner=True)
    contents = [m["content"] for m in messages]
    assert not any("internal_thought" in c and '"action"' in c for c in contents)
    assert any("still thinking about the last image" in c for c in contents)
