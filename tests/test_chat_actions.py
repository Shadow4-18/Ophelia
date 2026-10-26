"""Reactions and replies on Telegram: she sees his, and can do her own."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.chat_actions import MessageIndex, normalize_emoji, reaction_note
from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.channels.telegram_bot import TelegramGateway, _RecordingBot

BOT_ID = 999


def _index():
    idx = MessageIndex()
    idx.add(1, 10, "him", "Use your vision to look at both images")
    idx.add(1, 11, "her", "Careful what you ask for, I might actually do it.")
    idx.add(1, 12, "him", "Do it then")
    return idx


def test_index_finds_messages_by_their_words():
    idx = _index()
    assert idx.last(1, "him").message_id == 12
    assert idx.find(1, "careful what you ask").message_id == 11
    assert idx.find(1, "look at both images please").message_id == 10  # most words in common
    assert idx.find(1, "something never said") is None
    assert idx.get(1, 11).who == "her"


def test_emoji_variants_match_telegram():
    assert normalize_emoji("❤️") == "❤"
    assert "the hearts" not in reaction_note("❤", None)
    note = reaction_note("🤣", _index().get(1, 11))
    assert note.startswith("[He reacted 🤣 to your message") and "Careful" in note


def _gateway():
    gw = TelegramGateway.__new__(TelegramGateway)
    gw._index = _index()
    gw._app = SimpleNamespace(bot=SimpleNamespace(
        id=BOT_ID, set_message_reaction=AsyncMock(), send_message=AsyncMock()))
    gw.settings = SimpleNamespace(is_owner_channel=lambda ch: ch == "telegram:1")
    memory = MagicMock()
    memory.append_message = AsyncMock()
    gw.session = SimpleNamespace(agent=SimpleNamespace(memory=memory), note_reaction=AsyncMock(),
                                 _send_logged=AsyncMock())
    gw._allowed = lambda uid: uid == 1
    return gw


def _update(text="hm", *, reply_to=None, quote=None, message_id=13):
    msg = SimpleNamespace(text=text, caption=None, message_id=message_id,
                          reply_to_message=reply_to, quote=quote)
    return SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=1),
                           effective_user=SimpleNamespace(id=1))


def test_she_knows_what_hes_replying_to():
    gw = _gateway()
    hers = SimpleNamespace(text="Careful what you ask for", caption=None,
                           from_user=SimpleNamespace(id=BOT_ID, is_bot=True))
    out = gw._with_reply(_update("prove it", reply_to=hers), "prove it")
    assert out == '[Replying to your message: "Careful what you ask for"]\nprove it'
    partial = gw._with_reply(_update("this", reply_to=hers, quote=SimpleNamespace(text="ask for")),
                             "this")
    assert partial.startswith('[Replying to this part of your message: "ask for"]')
    his = SimpleNamespace(text="Do it then", caption=None,
                          from_user=SimpleNamespace(id=1, is_bot=False))
    assert "his own earlier message" in gw._with_reply(_update("^", reply_to=his), "^")
    assert gw._with_reply(_update("plain"), "plain") == "plain"


@pytest.mark.asyncio
async def test_she_can_react_to_a_message():
    gw = _gateway()
    actions = gw._chat_actions(_update(message_id=12), "telegram:1")
    out = await actions.react("❤️", "")
    gw._app.bot.set_message_reaction.assert_awaited_once_with(1, 12, reaction="❤")
    assert out.startswith("Reacted ❤")
    note = gw.session.agent.memory.append_message.await_args.args[2]
    assert note.startswith("[You reacted ❤ to his message")
    out = await actions.react("🦖", "")
    assert "doesn't allow" in out


@pytest.mark.asyncio
async def test_she_can_reply_to_a_specific_message():
    gw = _gateway()
    actions = gw._chat_actions(_update(message_id=12), "telegram:1")
    out = await actions.reply("both of them, obviously", "look at both images")
    assert "Don't repeat it" in out
    send = gw.session._send_logged.await_args.args[1]
    await send("both of them, obviously")
    kw = gw._app.bot.send_message.await_args.kwargs
    assert kw["reply_parameters"].message_id == 10


@pytest.mark.asyncio
async def test_his_reaction_reaches_her():
    gw = _gateway()
    from telegram import ReactionTypeEmoji

    mr = SimpleNamespace(user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1), message_id=11,
                         old_reaction=(), new_reaction=(ReactionTypeEmoji("🔥"),))
    await gw.on_reaction(SimpleNamespace(message_reaction=mr), None)
    channel, note, emoji = gw.session.note_reaction.await_args.args
    assert channel == "telegram:1" and emoji == "🔥"
    assert note == '[He reacted 🔥 to your message: "Careful what you ask for, I might actually do it."]'
    gw.session.note_reaction.reset_mock()
    mr.old_reaction = mr.new_reaction  # no change (or a removal): nothing to tell her
    await gw.on_reaction(SimpleNamespace(message_reaction=mr), None)
    gw.session.note_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_messages_she_sends_are_recorded(monkeypatch):
    from telegram import Bot
    from telegram.ext import Application

    sent = SimpleNamespace(message_id=55, chat=SimpleNamespace(id=1), text="hi you",
                           caption=None)
    monkeypatch.setattr(Bot, "_send_message", AsyncMock(return_value=sent))
    app = Application.builder().token("1:x").build()
    object.__setattr__(app.bot, "__class__", _RecordingBot)
    gw = _gateway()
    _RecordingBot.on_sent = staticmethod(gw._record_sent)
    try:
        await app.bot._send_message("sendMessage", {})
    finally:
        _RecordingBot.on_sent = None
    assert gw._index.get(1, 55).text == "hi you" and gw._index.get(1, 55).who == "her"


@pytest.mark.asyncio
async def test_tools_without_a_chat():
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.set_chat_actions(None)
    assert "aren't available" in await reg._react_to_message("❤")
    reg.set_chat_actions(SimpleNamespace(react=AsyncMock(return_value="Reacted ❤"),
                                         reply=AsyncMock(return_value="Sent")))
    assert await reg._react_to_message("❤", "do it") == "Reacted ❤"


@pytest.mark.asyncio
async def test_note_reaction_goes_in_history_without_a_reply():
    session = ChannelSession.__new__(ChannelSession)
    memory = MagicMock()
    memory.append_message = AsyncMock()
    humor = SimpleNamespace(note_sticker_reaction=AsyncMock())
    session.agent = SimpleNamespace(memory=memory, humor=humor,
                                    settings=SimpleNamespace(is_owner_channel=lambda c: True))
    session._chat_logger = None
    session._logger = lambda: None
    await session.note_reaction("telegram:1", "[He reacted ❤ to your message: \"hi\"]", "❤")
    assert memory.append_message.await_args.kwargs["metadata"] == {"type": "reaction_received"}
    humor.note_sticker_reaction.assert_awaited_once_with("❤")


@pytest.mark.asyncio
async def test_photos_she_sends_are_remembered_by_file(monkeypatch):
    from telegram import Bot, InputFile
    from telegram.ext import Application

    sent = SimpleNamespace(message_id=56, chat=SimpleNamespace(id=1), text=None, caption=None,
                           photo=[1])
    monkeypatch.setattr(Bot, "_send_message", AsyncMock(return_value=sent))
    app = Application.builder().token("1:x").build()
    object.__setattr__(app.bot, "__class__", _RecordingBot)
    seen = []
    _RecordingBot.on_sent_photo = staticmethod(lambda msg, name: seen.append((msg.message_id,
                                                                              name)))
    try:
        photo = InputFile(b"\x89PNG....", filename="civitai_1_2_0.png")
        await app.bot._send_message("sendPhoto", {"photo": photo})
        await app.bot._send_message("sendMessage", {"text": "hi"})
    finally:
        _RecordingBot.on_sent_photo = None
    assert seen == [(56, "civitai_1_2_0.png")]
