"""Owner messages that arrive mid-task are answered between steps, and she
carries on with the task instead of the message waiting until the end."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — break circular import
from ophelia.core import interjections
from ophelia.core import agent_loop as al

CH = "telegram:1"


@pytest.fixture(autouse=True)
def _clean():
    interjections.reset()
    yield
    interjections.reset()


def _choice(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, tool_calls=tool_calls))])


def _tc(name, arguments="{}", call_id="c"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _agent():
    agent = al.AgentLoop.__new__(al.AgentLoop)
    agent.settings = MagicMock()
    agent.settings.max_tool_rounds = 3
    agent.settings.tool_loop_resume = False
    agent.use_tools = True
    agent.tools = MagicMock()
    agent.tools.tool_definitions = AsyncMock(return_value=[
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("phone_tap", "send_message")
    ])
    agent.tools.proactive_sender = None
    agent.stack = MagicMock()
    agent.stack.name.return_value = "test"
    agent._pending_resume = {}
    agent._continuation_count = {}
    agent.honcho = None
    agent._client = AsyncMock(return_value=MagicMock())
    agent._model = MagicMock(return_value="m")
    agent._store = AsyncMock()
    agent.search_past = AsyncMock(return_value="[]")
    return agent


async def _run(agent, responses, *, message_during_round=1):
    """Run a chat turn on CH; the owner messages during tool round N."""
    sent: list[str] = []

    async def sender(chunk):
        sent.append(chunk)

    agent.tools._message_sender = sender
    offers: list[asyncio.Task] = []
    rounds = {"n": 0}

    async def dispatch(name, arguments):
        rounds["n"] += 1
        if rounds["n"] == message_during_round:
            offers.append(asyncio.create_task(interjections.offer(CH, "how's it going?")))
            await asyncio.sleep(0)  # let it post
        if name == "send_message":
            sent.append("[send_message]")
        return "ok"

    agent.tools.dispatch = AsyncMock(side_effect=dispatch)
    agent.seen = []  # what the model was shown on each call (copied: the list grows)
    replies = iter(responses)

    async def call(**kw):
        agent.seen.append(list(kw["messages"]))
        return next(replies)

    agent._call_with_fallback = AsyncMock(side_effect=call)
    token = al._INTERJECT_CHANNEL.set(CH)
    interjections.open_for(CH)
    try:
        out = await agent._complete([{"role": "user", "content": "play balatro"}],
                                    store_channel=CH, role="chat")
    finally:
        interjections.close_for(CH)
        al._INTERJECT_CHANNEL.reset(token)
    taken = await offers[0] if offers else None
    return out, sent, taken


@pytest.mark.asyncio
async def test_answers_mid_task_with_send_message_then_carries_on():
    agent = _agent()
    out, sent, taken = await _run(agent, [
        _choice(tool_calls=[_tc("phone_tap", '{"element": 3}')]),
        _choice(tool_calls=[_tc("send_message", '{"text": "going well, on ante 3"}')]),
        _choice(tool_calls=[_tc("phone_tap", '{"element": 5}')]),
        _choice(content="won the round!"),
    ])
    assert taken is True
    assert out == "won the round!"
    # The message reached her before the task ended, with the nudge.
    second_call = agent.seen[1]
    assert second_call[-2] == {"role": "user", "content": "how's it going?"}
    assert second_call[-1]["content"] == al._INTERJECTION_NUDGE
    # Both sides of the exchange are kept in history.
    stored = [(c.args[1], c.args[2], c.kwargs.get("metadata")) for c in agent._store.await_args_list]
    assert ("user", "how's it going?", {"type": "interjection"}) in stored
    assert ("assistant", "going well, on ante 3", {"type": "interjection_reply"}) in stored
    # Answering didn't eat the task's step budget (3 rounds + bonus).
    assert agent._call_with_fallback.await_count == 4


@pytest.mark.asyncio
async def test_plain_text_answer_is_sent_and_she_resumes():
    agent = _agent()
    out, sent, taken = await _run(agent, [
        _choice(tool_calls=[_tc("phone_tap", '{"element": 1}')]),
        _choice(content="pretty good! about to win"),
        _choice(tool_calls=[_tc("phone_tap", '{"element": 2}')]),
        _choice(content="done, I won"),
    ])
    assert taken is True
    assert sent == ["pretty good! about to win"]  # delivered mid-task
    assert out == "done, I won"
    third_call = agent.seen[2]
    assert third_call[-1]["content"] == al._RESUME_NUDGE


@pytest.mark.asyncio
async def test_unclaimed_message_falls_back_to_a_normal_turn():
    agent = _agent()
    # The task finishes right after the round in which the message arrives.
    out, sent, taken = await _run(agent, [
        _choice(tool_calls=[_tc("phone_tap")]),
        _choice(content="all done"),
    ], message_during_round=99)
    assert taken is None  # never posted
    interjections.open_for(CH)
    task = asyncio.create_task(interjections.offer(CH, "hi"))
    await asyncio.sleep(0)
    interjections.close_for(CH)
    assert await task is False


@pytest.mark.asyncio
async def test_offer_only_when_a_turn_is_open():
    assert await interjections.offer(CH, "hi") is False
    interjections.open_for(CH)
    t = asyncio.create_task(interjections.offer(CH, "one"))
    t2 = asyncio.create_task(interjections.offer(CH, "two"))
    await asyncio.sleep(0)
    assert interjections.take_all(CH) == ["one", "two"]
    assert await t is True and await t2 is True
    assert interjections.take_all("telegram:2") == []


@pytest.mark.asyncio
async def test_autonomous_and_other_channels_dont_take_messages():
    agent = _agent()
    interjections.open_for(CH)
    task = asyncio.create_task(interjections.offer(CH, "hey"))
    await asyncio.sleep(0)
    agent._call_with_fallback = AsyncMock(side_effect=[_choice(content="thinking")])
    agent.tools._message_sender = AsyncMock()
    # No _INTERJECT_CHANNEL set: this is not the owner's chat turn.
    await agent._complete([{"role": "user", "content": "tick"}], store_channel=CH, role="chat")
    assert not task.done()
    interjections.close_for(CH)
    assert await task is False


@pytest.mark.asyncio
async def test_session_try_interject_rules():
    session = ChannelSession.__new__(ChannelSession)
    session.agent = SimpleNamespace(settings=MagicMock())
    session.agent.settings.interjections_enabled = True
    session.agent.settings.is_owner_channel = lambda ch: ch == CH
    session.signals = SimpleNamespace(last_user_message_at=0)
    session._log_inbound = AsyncMock()
    interjections.open_for(CH)
    assert await session.try_interject("telegram:2", "hi") is False  # guest
    assert await session.try_interject(CH, "/stop") is False  # commands go their own way
    task = asyncio.create_task(session.try_interject(CH, "hi"))
    await asyncio.sleep(0)
    assert interjections.take_all(CH) == ["hi"]
    assert await task is True
    session._log_inbound.assert_awaited_once()
    session.agent.settings.interjections_enabled = False
    assert await session.try_interject(CH, "hi") is False


@pytest.mark.asyncio
async def test_telegram_text_mid_task_goes_to_the_running_turn():
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw._turn_lock = asyncio.Lock()
    gw._telegram_log_context = lambda user: {}
    ran: list[str] = []
    gw.session = SimpleNamespace(try_interject=AsyncMock(return_value=True))

    async def on_text(update, context):
        ran.append(update.message.text)

    gw.on_text = on_text
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                             message=SimpleNamespace(text="how's it going?"))
    async with gw._turn_lock:  # she's busy with a long turn
        await gw._in_background(gw.on_text)(update, None)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)
    gw.session.try_interject.assert_awaited_once()
    assert ran == []  # answered inside the running turn, not queued behind it

    gw.session.try_interject = AsyncMock(return_value=False)  # nothing running
    await gw._in_background(gw.on_text)(update, None)
    await asyncio.sleep(0.01)
    assert ran == ["how's it going?"]
