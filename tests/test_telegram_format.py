"""Code blocks reach Telegram as real tap-to-copy blocks."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ophelia.channels.telegram_format import send, send_kwargs, to_entities


def _span(text, ent):
    raw = text.encode("utf-16-le")
    return raw[ent["offset"] * 2:(ent["offset"] + ent["length"]) * 2].decode("utf-16-le")


def test_fenced_lyrics_become_a_pre_block():
    msg = ("Lyrics field:\n```\n[Intro: whispered, close mic]\nsignals in the wet concrete\n"
           "```\nTitle it Light Left Burning.")
    text, ents = to_entities(msg)
    assert "```" not in text
    assert ents == [{"type": "pre", "offset": len("Lyrics field:\n"),
                     "length": len("[Intro: whispered, close mic]\nsignals in the wet concrete")}]
    assert _span(text, ents[0]).startswith("[Intro: whispered")
    assert text.endswith("\nTitle it Light Left Burning.")


def test_inline_code_and_languages_and_emoji_offsets():
    msg = "Style box 🎛️: `dark synthwave, 100 BPM` and\n```python\nprint('hi')\n```"
    text, ents = to_entities(msg)
    code, pre = ents
    assert code["type"] == "code" and _span(text, code) == "dark synthwave, 100 BPM"
    assert pre["type"] == "pre" and pre["language"] == "python"
    assert _span(text, pre) == "print('hi')"  # offsets right after a 2-unit emoji


def test_plain_text_is_untouched():
    assert to_entities("no code here, just *stars* and _lines_") == (
        "no code here, just *stars* and _lines_", [])
    assert send_kwargs("hello") == {"text": "hello"}
    lonely = "an unclosed ``` fence stays as text"
    assert to_entities(lonely) == (lonely, [])


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_if_telegram_rejects_it():
    from telegram.error import BadRequest

    call = AsyncMock(side_effect=[BadRequest("can't parse entities"), "ok"])
    assert await send(call, "```\nx\n```", chat_id=1) == "ok"
    first, second = call.await_args_list
    assert "entities" in first.kwargs and first.kwargs["text"] == "x"
    assert second.kwargs == {"text": "```\nx\n```", "chat_id": 1}
