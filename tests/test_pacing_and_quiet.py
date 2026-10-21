"""Human reply timing, and a silent treatment she can actually keep."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels import pacing
from ophelia.channels.session import ChannelSession
from ophelia.mind import quiet

CH = "telegram:1"


@pytest.fixture(autouse=True)
def _clean():
    quiet.end_all()
    yield
    quiet.end_all()


def test_typing_time_scales_with_length_and_counts_thinking():
    short, long = pacing.typing_seconds("ok", first=False, elapsed=0), \
        pacing.typing_seconds("x" * 600, first=False, elapsed=0)
    assert short < long <= 7.0 + 2.2
    # She already spent 5s thinking: a short first message goes out at once.
    assert pacing.typing_seconds("sure", first=True, elapsed=5) == 0
    assert pacing.typing_seconds("x" * 120, first=True, elapsed=1) == pytest.approx(3.6)


def test_sleepy_wait_only_when_just_woken_or_drowsy(monkeypatch):
    from ophelia.mind import body_clock

    st = SimpleNamespace(woken_by_owner=True, woke_at=time.time() - 30)
    clock = SimpleNamespace(state=st, phase=lambda: "steady")
    monkeypatch.setattr(body_clock, "get", lambda s: clock)
    monkeypatch.setattr(pacing.random, "random", lambda: 0.1)
    assert 15 <= pacing.sleepy_seconds(None) <= 75
    st.woken_by_owner = False
    assert pacing.sleepy_seconds(None) == 0
    clock.phase = lambda: "drowsy"
    assert 3 <= pacing.sleepy_seconds(None) <= 15


def test_only_on_when_configured():
    assert pacing.enabled(SimpleNamespace(human_pacing=True))
    assert not pacing.enabled(MagicMock())


@pytest.mark.asyncio
async def test_pace_shows_typing_while_waiting(monkeypatch):
    session = ChannelSession.__new__(ChannelSession)
    session.agent = SimpleNamespace(settings=SimpleNamespace(human_pacing=True))
    monkeypatch.setattr(pacing, "typing_seconds", lambda *a, **k: 0.2)
    monkeypatch.setattr(pacing, "sleepy_seconds", lambda s: 0.0)
    typing = AsyncMock()
    t0 = time.time()
    await session._pace("hello", first=True, elapsed=0,
                        actions=SimpleNamespace(typing=typing))
    assert time.time() - t0 >= 0.18 and typing.await_count >= 1


def test_quiet_is_capped_and_broken_by_apologies():
    q = quiet.start(CH, 240, "he called me all talk", cap_minutes=30)
    assert 29 * 60 < q.until - time.time() <= 30 * 60
    assert quiet.active(CH) is q
    for text in ("sorry", "I apologize", "this is urgent", "are you ok?", "/stop"):
        assert quiet.breaks_silence(text)
    assert not quiet.breaks_silence("pfft whatever")
    assert quiet.end_all() == [CH] and quiet.active(CH) is None


def _session():
    session = ChannelSession.__new__(ChannelSession)
    memory = MagicMock()
    memory.append_message = AsyncMock()
    settings = SimpleNamespace(is_owner_channel=lambda c: c == CH, interjections_enabled=False,
                               interjection_mode_resolved=lambda: "off")
    session.agent = SimpleNamespace(settings=settings, memory=memory)
    session.signals = SimpleNamespace(last_user_message_at=0)
    session._log_inbound = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_messages_wait_while_quiet_then_she_comes_back(monkeypatch):
    session = _session()
    q = quiet.start(CH, 1, "annoyed", cap_minutes=30)
    q.until = time.time() + 0.05
    replies = []
    await session._hold_while_quiet(CH, "hello??", q, reply=AsyncMock(), media_reply=None,
                                    log_context=None, actions=None)
    assert session.agent.memory.append_message.await_args.kwargs["metadata"] == {
        "type": "while_quiet"}
    handled = []

    async def fake_handle(channel, text, reply, **kw):
        handled.append(text)

    session.handle_chat = fake_handle
    await asyncio.sleep(0.15)
    assert len(handled) == 1
    assert handled[0].startswith("[Your silent treatment (annoyed) is over")
    assert '"hello??"' in handled[0]
    assert quiet.active(CH) is None


@pytest.mark.asyncio
async def test_go_quiet_tool_and_no_outreach_while_quiet():
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(quiet_max_minutes=20, primary_user_channel=lambda: CH)
    reg.set_owner(True)
    reg._current_sender_channel = CH
    out = await reg._go_quiet(90, "teasing me")
    assert "20 min" in out and quiet.active(CH) is not None
    reg._message_sender = None
    reg.proactive_sender = AsyncMock()
    assert "silent treatment" in await reg._send_message("miss me?")
    reg.proactive_sender.assert_not_awaited()
    reg.set_owner(False)
    assert "only go quiet on your owner" in await reg._go_quiet(5)
