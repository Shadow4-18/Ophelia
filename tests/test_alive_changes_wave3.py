"""Tests for Wave 3 — Neuro-style concurrency, cadence, memory prefetch, play mode.

- 3a: ModelGate.is_local_busy() / is_role_busy(); consciousness yields to
      local-busy / own-role-busy, not any-busy.
- 3b: tick_interval_seconds floor lowered to 8s.
- 3c: _memory_prefetch auto-recalls relevant memories into the system prompt.
- 3d: play_hint activates when social + agency drives are both high.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure full import chain loads (avoid circular imports).
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.mind.drives import DriveState
from ophelia.mind.psyche import Mood, PsycheState


# --- Wave 3a: concurrency ---


def test_model_gate_is_local_busy_exists():
    """ModelGate should have an is_local_busy() method that reports only
    local-provider activity, not cloud per-role activity."""
    from ophelia.providers.model_gate import ModelGate

    gate = ModelGate()
    assert hasattr(gate, "is_local_busy")
    # No active sessions — not busy.
    assert gate.is_local_busy() is False


def test_model_gate_is_role_busy_exists():
    """ModelGate should have an is_role_busy(role) method for per-role
    re-entrancy guards."""
    from ophelia.providers.model_gate import ModelGate

    gate = ModelGate()
    assert hasattr(gate, "is_role_busy")
    assert gate.is_role_busy("consciousness") is False


@pytest.mark.asyncio
async def test_consciousness_yields_to_local_busy_not_any_busy():
    """The consciousness loop should use is_local_busy()/is_role_busy() rather
    than the old is_busy() gate, so cloud per-role concurrency works."""
    import inspect

    from ophelia.mind.consciousness import ConsciousnessLoop

    src = inspect.getsource(ConsciousnessLoop.run)
    # The old broad gate is gone.
    assert "is_busy()" not in src
    # The new granular gates are present.
    assert "is_local_busy" in src
    assert "is_role_busy" in src


# --- Wave 3b: faster cadence floor ---


def test_tick_interval_floor_is_8_seconds():
    """The minimum tick interval should now be 8s (down from 15s) so an
    aroused Ophelia ticks faster."""
    psyche = PsycheState(mood=Mood(arousal=1.0))  # max arousal
    interval = psyche.tick_interval_seconds(base=90)
    # At max arousal, factor = 0.45, so 90 * 0.45 = 40.5 — above floor.
    assert interval >= 8.0
    # With a tiny base, the floor kicks in.
    interval_floor = psyche.tick_interval_seconds(base=10)
    assert interval_floor == 8.0


def test_tick_interval_lower_than_old_floor():
    """Sanity: with a small base and high arousal, we can now get below 15s."""
    psyche = PsycheState(mood=Mood(arousal=1.0))
    interval = psyche.tick_interval_seconds(base=20)
    # 20 * 0.45 = 9.0 — below the old 15s floor, above the new 8s floor.
    assert interval == 9.0
    assert interval < 15.0


# --- Wave 3c: memory prefetch ---


@pytest.mark.asyncio
async def test_memory_prefetch_returns_empty_for_short_text():
    """Short messages shouldn't trigger a memory search (wasteful)."""
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    result = await agent._memory_prefetch("hi", "telegram:1")
    assert result == ""


@pytest.mark.asyncio
async def test_memory_prefetch_returns_empty_without_memory():
    """No memory store → no prefetch."""
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    result = await agent._memory_prefetch("tell me about that thing we discussed", "telegram:1")
    assert result == ""


@pytest.mark.asyncio
async def test_memory_prefetch_injects_hits():
    """When memory has hits, prefetch should format them into a block."""
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    agent.memory.search_messages = AsyncMock(
        return_value=[
            {"role": "user", "channel": "telegram:1", "content": "we talked about cats"},
        ]
    )
    agent.memory.search_lessons = AsyncMock(
        return_value=[{"lesson": "Owner prefers cats over dogs"}]
    )
    result = await agent._memory_prefetch(
        "what did we say about cats", "telegram:1"
    )
    assert "Relevant memories" in result
    assert "cats" in result.lower()
    assert "Owner prefers cats" in result


@pytest.mark.asyncio
async def test_memory_prefetch_handles_store_errors_gracefully():
    """If the memory store throws, prefetch should return '' (never crash
    the turn)."""
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    agent.memory.search_messages = AsyncMock(side_effect=RuntimeError("db locked"))
    agent.memory.search_lessons = AsyncMock(side_effect=RuntimeError("db locked"))
    result = await agent._memory_prefetch(
        "a sufficiently long user message", "telegram:1"
    )
    assert result == ""


# --- Wave 3d: playful output mode ---


def test_play_hint_inactive_when_drives_low():
    """When social or agency is below threshold, play mode is off."""
    from ophelia.mind.mood_behavior import play_hint

    drives = DriveState(social=0.5, agency=0.6)
    assert play_hint(drives) == ""


def test_play_hint_active_when_social_and_agency_high():
    """When both social >= 0.7 and agency >= 0.8, play mode activates."""
    from ophelia.mind.mood_behavior import play_hint

    drives = DriveState(social=0.75, agency=0.85)
    hint = play_hint(drives)
    assert hint != ""
    assert "Play mode" in hint
    # Should encourage loosening the filter.
    assert "filter" in hint.lower() or "tease" in hint.lower()


def test_play_hint_requires_both_drives_high():
    """High social alone or high agency alone isn't enough."""
    from ophelia.mind.mood_behavior import play_hint

    high_social_only = DriveState(social=0.9, agency=0.5)
    assert play_hint(high_social_only) == ""

    high_agency_only = DriveState(social=0.5, agency=0.9)
    assert play_hint(high_agency_only) == ""


def test_play_hint_none_drives_returns_empty():
    """None drives (e.g. before load) → empty hint."""
    from ophelia.mind.mood_behavior import play_hint

    assert play_hint(None) == ""


def test_play_hint_wired_into_system_prompt():
    """The system prompt builder should call play_hint so play mode actually
    reaches the model."""
    import inspect

    from ophelia.core.agent_loop import AgentLoop

    src = inspect.getsource(AgentLoop._system_prompt)
    assert "play_hint" in src
