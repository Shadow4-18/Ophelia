"""Self-initiated turns stay private, and what she sends on her own is
remembered — the 'files went through' confusion."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.memory.store import MemoryStore


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    await s.init()
    return s


def _agent(store):
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = store
    return agent


@pytest.mark.asyncio
async def test_autonomous_messages_are_tagged_and_render_private(store):
    agent = _agent(store)
    with agent.autonomous():
        await agent._store("telegram:1", "user", "[Autonomous act] make a sketch", is_owner=True)
        await agent._store("telegram:1", "assistant", "Files went through.", is_owner=True)
    await agent._store("telegram:1", "user", "what do you mean?", is_owner=True)

    rows = await store.recent_messages("telegram:1", limit=5)
    assert [r["metadata"].get("type") for r in rows] == ["autonomous", "autonomous_note", None]

    agent._system_prompt = AsyncMock(return_value="# Persona (SOUL)\nx")
    msgs = await agent._build_messages("telegram:1", "what do you mean?")
    roles = [(m["role"], m["content"][:40]) for m in msgs[1:]]
    # Neither the self-started prompt nor the private note look like chat.
    assert roles[0][0] == "system" and "started on your own" in msgs[1]["content"]
    assert roles[1][0] == "system" and "owner never saw this" in msgs[2]["content"]
    assert msgs[-1] == {"role": "user", "content": "what do you mean?"}


@pytest.mark.asyncio
async def test_autonomous_flag_is_scoped(store):
    agent = _agent(store)
    with agent.autonomous():
        pass
    await agent._store("telegram:1", "assistant", "hi", is_owner=True)
    assert (await store.recent_messages("telegram:1"))[0]["metadata"] == {}


@pytest.mark.asyncio
async def test_background_send_message_is_remembered(store):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.memory = store
    reg.settings = MagicMock()
    reg.settings.primary_user_channel.return_value = "telegram:1"
    sent = []

    async def proactive(chunk):
        sent.append(chunk)

    reg.proactive_sender = proactive
    out = await reg._send_message("made you a little sketch, look")
    assert "Sent 1" in out and sent
    rows = await store.recent_messages("telegram:1")
    assert rows[-1]["content"] == "made you a little sketch, look"
    assert rows[-1]["metadata"]["type"] == "spontaneous"


@pytest.mark.asyncio
async def test_act_wrapup_is_not_forwarded(store):
    """The act turn's final text is a private note, never auto-sent."""
    from ophelia.mind.consciousness import ConsciousnessLoop

    loop = ConsciousnessLoop.__new__(ConsciousnessLoop)
    agent = _agent(store)
    agent.run_turn = AsyncMock(return_value="Files went through. Nothing to fuss about.")
    agent.run_consciousness_tick = AsyncMock(return_value=json.dumps(
        {"action": "act", "internal_thought": "sketch something", "tool_intent": "make a sketch"}
    ))
    agent.pending_resume_for = MagicMock(return_value=None)
    agent.tools = MagicMock()
    agent.tools.consume_pending_artifacts = MagicMock(return_value=[])
    notify = AsyncMock()
    _wire(loop, agent, store, notify)

    await loop._tick(idle_seconds=4000)

    agent.run_turn.assert_awaited_once()
    notify.assert_not_awaited()


def _wire(loop, agent, store, notify):
    from ophelia.mind.drives import DriveState
    from ophelia.mind.psyche import PsycheState

    loop.agent = agent
    loop.memory = store
    loop.signals = SimpleNamespace(
        last_action_at=0.0, mark_action=AsyncMock(), inner_mirror=False
    )
    loop.psyche = PsycheState()
    loop.drives = DriveState(social=1, curiosity=1, boredom=1, agency=1, expressiveness=1)
    loop.goals = MagicMock()
    loop.goals.pick_for_tick.return_value = None
    loop.goals.to_context_block.return_value = ""
    loop.governor = MagicMock()
    loop.governor.allow_outreach.return_value = (True, "ok")
    loop.vision = None
    loop.inner = None
    loop.games = None
    loop.initiative_threshold = 0.3
    loop.user_channel = "telegram:1"
    loop.notify = notify
    loop.notify_media = None
    loop.notify_voice = None
    loop.life = None
    loop.settings = None
    loop.humor = None
    loop.director = None
    loop.curiosity = None
    loop.action_cooldown = 0
    loop.idle_nudge_rotate = False
    loop._nudge_idx = 0
    loop._pending_dream_ref = None
    loop._feedback = None
