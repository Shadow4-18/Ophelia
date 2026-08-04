"""Tests for Reaction mind + director react routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.mind.director import DirectorDecision
from ophelia.mind.reaction_mind import ReactionMind, _clean_reaction


def test_clean_reaction_strips_wrappers_and_caps():
    assert _clean_reaction('"hey!"') == "hey!"
    assert _clean_reaction("```\ncool\n```") == "cool"
    assert _clean_reaction('{"action": "message"}') == ""
    long = "x" * 400
    cleaned = _clean_reaction(long)
    assert len(cleaned) <= 280
    assert cleaned.endswith("…")


def test_clean_reaction_rejects_tool_leak():
    assert _clean_reaction("I'll call tool_intent now") == ""


@pytest.mark.asyncio
async def test_reaction_mind_disabled_returns_empty(settings):
    settings.reaction_enabled = False
    mind = ReactionMind(settings)
    assert mind.enabled is False
    out = await mind.react("hi", agent=MagicMock())
    assert out == ""


@pytest.mark.asyncio
async def test_reaction_mind_calls_consciousness_model(settings):
    settings.reaction_enabled = True
    mind = ReactionMind(settings)

    choice = SimpleNamespace(message=SimpleNamespace(content="  oh wow, fair.  "))
    resp = SimpleNamespace(choices=[choice])
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=resp))
        )
    )
    agent = MagicMock()
    agent._client = AsyncMock(return_value=client)
    agent._model = MagicMock(return_value="llama-test")
    agent.stack = MagicMock()
    agent.stack.name = MagicMock(return_value="ollama")
    agent.settings = settings

    decision = DirectorDecision(action="react", urgency="high", pace_hint="punchy")
    out = await mind.react(
        "lol what",
        agent=agent,
        psyche=None,
        decision=decision,
        recent_context="user: hi\nassistant: hey",
    )
    assert out == "oh wow, fair."
    agent._client.assert_awaited_with("reaction")
    client.chat.completions.create.assert_awaited()


def _make_agent(settings, memory, stack):
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.mind.psyche import PsycheState

    tools = MagicMock()
    return AgentLoop(settings, memory, tools, PsycheState(), stack=stack)


@pytest.mark.asyncio
async def test_run_reaction_turn_stores_and_returns(settings, memory):
    from ophelia.mind.reaction_mind import ReactionMind
    from ophelia.providers.router import ProviderStack

    settings.reaction_enabled = True
    stack = ProviderStack(settings)
    agent = _make_agent(settings, memory, stack)
    agent.reaction_mind = ReactionMind(settings)
    agent.reaction_mind.react = AsyncMock(return_value="nice.")

    decision = DirectorDecision(action="react", urgency="normal")
    out = await agent.run_reaction_turn(
        "telegram:owner", "yo", is_owner=True, decision=decision
    )
    assert out == "nice."
    hist = await memory.recent_messages("telegram:owner", limit=10)
    roles = [m["role"] for m in hist]
    assert "user" in roles and "assistant" in roles
    assert any(m["content"] == "nice." for m in hist)


@pytest.mark.asyncio
async def test_run_reaction_turn_empty_when_mind_fails(settings, memory):
    from ophelia.mind.reaction_mind import ReactionMind
    from ophelia.providers.router import ProviderStack

    settings.reaction_enabled = True
    stack = ProviderStack(settings)
    agent = _make_agent(settings, memory, stack)
    agent.reaction_mind = ReactionMind(settings)
    agent.reaction_mind.react = AsyncMock(return_value="")

    out = await agent.run_reaction_turn("telegram:owner", "yo", is_owner=True)
    assert out == ""
    hist = await memory.recent_messages("telegram:owner", limit=10)
    assert hist == []


def _stub_tools(agent) -> None:
    agent.tools = MagicMock()
    agent.tools.begin_turn_artifacts = MagicMock()
    agent.tools.set_owner = MagicMock()
    agent.tools.set_message_sender = MagicMock()
    agent.tools.clear_message_sender = MagicMock()
    agent.tools.clear_media_sender = MagicMock()
    agent.tools.clear_owner = MagicMock()
    agent.tools.consume_pending_artifacts = MagicMock(return_value=[])
    agent.tools._delivered_artifacts = set()
    agent.tools._current_sender_channel = None
    agent.humor = None


def _owner_session(settings, memory, signals, *, agent):
    from ophelia.channels.session import ChannelSession
    from ophelia.mind.drives import DriveState

    object.__setattr__(
        settings, "is_owner_channel", lambda channel: True
    )
    return ChannelSession(agent, signals, memory, DriveState())


@pytest.mark.asyncio
async def test_session_react_uses_fast_path_then_fallback(settings, memory, signals):
    """Director react → reaction turn; empty reaction → full run_turn."""
    from ophelia.mind.reaction_mind import ReactionMind
    from ophelia.providers.router import ProviderStack

    settings.reaction_enabled = True
    settings.director_enabled = True
    stack = ProviderStack(settings)
    agent = _make_agent(settings, memory, stack)
    agent.reaction_mind = ReactionMind(settings)
    _stub_tools(agent)

    director = MagicMock()
    director.available = MagicMock(return_value=True)
    director.decide = AsyncMock(
        return_value=DirectorDecision(
            action="react", urgency="high", pace_hint="one beat"
        )
    )
    agent.director = director

    agent.run_reaction_turn = AsyncMock(return_value="")
    agent.run_turn = AsyncMock(return_value="full reply")

    session = _owner_session(settings, memory, signals, agent=agent)
    replies: list[str] = []

    async def reply(chunk: str) -> None:
        replies.append(chunk)

    await session.handle_chat("cli:test", "hey", reply)
    agent.run_reaction_turn.assert_awaited()
    agent.run_turn.assert_awaited()
    assert any("full reply" in r for r in replies)


@pytest.mark.asyncio
async def test_session_react_skips_full_turn_when_reaction_lands(
    settings, memory, signals
):
    from ophelia.mind.reaction_mind import ReactionMind
    from ophelia.providers.router import ProviderStack

    settings.reaction_enabled = True
    stack = ProviderStack(settings)
    agent = _make_agent(settings, memory, stack)
    agent.reaction_mind = ReactionMind(settings)
    _stub_tools(agent)

    director = MagicMock()
    director.available = MagicMock(return_value=True)
    director.decide = AsyncMock(
        return_value=DirectorDecision(action="react", urgency="normal")
    )
    agent.director = director
    agent.run_reaction_turn = AsyncMock(return_value="ha!")
    agent.run_turn = AsyncMock(return_value="should not run")

    session = _owner_session(settings, memory, signals, agent=agent)
    replies: list[str] = []

    async def reply(chunk: str) -> None:
        replies.append(chunk)

    await session.handle_chat("cli:test", "hey", reply)
    agent.run_reaction_turn.assert_awaited()
    agent.run_turn.assert_not_awaited()
    assert replies == ["ha!"]
