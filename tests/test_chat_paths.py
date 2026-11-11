"""Every path through handle_chat, end to end. Written to pin behaviour
before handle_chat was split into steps; run against both."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    real = asyncio.sleep

    async def quick(_s, *a, **k):
        await real(0)

    monkeypatch.setattr("ophelia.channels.session.asyncio.sleep", quick)
    monkeypatch.setattr(ChannelSession, "_pace", AsyncMock())
    monkeypatch.setattr("ophelia.channels.session.spawn",
                        lambda coro, name="": coro.close())


def _session(tmp_path, *, owner=True, out="hello you"):
    settings = SimpleNamespace(
        data_dir=tmp_path, chat_log_enabled=False, voice_reply_default=False,
        humanize_replies=False, is_owner_channel=lambda ch: owner,
    )
    tools = MagicMock()
    tools.consume_pending_artifacts = MagicMock(return_value=[])
    tools._delivered_artifacts = set()
    tools._pending_artifacts = []
    tools._doing_results = []
    tools.is_artifact_delivered = MagicMock(return_value=False)
    agent = MagicMock()
    agent.settings = settings
    agent.tools = tools
    agent.director = None
    agent.reaction_mind = None
    agent.humor = None
    agent.life = None
    agent.run_turn = AsyncMock(return_value=out)
    signals = MagicMock()
    signals.set_user_talking = AsyncMock()
    signals.set_agent_thinking = AsyncMock()
    memory = MagicMock()
    memory.save_drives = AsyncMock()
    drives = MagicMock()
    s = ChannelSession(agent, signals, memory, drives)
    s.try_interject = AsyncMock(return_value=False)
    s._log_inbound = AsyncMock()
    s._maybe_follow_through = MagicMock()
    s._catch_phantom = AsyncMock(side_effect=lambda ch, t, out, extra: out)
    return s, agent, tools, signals, drives


async def _chat(s, text="hi", **kw):
    sent = []

    async def reply(chunk):
        sent.append(chunk)

    await s.handle_chat("telegram:1", text, reply, **kw)
    return sent


@pytest.mark.asyncio
async def test_owner_reply_is_sent_and_the_turn_cleans_up(tmp_path):
    s, agent, tools, signals, drives = _session(tmp_path)
    sent = await _chat(s)
    assert sent == ["hello you"]
    agent.run_turn.assert_awaited_once()
    assert agent.run_turn.await_args.kwargs["is_owner"] is True
    drives.on_user_message.assert_called_once_with("hi")
    tools.set_owner.assert_called_with(True)
    tools.clear_message_sender.assert_called_once()
    tools.clear_owner.assert_called_once()
    assert tools._current_sender_channel is None
    signals.set_user_talking.assert_awaited_with(False)
    signals.set_agent_thinking.assert_awaited_with(False)
    s._maybe_follow_through.assert_called_once()


@pytest.mark.asyncio
async def test_guest_turn_leaves_her_drives_alone(tmp_path):
    s, agent, tools, _, drives = _session(tmp_path, owner=False)
    assert await _chat(s) == ["hello you"]
    drives.on_user_message.assert_not_called()
    tools.set_owner.assert_called_with(False)
    s._maybe_follow_through.assert_not_called()
    s._catch_phantom.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_reply_goes_out_in_pieces_and_tick_json_never_does(tmp_path):
    s, *_ = _session(tmp_path, out="first bit[[break]]second bit")
    assert await _chat(s) == ["first bit", "second bit"]
    s, *_ = _session(tmp_path, out='{"internal_thought": "x", "action": "silent"}')
    assert await _chat(s) == []


@pytest.mark.asyncio
async def test_a_crash_is_reported_and_still_cleans_up(tmp_path):
    s, agent, tools, signals, _ = _session(tmp_path)
    agent.run_turn = AsyncMock(side_effect=RuntimeError("boom"))
    assert await _chat(s) == ["Error: boom"]
    tools.clear_message_sender.assert_called_once()
    signals.set_agent_thinking.assert_awaited_with(False)


@pytest.mark.asyncio
async def test_files_left_over_are_sent_at_the_end(tmp_path):
    s, agent, tools, *_ = _session(tmp_path)
    pic = tmp_path / "a.png"
    pic.write_bytes(b"x")
    tools.consume_pending_artifacts = MagicMock(return_value=[pic])
    media = AsyncMock(return_value=True)
    await _chat(s, media_reply=media)
    media.assert_awaited_once_with(pic, "")
    tools._mark_artifact_delivered.assert_called_once_with(pic)
    tools.set_media_sender.assert_called_once()


@pytest.mark.asyncio
async def test_director_pace_reaches_the_turn_and_fast_reactions_skip_it(tmp_path):
    from ophelia.mind.director import DirectorDecision

    s, agent, *_ = _session(tmp_path)
    decision = DirectorDecision(action="speak", reason="", urgency="high",
                                pace_hint="keep it short")
    agent.director = SimpleNamespace(available=lambda: True,
                                     decide=AsyncMock(return_value=decision))
    await _chat(s)
    assert "keep it short" in agent.run_turn.await_args.kwargs["system_extra"]

    s, agent, *_ = _session(tmp_path)
    agent.director = SimpleNamespace(available=lambda: True, decide=AsyncMock(
        return_value=DirectorDecision(action="react", reason="", urgency="low")))
    agent.reaction_mind = SimpleNamespace(enabled=True)
    agent.run_reaction_turn = AsyncMock(return_value="lol")
    s._needs_action = AsyncMock(return_value=False)
    assert await _chat(s) == ["lol"]
    agent.run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_interjection_and_stop_end_before_a_turn(tmp_path):
    s, agent, *_ = _session(tmp_path)
    s.try_interject = AsyncMock(return_value=True)
    assert await _chat(s) == []
    agent.run_turn.assert_not_awaited()
    s, agent, *_ = _session(tmp_path)
    sent = await _chat(s, "/stop")
    assert sent and sent[0].startswith(("Stopped", "Nothing was running"))
    agent.run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_phantom_check_sees_the_reply(tmp_path):
    s, *_ = _session(tmp_path, out="here you go")
    s._catch_phantom = AsyncMock(return_value="sorry, that one failed")
    assert await _chat(s) == ["sorry, that one failed"]
