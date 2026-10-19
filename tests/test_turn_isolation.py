"""Regression tests for state shared across concurrent turns / subsystems.

- The tool registry is shared by every channel. Its per-turn owner flag,
  sender channel and reply callbacks were plain attributes, so overlapping
  turns (Telegram + Discord, or a guest-approval replay launched with
  create_task) could hand a guest the owner's tool powers once another turn
  finished and reset the flag to "owner".
- Orchestrator.start() rebound psyche/drives to freshly loaded objects, so
  the hub, tool registry and director kept the throwaway defaults; owner
  messages then bumped and saved over the stale DriveState.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.tools.registry import ToolRegistry

OWNER = "telegram:111"
GUEST = "discord:222"


def _registry() -> ToolRegistry:
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._mcp_ready = True
    reg.mcp = MagicMock()
    reg.mcp.dispatch = AsyncMock(return_value=None)
    reg._handlers = {"edit_soul": AsyncMock(return_value="soul edited")}
    reg._pending_artifacts = []
    reg._delivered_artifacts = set()
    reg.proactive_sender = None
    reg.proactive_media_sender = None
    reg.begin_turn_artifacts = MagicMock()
    return reg


def _session(reg: ToolRegistry, run_turn, tmp_path) -> ChannelSession:
    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.chat_log_enabled = False
    settings.is_owner_channel.side_effect = lambda ch: ch == OWNER
    settings.voice_reply_default = False

    agent = MagicMock()
    agent.settings = settings
    agent.tools = reg
    agent.director = None
    agent.humor = None
    agent.life = None
    agent.run_turn = run_turn

    signals = MagicMock()
    signals.set_user_talking = AsyncMock()
    signals.set_agent_thinking = AsyncMock()

    memory = MagicMock()
    memory.save_drives = AsyncMock()

    session = ChannelSession(agent, signals, memory, MagicMock())
    session._chat_logger = None
    session._log_hooks = []
    session._wake_after_chat = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_guest_turn_keeps_sandbox_when_owner_turn_overlaps(tmp_path):
    reg = _registry()
    owner_done = asyncio.Event()
    seen: dict[str, object] = {}

    async def run_turn(channel, text, **kw):
        if channel == GUEST:
            # Let the owner's whole turn (set_owner(True) … clear_owner())
            # happen while this guest turn is still mid-flight.
            await owner_done.wait()
            seen["guest_tool"] = await reg.dispatch("edit_soul", "{}")
            seen["guest_sender_channel"] = reg._current_sender_channel
            seen["guest_message_sender"] = reg._message_sender
        return "ok"

    session = _session(reg, run_turn, tmp_path)
    guest_replies: list[str] = []
    owner_replies: list[str] = []

    async def guest_reply(chunk: str) -> None:
        guest_replies.append(chunk)

    async def owner_reply(chunk: str) -> None:
        owner_replies.append(chunk)

    guest_task = asyncio.create_task(session.handle_chat(GUEST, "hi", guest_reply))
    await asyncio.sleep(0)  # guest turn starts and parks in run_turn
    await asyncio.create_task(session.handle_chat(OWNER, "hey", owner_reply))
    owner_done.set()
    await guest_task

    assert "owner-only" in seen["guest_tool"]
    assert seen["guest_sender_channel"] == GUEST
    # Mid-turn send_message in the guest turn must reach the guest, not the
    # owner (and not the cleared/None sender left by the owner's turn).
    sender = seen["guest_message_sender"]
    assert sender is not None
    await sender("mid-turn")
    assert "mid-turn" in guest_replies
    assert "mid-turn" not in owner_replies


@pytest.mark.asyncio
async def test_turn_state_defaults_and_reset_outside_turns():
    reg = _registry()
    assert reg._is_owner is True
    assert reg._current_sender_channel is None
    assert reg._message_sender is None

    reg.set_owner(False)
    assert reg._is_owner is False
    reg.clear_owner()
    assert reg._is_owner is True


@pytest.mark.asyncio
async def test_load_persisted_state_updates_shared_instances(tmp_path):
    from ophelia.core.orchestrator import Orchestrator
    from ophelia.memory.store import MemoryStore
    from ophelia.mind.drives import DriveState
    from ophelia.mind.psyche import PsycheState

    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    saved_drives = DriveState(social=0.9, boredom=0.7)
    saved_psyche = PsycheState(internal_thought="still thinking about it")
    await store.save_drives(saved_drives)
    await store.save_psyche(saved_psyche)

    orch = Orchestrator.__new__(Orchestrator)
    orch.memory = store
    orch.psyche = PsycheState()
    orch.drives = DriveState()
    # Stand-ins for the hub / tool registry / director references taken in
    # __init__ — they must observe the loaded values.
    held_psyche, held_drives = orch.psyche, orch.drives

    await orch._load_persisted_state()

    assert orch.drives is held_drives
    assert orch.psyche is held_psyche
    assert held_drives.social == pytest.approx(0.9)
    assert held_drives.boredom == pytest.approx(0.7)
    assert held_psyche.internal_thought == "still thinking about it"
