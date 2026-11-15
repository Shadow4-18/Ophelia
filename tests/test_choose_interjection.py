"""choose mode: she decides what his mid-task message gets (interrupt /
fold in / finish first), says which, and 'finish first' has a deadline."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.core import interjections


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    interjections.reset()
    monkeypatch.setattr(interjections, "_POLL_S", 0.01)
    yield
    interjections.reset()


def _session(choice=("fold", ""), deadline=180):
    settings = SimpleNamespace(
        interjection_mode_resolved=lambda: "choose", interjections_enabled=True,
        is_owner_channel=lambda ch: True, interjection_finish_deadline_s=deadline,
        voice_reply_default=False, chat_log_enabled=False, humanize_replies=False)
    agent = MagicMock()
    agent.settings = settings
    agent.choose_interjection = AsyncMock(return_value=choice)
    agent._store = AsyncMock()
    s = ChannelSession(agent, MagicMock(), MagicMock(), MagicMock())
    s._log_inbound = AsyncMock()
    return s, agent


async def _interject(s, text):
    sent = []

    async def reply(chunk):
        sent.append(chunk)

    handled = await s.try_interject("telegram:1", text, reply=reply)
    return handled, sent


async def _task_running(seconds):
    """A turn on telegram:1 that ends after ``seconds``, or at its next step
    once a stop is asked for (checked every 10 ms, like rounds)."""
    interjections.open_for("telegram:1")
    interjections.set_task("telegram:1", "build the chip page")
    stopped = False
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            if interjections.stop_requested("telegram:1"):
                stopped = True
                break
            interjections.take_all("telegram:1")  # a step reads its mailbox
            await asyncio.sleep(0.01)
    finally:
        interjections.close_for("telegram:1")
    return stopped


@pytest.mark.asyncio
async def test_stop_now_always_interrupts_without_asking_her():
    s, agent = _session()
    task = asyncio.create_task(_task_running(5))
    await asyncio.sleep(0.02)
    handled, sent = await _interject(s, "stop, look at this first")
    assert handled is False  # runs as a normal turn now
    assert await task is True  # the task stopped at its next step
    agent.choose_interjection.assert_not_awaited()
    assert sent == ["Dropping that for now. What's up?"]


@pytest.mark.asyncio
async def test_her_fold_choice_goes_into_the_task_and_she_says_so():
    s, agent = _session(choice=("fold", "Adding the blue one in."))
    task = asyncio.create_task(_task_running(1))
    await asyncio.sleep(0.02)
    handled, sent = await _interject(s, "use the blue one")
    assert handled is True and sent == ["Adding the blue one in."]
    assert agent._store.await_args.kwargs["metadata"]["choice"] == "fold"
    task.cancel()


@pytest.mark.asyncio
async def test_finish_first_waits_for_the_task_then_answers():
    s, _ = _session(choice=("finish", "Two secs, finishing this."))
    task = asyncio.create_task(_task_running(0.2))
    await asyncio.sleep(0.02)
    handled, sent = await _interject(s, "how's it going?")
    assert handled is False and sent == ["Two secs, finishing this."]
    assert await task is False  # it finished on its own, not stopped


@pytest.mark.asyncio
async def test_finish_first_has_a_deadline():
    s, _ = _session(choice=("finish", "Almost there."), deadline=0.1)
    task = asyncio.create_task(_task_running(5))
    await asyncio.sleep(0.02)
    handled, sent = await _interject(s, "how's it going?")
    assert handled is False
    assert sent == ["Almost there.", "Okay, that's taking too long. Stopping it."]
    assert await task is True


@pytest.mark.asyncio
async def test_writing_again_while_she_finishes_interrupts():
    s, agent = _session(choice=("finish", "Nearly done."))
    task = asyncio.create_task(_task_running(5))
    await asyncio.sleep(0.02)
    first = asyncio.create_task(_interject(s, "how's it going?"))
    await asyncio.sleep(0.05)
    handled, sent = await _interject(s, "hello??")
    assert handled is False and sent == ["Dropping that for now. What's up?"]
    assert await task is True
    assert (await first)[0] is False
    assert agent.choose_interjection.await_count == 1


@pytest.mark.asyncio
async def test_a_stopped_turn_keeps_its_progress_for_her():
    from tests.test_tool_tiers import _agent, _resp, _tc

    agent = _agent("deepseek")
    agent.settings.tool_loop_resume = True
    calls = 0

    async def model(**kw):
        nonlocal calls
        calls += 1
        interjections.request_stop("telegram:1")  # he interrupts during round 1
        return _resp(tool_calls=[_tc("web_search", '{"query": "x"}', f"c{calls}")])

    agent._call_with_fallback = model
    from ophelia.core.agent_loop import _INTERJECT_CHANNEL

    tok = _INTERJECT_CHANNEL.set("telegram:1")
    interjections.open_for("telegram:1")
    interjections.set_task("telegram:1", "build the chip page")
    try:
        out = await agent._complete([{"role": "user", "content": "build the chip page"}],
                                    store_channel="telegram:1")
    finally:
        interjections.close_for("telegram:1")
        _INTERJECT_CHANNEL.reset(tok)
    assert out == "" and calls == 1
    pending = agent._pending_resume["telegram:1"]
    assert pending["autonomous"] is True  # hers to finish, not glued to his reply
    note = agent._store.await_args
    assert "set aside when he interrupted: build the chip page" in note.args[2]


@pytest.mark.asyncio
async def test_choice_parsing_and_fallback(monkeypatch):
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.providers import fallback

    agent = AgentLoop.__new__(AgentLoop)
    agent.settings, agent.stack = MagicMock(), MagicMock()
    replies = iter(['{"choice": "Interrupt", "say": "Okay, all yours."}', "no idea",
                    '{"choice": "dance"}'])
    monkeypatch.setattr(fallback, "complete_text", AsyncMock(side_effect=lambda *a, **k:
                                                              next(replies)))
    assert await agent.choose_interjection("telegram:1", "x") == ("interrupt",
                                                                 "Okay, all yours.")
    assert await agent.choose_interjection("telegram:1", "x") == ("fold", "")
    assert await agent.choose_interjection("telegram:1", "x") == ("fold", "")


def test_choose_is_the_default_mode():
    from ophelia.config import Settings

    s = Settings(_env_file=None)
    assert s.interjection_mode_resolved() == "choose"
    assert s.interjection_finish_deadline_s == 180
