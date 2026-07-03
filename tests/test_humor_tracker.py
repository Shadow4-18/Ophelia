"""Tests for HumorTracker — the regression-prone scoring path (Tier C #15).

Humor scoring is the kind of thing that breaks silently: a regex tweak, a
sign flip, or an emoji-range change and Ophelia stops learning what's funny.
These tests pin the contract of note_chat_reply → score_inbound_reply and
the sticker/emoji positive-signal path.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _tracker(memory):
    from ophelia.mind.humor_tracker import HumorTracker

    return HumorTracker(memory)


async def test_positive_reaction_scores_high(memory):
    t = await _tracker(memory)
    await t.note_outbound("imagine if I just ate your whole snack drawer lol")
    await t.score_inbound_reply("LMAO that got me")
    hints = await t.hints_for_prompt(limit=5)
    assert "landed" in hints
    assert "snack drawer" in hints


async def test_negative_reaction_scores_negative(memory):
    t = await _tracker(memory)
    await t.note_outbound("ok so plot twist: I'm the snack thief")
    await t.score_inbound_reply("not funny stop")
    hints = await t.hints_for_prompt(limit=5)
    assert "flopped" in hints


async def test_chat_reply_joke_is_tracked_only_if_joke_shaped(memory):
    t = await _tracker(memory)
    # joke-shaped reply → tracked as a pending chat-joke
    await t.note_chat_reply("lol imagine me doing taxes, I'd cry")
    assert t._pending_chat is not None
    # plain factual answer → NOT tracked
    await t.note_chat_reply("The meeting is at 3pm.")
    # The second call shouldn't overwrite the first pending joke.
    assert "taxes" in (t._pending_chat or "")


async def test_sticker_reaction_attributes_to_pending_outbound(memory):
    t = await _tracker(memory)
    await t.note_outbound("hot take: naps are just horizontal snacks")
    await t.note_sticker_reaction("😂")
    # Sticker clears the pending outbound (it's been attributed).
    assert t._pending_outbound is None
    hints = await t.hints_for_prompt(limit=5)
    assert "landed" in hints


async def test_sticker_attributes_to_pending_chat_when_no_outbound(memory):
    t = await _tracker(memory)
    await t.note_chat_reply("btw, I invented a new dance. it's called The Drop.")
    assert t._pending_chat is not None
    await t.note_sticker_reaction("💀")
    assert t._pending_chat is None


async def test_short_non_reaction_is_neutral_or_negative(memory):
    """A terse 'k' shouldn't register as a positive humor signal."""
    t = await _tracker(memory)
    await t.note_outbound("fun fact: I once tried to herd a cat. it did not work.")
    await t.score_inbound_reply("k")
    hints = await t.hints_for_prompt(limit=5)
    # 'k' alone should not be tagged 'landed'.
    assert "landed" not in hints or "flopped" in hints or "meh" in hints


async def test_emoji_only_helper():
    from ophelia.mind.humor_tracker import _is_emoji_only

    assert _is_emoji_only("😂") is True
    assert _is_emoji_only("💀🔥") is True
    assert _is_emoji_only("hello") is False
    assert _is_emoji_only("") is False
    assert _is_emoji_only("lol") is False  # ascii letters
    assert _is_emoji_only("a long sentence with emoji 😂") is False
