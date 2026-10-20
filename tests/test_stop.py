"""/stop actually stops her: turns run in the background so the command is
received mid-turn, and it cancels the reply in progress plus anything queued
or autonomous."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.core import interrupts


@pytest.mark.asyncio
async def test_stop_all_cancels_tracked_tasks_but_not_the_caller():
    started = asyncio.Event()

    async def long_job():
        started.set()
        await asyncio.sleep(60)

    job = interrupts.track(asyncio.create_task(long_job()), "playing balatro")
    await started.wait()
    me = interrupts.track(asyncio.current_task(), "the /stop itself")
    assert set(interrupts.running()) >= {"playing balatro", "the /stop itself"}
    assert interrupts.stop_all(except_task=me) == ["playing balatro"]
    with pytest.raises(asyncio.CancelledError):
        await job
    assert "playing balatro" not in interrupts.running()


def _gateway():
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw._turn_lock = asyncio.Lock()
    gw.settings = SimpleNamespace(is_owner_channel=lambda ch: ch == "telegram:1")
    gw.games = None
    gw.replies = []

    async def reply_text(update, text):
        gw.replies.append(text)

    gw._reply_text = reply_text
    return gw


def _update(uid=1):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid))


@pytest.mark.asyncio
async def test_telegram_stop_reaches_her_mid_turn():
    gw = _gateway()
    started, finished = asyncio.Event(), []

    async def slow_turn(update, context):
        started.set()
        await asyncio.sleep(60)  # a long tool chain
        finished.append(True)

    handler = gw._in_background(slow_turn)
    await handler(_update(), None)  # returns at once; the turn runs on
    await handler(_update(), None)  # a second message queues behind it
    await asyncio.wait_for(started.wait(), 1)
    await gw.cmd_stop(_update(), None)
    await asyncio.sleep(0)
    assert gw.replies and gw.replies[0].startswith("Stopped. (reply to telegram:1")
    assert gw.replies[0].count("reply to telegram:1") == 2  # running + queued
    await asyncio.sleep(0.05)
    assert finished == [] and not gw._turn_lock.locked()
    await gw.cmd_stop(_update(), None)
    assert gw.replies[-1] == "Nothing was running."


@pytest.mark.asyncio
async def test_guests_cannot_stop_her():
    gw = _gateway()
    await gw.cmd_stop(_update(uid=999), None)
    assert gw.replies == []


@pytest.mark.asyncio
async def test_turns_still_run_one_at_a_time_in_order():
    gw = _gateway()
    order = []

    def make(n):
        async def turn(update, context):
            order.append(f"start {n}")
            await asyncio.sleep(0.01)
            order.append(f"end {n}")
        return gw._in_background(turn)

    for n in (1, 2, 3):
        await make(n)(_update(), None)
    for _ in range(50):
        if len(order) == 6:
            break
        await asyncio.sleep(0.01)
    assert order == ["start 1", "end 1", "start 2", "end 2", "start 3", "end 3"]


@pytest.mark.asyncio
async def test_stop_typed_on_other_surfaces():
    from ophelia.channels.session import ChannelSession

    session = ChannelSession.__new__(ChannelSession)
    session.agent = SimpleNamespace(settings=SimpleNamespace(is_owner_channel=lambda ch: True))

    async def busy():
        await asyncio.sleep(60)

    job = interrupts.track(asyncio.create_task(busy()), "reply to discord:5")
    await asyncio.sleep(0)
    reply = AsyncMock()
    await session.handle_chat("discord:5", "/stop", reply)
    reply.assert_awaited_once()
    assert "reply to discord:5" in reply.await_args.args[0]
    await asyncio.sleep(0)
    assert job.cancelled()
