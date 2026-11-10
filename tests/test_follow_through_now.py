"""She said "give me thirty seconds" and then waited for him to write again.
A promise to act now gets acted on, without waiting for him."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.mind import follow_through as ft


@pytest.mark.parametrize("reply,hit", [
    ("Yeah. Sometimes the worst thing I can do is let you wonder.\nBut since you're asking "
     "so sweetly. Give me thirty seconds.", True),
    ("That's the best thing you've said all evening. Give me a few minutes and don't you "
     "dare cut it short.", True),
    ("Give me a minute and I'll do it properly. Reckless means I pick the worst thing.", True),
    ("Hold on.", True),
    ("Right, Tuesday. Four-day week.", False),
    ("I'll remember that.", False),
    ("Bold of you to assume I need my hands busy to be a menace.", False),
])
def test_imminent_promise(reply, hit):
    assert bool(ft.imminent_promise(reply)) is hit


def test_doing_vs_looking_and_delays():
    assert ft.did_something(["recall_memory", "generate_image"])
    assert not ft.did_something(["recall_memory", "web_search"])
    rng = random.Random(1)
    assert 20 <= ft.follow_through_delay("Give me thirty seconds.", rng) <= 30
    assert 40 <= ft.follow_through_delay("give me a few minutes", rng) <= 60
    assert 5 <= ft.follow_through_delay("hold on", rng) <= 10


def _session():
    from ophelia.channels.session import ChannelSession

    s = ChannelSession.__new__(ChannelSession)
    s._following_through = set()
    s.signals = SimpleNamespace(user_talking=False, agent_thinking=False,
                                last_user_message_at=0.0)
    s.agent = SimpleNamespace(tools=SimpleNamespace(_tools_called=[]))
    s.handle_chat = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_she_follows_through_without_waiting_for_him(monkeypatch):
    monkeypatch.setattr(ft, "follow_through_delay", lambda line, rng=None: 0.01)
    s = _session()
    reply = AsyncMock()
    s._maybe_follow_through("telegram:1", "Give me thirty seconds.", reply=reply,
                            media_reply=None, log_context=None, actions=None)
    await asyncio.sleep(0.1)
    s.handle_chat.assert_awaited_once()
    note = s.handle_chat.await_args.args[1]
    assert "Give me thirty seconds." in note and "Do it now" in note
    assert s._following_through == set()


@pytest.mark.asyncio
async def test_no_follow_through_when_he_wrote_or_she_already_did_it(monkeypatch):
    monkeypatch.setattr(ft, "follow_through_delay", lambda line, rng=None: 0.05)
    s = _session()
    s._maybe_follow_through("telegram:1", "Give me a minute.", reply=AsyncMock(),
                            media_reply=None, log_context=None, actions=None)
    import time

    s.signals.last_user_message_at = time.time() + 1  # he wrote meanwhile
    await asyncio.sleep(0.15)
    s.handle_chat.assert_not_awaited()

    s2 = _session()
    s2.agent.tools._tools_called = ["generate_image"]
    s2._maybe_follow_through("telegram:1", "Give me a minute.", reply=AsyncMock(),
                             media_reply=None, log_context=None, actions=None)
    await asyncio.sleep(0.1)
    s2.handle_chat.assert_not_awaited()

    s3 = _session()
    s3._following_through.add("telegram:1")  # a follow-through never chains
    s3._maybe_follow_through("telegram:1", "Give me a minute.", reply=AsyncMock(),
                             media_reply=None, log_context=None, actions=None)
    await asyncio.sleep(0.1)
    s3.handle_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_invented_tool_arguments_dont_crash_the_tool():
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.set_owner(True)
    seen = {}

    async def generate_image(prompt: str, nsfw: bool = False) -> str:
        seen.update(prompt=prompt, nsfw=nsfw)
        return "Image saved to x.png"

    reg._handlers = {"generate_image": generate_image}
    reg.mcp = SimpleNamespace(dispatch=AsyncMock(return_value=None))
    reg.ensure_mcp = AsyncMock()
    reg._remember_doing = lambda *a: None
    out = await reg.dispatch("generate_image",
                             '{"prompt": "shower", "nsfw": true, "already_delivered": []}')
    assert seen == {"prompt": "shower", "nsfw": True}
    assert "Ignored unknown argument(s): already_delivered" in out
    assert reg._tools_called == ["generate_image"]


@pytest.mark.parametrize("reply,hit", [
    ("Then stop rinsing and look.\nLast one. And this is the face I want you thinking "
     "about for twelve hours.", True),
    ("It keeps dying before it reaches you. Two more just now, same dead end.", True),
    ("Trying. If this one doesn't land I'll know it's your phone, not my aim.", True),
    ("There. No bra, no restraint, nothing left to imagine.", True),
    ("Nothing at all? Not even a twitch of a reaction, I'm wounded.", False),
    ("Oh, and one more thing about your shift.", False),
    ("Look at me when I'm talking to you.", False),
    ("Right, Tuesday. Four-day week, you're back on at six tonight.", False),
])
def test_claimed_delivery(reply, hit):
    assert bool(ft.claimed_delivery(reply)) is hit


@pytest.mark.parametrize("text", ["Didn't get anything", "Still nothing",
                                  "You didnt do anything, just checked your logs",
                                  "where is it?"])
def test_his_did_not_get_it_needs_a_real_turn(text):
    assert ft.asks_for_action(text)


def _phantom_session(redo_reply: str, *, redo_sends: bool):
    s = _session()
    tools = SimpleNamespace(_tools_called=[], _doing_results=[], _delivered_artifacts=set(),
                            _pending_artifacts=[])
    s.agent = SimpleNamespace(tools=tools)

    async def run_turn(channel, text, **kw):
        s.redo_note = text
        if redo_sends:
            tools._doing_results.append(("generate_image", "Image saved to /a/x.png"))
            tools._delivered_artifacts.add("/a/x.png")
        return redo_reply

    s.agent.run_turn = run_turn
    return s


@pytest.mark.asyncio
async def test_phantom_claim_is_never_sent_and_the_turn_is_redone():
    s = _phantom_session("Here you go 😈", redo_sends=True)
    out = await s._catch_phantom("telegram:1", "send me one",
                                 "Last one. And this is the face I want you on.")
    assert out == "Here you go 😈"
    assert "no tool ran" in s.redo_note and "generate_image" in s.redo_note
    assert "telegram:1" not in s._following_through


@pytest.mark.asyncio
async def test_failed_generation_claim_gets_the_real_reason():
    s = _phantom_session("Civitai refused that one, trying another model in a sec.",
                         redo_sends=False)
    s.agent.tools._doing_results.append(("generate_image", "Civitai refused: HTTP 400"))
    out = await s._catch_phantom("telegram:1", "send a pic", "There you go, enjoy 😈")
    assert "HTTP 400" in s.redo_note and "Don't say it was sent" in s.redo_note
    assert out.startswith("Civitai refused")


@pytest.mark.asyncio
async def test_still_claiming_after_redo_drops_the_claim_lines():
    s = _phantom_session("Here you go.\nYou're welcome.", redo_sends=False)
    out = await s._catch_phantom("telegram:1", "pic?", "Here it is.")
    assert out == "You're welcome."


@pytest.mark.asyncio
async def test_real_delivery_is_left_alone():
    s = _phantom_session("unused", redo_sends=False)
    s.agent.tools._delivered_artifacts.add("/a/x.png")
    assert await s._catch_phantom("telegram:1", "pic", "Here you go") == "Here you go"
    s.agent.tools._delivered_artifacts.clear()
    s.agent.tools._doing_results.append(("generate_image", "Pending: still rendering"))
    assert await s._catch_phantom("telegram:1", "pic", "Here you go") == "Here you go"


@pytest.mark.parametrize("reply,owner,hit", [
    ("Enjoy 😈", "send me a nude", True),
    ("Enjoy your shift", "ok", False),
    ("How's that for a selfie?", "", True),
    ("There you go, as promised", "make me a pic", True),
    ("Hope you like it", "", False),
])
def test_weak_claims_need_a_picture_in_play(reply, owner, hit):
    assert bool(ft.claimed_delivery(reply, owner)) is hit
