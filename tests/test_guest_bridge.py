"""Tests for Wave 4 — guest mode rework + guest/owner bridge.

- 4a: Guest system prompt is warmer, mentions loyalty, allows full personality.
- 4b: Owner's system prompt includes a guest activity digest.
- 4c: Guest system prompt includes rapport notes + last-time gist.
- 4d/4e: set_guest_rapport tool + persistence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure full import chain loads (avoid circular imports).
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.memory.store import MemoryStore


# --- Wave 4a: guest system prompt rework ---


@pytest.mark.asyncio
async def test_guest_prompt_mentions_loyalty(tmp_path, monkeypatch):
    """The guest prompt should explicitly mention loyalty / owner ownership."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt(channel="discord:99")
    low = prompt.lower()
    assert "loyalty" in low or "your owner made you" in low
    assert "owner" in low


@pytest.mark.asyncio
async def test_guest_prompt_allows_full_personality(tmp_path, monkeypatch):
    """The guest prompt should explicitly permit full personality (joking,
    teasing, etc.) — not the old 'kiosk' vibe."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt(channel="discord:99")
    low = prompt.lower()
    assert "joke" in low or "tease" in low or "fully yourself" in low
    # Should NOT say "just talk" (the old restrictive phrasing).
    assert "just talk" not in low


@pytest.mark.asyncio
async def test_guest_prompt_mentions_continuity(tmp_path, monkeypatch):
    """The guest prompt should mention referring back to past conversations."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt(channel="discord:99")
    low = prompt.lower()
    assert "continuit" in low or "past conversation" in low or "history" in low


def test_guest_prompt_denies_system_access_tools():
    """set_guest_rapport must be in GUEST_DENIED_TOOLS (owner-only)."""
    from ophelia.tools.registry import GUEST_DENIED_TOOLS

    assert "set_guest_rapport" in GUEST_DENIED_TOOLS


# --- Wave 4b: owner activity digest ---


@pytest.mark.asyncio
async def test_recent_guest_activity_returns_per_channel(tmp_path, monkeypatch):
    """recent_guest_activity should return messages grouped by channel."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    store = MemoryStore(db_path=tmp_path / "test.db")
    await store.init()
    await store.append_guest_message("telegram:111", "user", "hi from guest 1")
    await store.append_guest_message("telegram:111", "assistant", "hey!")
    await store.append_guest_message("discord:222", "user", "hello from guest 2")

    activity = await store.recent_guest_activity(
        ["telegram:111", "discord:222", "telegram:999"], per_channel=5
    )
    assert "telegram:111" in activity
    assert "discord:222" in activity
    # Channel with no messages should be absent.
    assert "telegram:999" not in activity
    assert len(activity["telegram:111"]) == 2
    assert len(activity["discord:222"]) == 1


def test_activity_digest_formats_messages():
    """_activity_digest should compress recent messages into a one-liner."""
    from ophelia.memory.guests import _activity_digest

    msgs = [
        {"role": "user", "content": "what's your favorite color"},
        {"role": "assistant", "content": "black, obviously"},
    ]
    digest = _activity_digest(msgs)
    assert "they said" in digest
    assert "you said" in digest
    assert "favorite color" in digest


def test_activity_digest_empty_for_no_msgs():
    from ophelia.memory.guests import _activity_digest

    assert _activity_digest([]) == ""


def test_activity_digest_skips_long_messages():
    """Very long messages shouldn't dominate the digest."""
    from ophelia.memory.guests import _activity_digest

    msgs = [{"role": "user", "content": "x" * 300}]
    assert _activity_digest(msgs) == ""


def test_guests_context_block_includes_activity():
    """guests_context_block should include the activity digest when provided."""
    from ophelia.memory.guests import guests_context_block

    roster = [
        {
            "platform": "telegram",
            "user_id": 111,
            "channel": "telegram:111",
            "name": "Eri",
            "name_source": "owner",
            "status": "approved",
            "first_message": "",
            "last_ts": None,
        }
    ]
    activity = {
        "telegram:111": [
            {"role": "user", "content": "I got a new cat"},
            {"role": "assistant", "content": "that's amazing"},
        ]
    }
    block = guests_context_block(
        roster, owner_channel="telegram:owner", activity=activity
    )
    assert "Eri" in block
    assert "new cat" in block
    assert "recently with them" in block.lower()


# --- Wave 4c: guest rapport block ---


@pytest.mark.asyncio
async def test_guest_rapport_block_includes_owner_notes(tmp_path, monkeypatch):
    """When the owner has set rapport notes, they appear in the guest prompt."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MemoryStore(db_path=tmp_path / "test.db")
    await agent.memory.init()
    await agent.memory.set_fact(
        "guest_rapport:telegram:111", "Eri loves cats and is having a rough week"
    )
    block = await agent._guest_rapport_block("telegram:111")
    assert "Eri loves cats" in block
    assert "What you know about this guest" in block


@pytest.mark.asyncio
async def test_guest_rapport_block_includes_last_time_gist(tmp_path, monkeypatch):
    """The rapport block should include a gist of the last conversation."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MemoryStore(db_path=tmp_path / "test.db")
    await agent.memory.init()
    await agent.memory.append_guest_message(
        "telegram:111", "user", "we were talking about music"
    )
    await agent.memory.append_guest_message(
        "telegram:111", "assistant", "yeah I love darkwave"
    )
    block = await agent._guest_rapport_block("telegram:111")
    assert "Last time you talked" in block
    assert "music" in block.lower()


@pytest.mark.asyncio
async def test_guest_rapport_block_empty_when_no_history(tmp_path, monkeypatch):
    """No notes and no prior chat → empty block (guest prompt stays clean)."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MemoryStore(db_path=tmp_path / "test.db")
    await agent.memory.init()
    block = await agent._guest_rapport_block("telegram:999")
    assert block == ""


@pytest.mark.asyncio
async def test_guest_rapport_block_handles_missing_memory(tmp_path, monkeypatch):
    """No memory store → empty block, never raises."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    block = await agent._guest_rapport_block("telegram:111")
    assert block == ""


# --- Wave 4d/4e: set_guest_rapport tool ---


def test_set_guest_rapport_tool_definition_exists():
    """The set_guest_rapport tool should be defined."""
    from ophelia.tools.registry import TOOL_DEFINITIONS

    names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "set_guest_rapport" in names


@pytest.mark.asyncio
async def test_set_guest_rapport_owner_can_set(tmp_path, monkeypatch):
    """Owner can set a rapport note that persists in memory."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = MemoryStore(db_path=tmp_path / "test.db")
    await reg.memory.init()
    reg._is_owner = True
    reg.settings = MagicMock()
    reg.settings.data_dir = tmp_path

    result = await reg._set_guest_rapport("telegram", 111, "Eri likes cats")
    assert "remember" in result.lower()
    stored = await reg.memory.get_fact("guest_rapport:telegram:111")
    assert stored == "Eri likes cats"


@pytest.mark.asyncio
async def test_set_guest_rapport_guest_denied(tmp_path, monkeypatch):
    """Guests cannot set rapport notes."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = MemoryStore(db_path=tmp_path / "test.db")
    await reg.memory.init()
    reg._is_owner = False
    reg.settings = MagicMock()
    reg.settings.data_dir = tmp_path

    result = await reg._set_guest_rapport("telegram", 111, "trying to set notes")
    assert "only the owner" in result.lower()
    # Nothing was stored.
    stored = await reg.memory.get_fact("guest_rapport:telegram:111")
    assert stored is None


@pytest.mark.asyncio
async def test_set_guest_rapport_empty_note_clears(tmp_path, monkeypatch):
    """An empty note clears any existing rapport note."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = MemoryStore(db_path=tmp_path / "test.db")
    await reg.memory.init()
    reg._is_owner = True
    reg.settings = MagicMock()
    reg.settings.data_dir = tmp_path

    await reg._set_guest_rapport("telegram", 111, "old note")
    result = await reg._set_guest_rapport("telegram", 111, "")
    assert "cleared" in result.lower()
    stored = await reg.memory.get_fact("guest_rapport:telegram:111")
    assert stored == ""


@pytest.mark.asyncio
async def test_set_guest_rapport_round_trips_into_guest_prompt(tmp_path, monkeypatch):
    """End-to-end: owner sets a rapport note → it appears in the guest's
    system prompt next time. This is the bridge."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.tools.registry import ToolRegistry

    # Owner sets the note via the tool.
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = MemoryStore(db_path=tmp_path / "test.db")
    await reg.memory.init()
    reg._is_owner = True
    reg.settings = MagicMock()
    reg.settings.data_dir = tmp_path
    await reg._set_guest_rapport(
        "telegram", 111, "Eri is the owner's sister and loves cats"
    )

    # Guest's system prompt should now include the note.
    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = reg.memory
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt(channel="telegram:111")
    assert "Eri is the owner's sister" in prompt
