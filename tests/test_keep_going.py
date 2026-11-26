"""'Show me', 'I'm waiting', 'don't stop' never make her drop the task, and
work she set aside doesn't put a Continue button on every reply."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.core.agent_loop import AgentLoop, keep_going


@pytest.mark.parametrize("text", [
    "Show me baby", "Im waiting baby", "Come one baby, you're gonna let me go soft",
    "Dont you dare stop until I tell you to", "I dint want you to stop until I cum",
    "No body told you to stop and wait for a reply", "come on, send it",
])
def test_waiting_or_urging_is_keep_going(text):
    assert keep_going(text)


@pytest.mark.parametrize("text", [
    "stop, make a video instead", "hold on, wait", "never mind, show me later",
    "What's the weather", "cancel that", "actually no, do something else",
])
def test_real_stops_and_other_messages_are_not(text):
    assert not keep_going(text)


@pytest.mark.asyncio
async def test_interrupt_on_keep_going_becomes_fold(monkeypatch):
    agent = AgentLoop.__new__(AgentLoop)
    agent.settings = MagicMock()
    agent.stack = MagicMock()
    monkeypatch.setattr("ophelia.providers.fallback.complete_text",
                        AsyncMock(return_value='{"choice": "interrupt", "say": "ok stopping"}'))
    monkeypatch.setattr("ophelia.core.agent_loop.persona_system", lambda p: p, raising=False)
    choice, _ = await agent.choose_interjection("telegram:1", "Show me baby")
    assert choice == "fold"
    choice, _ = await agent.choose_interjection("telegram:1", "stop, do a video instead")
    assert choice == "interrupt"


@pytest.mark.asyncio
async def test_no_continue_button_for_her_set_aside_work():
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw.session = SimpleNamespace(agent=SimpleNamespace(_pending_resume={}))
    last = MagicMock(chat=MagicMock(), chat_id=1, message_id=9)
    gw._last_text_msg = last
    bot = MagicMock(edit_message_reply_markup=AsyncMock(), send_message=AsyncMock())
    ctx = SimpleNamespace(bot=bot)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=1))

    gw.session.agent._pending_resume["telegram:1"] = {"messages": [], "autonomous": True}
    await gw._maybe_attach_continue(upd, ctx, "telegram:1")
    bot.edit_message_reply_markup.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    gw.session.agent._pending_resume["telegram:1"] = {"messages": [], "autonomous": False}
    await gw._maybe_attach_continue(upd, ctx, "telegram:1")
    bot.edit_message_reply_markup.assert_awaited_once()
