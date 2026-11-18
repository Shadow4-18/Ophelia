"""Relays between conversations actually cross over (guest → owner and
owner → guest), instead of a "told him" with nothing sent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ophelia.mind import follow_through as ft


@pytest.mark.parametrize("text,hit", [
    ("let him know I said hi", True),
    ("pass this on to him", True),
    ("tell Eri I said goodnight", True),
    ("tell him I miss him", True),
    ("say hi to okia for me", True),
    ("Now tell okia hes gay", True),
    ("I told my mom about it", False),
    ("tell me a story", False),
    ("you can tell", False),
    ("what did he say", False),
])
def test_relay_asks_need_a_turn_with_tools(text, hit):
    assert ft.relay_expected(text) is hit
    assert ft.needs_action(text) is hit


@pytest.mark.parametrize("reply,hit", [
    ("done, told him 😏", True),
    ("<relay_to_owner>hi</relay_to_owner>", True),
    ("I let him know", True),
    ("message sent", True),
    ("lol he is so dumb", False),
    ("I told you already", False),
    ("sent you a pic", False),
])
def test_claimed_relay(reply, hit):
    assert bool(ft.claimed_relay(reply)) is hit


def _session(redo_relays: bool, redo_reply: str = "Sent it, he'll see it 😈"):
    from ophelia.channels.session import ChannelSession

    s = ChannelSession.__new__(ChannelSession)
    s._following_through = set()
    tools = SimpleNamespace(_tools_called=[])
    s.agent = SimpleNamespace(tools=tools)

    async def run_turn(channel, text, **kw):
        s.redo = (text, kw.get("is_owner"))
        if redo_relays:
            tools._tools_called.append("relay_to_owner")
        return redo_reply

    s.agent.run_turn = run_turn
    return s


@pytest.mark.asyncio
async def test_guest_relay_claim_without_the_tool_is_redone():
    s = _session(redo_relays=True)
    out = await s._catch_relay("telegram:2", "let him know he's an idiot",
                               "done, told him", False)
    assert out == "Sent it, he'll see it 😈"
    note, is_owner = s.redo
    assert is_owner is False and "relay_to_owner" in note
    assert "telegram:2" not in s._following_through


@pytest.mark.asyncio
async def test_owner_relay_redo_points_at_send_message_to_guest():
    s = _session(redo_relays=True)
    await s._catch_relay("telegram:1", "tell Eri goodnight", "told her 💕", True)
    assert "send_message_to_guest" in s.redo[0] and s.redo[1] is True


@pytest.mark.asyncio
async def test_still_not_relayed_drops_the_claim():
    s = _session(redo_relays=False, redo_reply="told him!\nanyway how was your day")
    out = await s._catch_relay("telegram:2", "pass this on", "passed it on", False)
    assert out == "anyway how was your day"


@pytest.mark.asyncio
async def test_real_relay_or_no_ask_is_left_alone():
    s = _session(redo_relays=True)
    s.agent.tools._tools_called = ["relay_to_owner"]
    assert await s._catch_relay("telegram:2", "let him know", "told him", False) == "told him"
    s.agent.tools._tools_called = []
    assert await s._catch_relay("telegram:2", "lol", "told him", False) == "told him"
    assert not hasattr(s, "redo")
