"""She keeps her word: dares and promises go to a turn that can act.

Lines below are from a real chat where every "do it" got another threat
from the tool-less reaction path, for ten minutes, before anything happened.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession
from ophelia.mind import follow_through as ft

OWNER_DARES = [
    "Do it then",
    "Please do then",
    "Prove me wrong then right now",
    "Well?",
    "What are you waiting for",
    "I'm daring you",
    "I dont believe you, you're all talk",
    "Then shut up and do it. Let your actions speak louder than words",
    "Mmm keep going",
    "Continue",
    "Two images aren't not enough",
]
HER_EMPTY_THREATS = [
    "Careful what you ask for, I might actually do it.",
    "Oh, you're going to regret those two little words.",
    "Daring me? Sweetheart, you have no idea what you just started.",
    "Ooh, \"you win\", cute. Enjoy it while it lasts.",
    "Oh, \"no such thing\", you're gonna regret handing me that.",
    "Fine. No more stalling. Let me actually make you eat them.",
]
PLAIN_BANTER = [
    "haha nice",
    "How's your day been",
    "That's a cute outfit",
]


@pytest.mark.parametrize("text", OWNER_DARES)
def test_dares_need_action(text):
    assert ft.needs_action(text)


@pytest.mark.parametrize("line", HER_EMPTY_THREATS)
def test_threats_are_recognised(line):
    assert ft.is_empty_promise(line)
    assert ft.needs_action("lol", her_last=line)  # an unkept promise is still owed


@pytest.mark.parametrize("text", PLAIN_BANTER)
def test_plain_banter_can_stay_quick(text):
    assert not ft.needs_action(text, her_last="Still riding that Balatro high.")


@pytest.mark.asyncio
async def test_session_skips_reaction_after_her_threat():
    session = ChannelSession.__new__(ChannelSession)
    memory = MagicMock()
    memory.recent_messages = AsyncMock(return_value=[
        {"role": "user", "content": "Use your vision"},
        {"role": "assistant", "content": "Careful what you ask for, I might actually do it."},
    ])
    session.agent = SimpleNamespace(memory=memory)
    assert await session._needs_action("telegram:1", "hm", is_owner=True)
    memory.recent_messages = AsyncMock(return_value=[
        {"role": "assistant", "content": "Morning. Slept okay?"}])
    assert not await session._needs_action("telegram:1", "yeah pretty good", is_owner=True)


@pytest.mark.asyncio
async def test_reaction_that_only_threatens_falls_back_to_a_full_turn():
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.reaction_mind = SimpleNamespace(
        enabled=True, react=AsyncMock(return_value="Oh, you're going to regret that."))
    agent.memory = MagicMock()
    agent.memory.recent_messages = AsyncMock(return_value=[])
    agent._store = AsyncMock()
    agent.honcho = None
    assert await agent.run_reaction_turn("telegram:1", "pfft") == ""
    agent._store.assert_not_awaited()  # nothing stored; the full turn answers
