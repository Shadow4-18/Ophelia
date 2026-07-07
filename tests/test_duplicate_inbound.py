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
