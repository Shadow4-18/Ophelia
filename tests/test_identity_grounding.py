"""Tests for identity grounding — stop inventing who the speaker is.

Without an authoritative identity block in the system prompt, Ophelia
answered 'who am I?' by guessing from the guest list / memory fragments
and fabricating IDs. These tests lock in:
1. Owner and guest system prompts include a Who you're talking to block.
2. BASE_PROMPT forbids fabricating tool output / inventing IDs.
3. who_am_i_talking_to tool returns the current channel + owner/guest role.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.memory.store import MemoryStore


def test_base_prompt_forbids_fabricating_tool_output():
    from ophelia.core.agent_loop import BASE_PROMPT

    low = BASE_PROMPT.lower()
    assert "never fabricate" in low or "do not invent" in low
    assert "who you're talking to" in low or "who_am_i_talking_to" in low
    assert "guest list" in low


def test_identity_block_owner():
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = {
        "telegram:975946960",
        "discord:420353149522542592",
    }
    block = agent._identity_block("telegram:975946960", is_owner=True)
    assert "AUTHORITATIVE" in block
    assert "telegram:975946960" in block
    assert "OWNER" in block
    assert "GUEST" not in block or "Not a guest" in block
    assert "guest list" in block.lower()


def test_identity_block_guest():
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = {"telegram:975946960"}
    block = agent._identity_block("discord:333", is_owner=False)
    assert "AUTHORITATIVE" in block
    assert "discord:333" in block
    assert "GUEST" in block
    assert "telegram:975946960" in block


@pytest.mark.asyncio
async def test_owner_system_prompt_includes_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.honcho = None
    agent.life = None
    agent.humor = None
    agent.body_status = ""
    agent._memory_entries = []
    agent._user_entries = []
    agent.psyche = MagicMock()
    agent.psyche.to_context_block.return_value = ""
    agent.drives = MagicMock()
    agent.drives.to_context_block.return_value = ""
    agent.drives.social = 0.3
    agent.drives.agency = 0.3
    agent.settings = MagicMock()
    agent.settings.timezone = "UTC"
    agent.settings.owner_channels.return_value = {"telegram:111"}
    agent.settings.is_owner_channel.return_value = True

    prompt = await agent._system_prompt(channel="telegram:111", user_text="hi")
    assert "Who you're talking to" in prompt
    assert "telegram:111" in prompt
    assert "OWNER" in prompt


@pytest.mark.asyncio
async def test_guest_system_prompt_includes_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = {"telegram:111"}
    prompt = await agent._guest_system_prompt(channel="discord:99")
    assert "Who you're talking to" in prompt
    assert "discord:99" in prompt
    assert "GUEST" in prompt


@pytest.mark.asyncio
async def test_who_am_i_talking_to_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.is_owner_channel.return_value = True
    settings.owner_channels.return_value = {"telegram:111", "discord:222"}

    memory = MemoryStore(db_path=tmp_path / "test.db")
    await memory.init()
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = memory
    reg._current_sender_channel = "telegram:111"

    result = await reg._who_am_i_talking_to()
    assert "telegram:111" in result
    assert "OWNER" in result
    assert "discord:222" in result


@pytest.mark.asyncio
async def test_who_am_i_talking_to_guest(tmp_path, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.is_owner_channel.return_value = False
    settings.owner_channels.return_value = {"telegram:111"}

    memory = MemoryStore(db_path=tmp_path / "test.db")
    await memory.init()
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = memory
    reg._current_sender_channel = "discord:333"

    result = await reg._who_am_i_talking_to()
    assert "discord:333" in result
    assert "GUEST" in result
    assert "telegram:111" in result


def test_who_am_i_talking_to_tool_definition_exists():
    from ophelia.tools.registry import TOOL_DEFINITIONS

    names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "who_am_i_talking_to" in names
