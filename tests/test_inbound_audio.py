"""Songs he sends arrive: audio documents, Telegram music messages, and a
clear answer when a file is over Telegram's 20 MB bot limit."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.inbound_media import (
    audio_prompt,
    classify_attachment,
    too_big_for_telegram,
)


@pytest.mark.parametrize("name, mime", [
    ("Life Left Burning.wav", "audio/x-wav"), ("song.mp3", ""), ("take.flac", ""),
    ("memo.m4a", "audio/mp4"), ("", "audio/mpeg"),
])
def test_audio_files_are_accepted(name, mime):
    assert classify_attachment(filename=name, mime=mime) == "audio"


def test_size_limit_message():
    assert too_big_for_telegram(5 * 1024 * 1024) == ""
    msg = too_big_for_telegram(int(47.6 * 1024 * 1024))
    assert "47.6 MB" in msg and "20 MB" in msg and "mp3" in msg


def test_audio_prompt_tells_her_what_she_can_do_with_it():
    p = audio_prompt(Path("/d/in_9.mp3"), name="Life Left Burning.mp3",
                     title="energizingrecorder464 - Life Left Burning", seconds=259)
    assert "saved to /d/in_9.mp3" in p and "Length: 4:19" in p
    assert "send_message_to_guest(file=\"/d/in_9.mp3\")" in p
    assert "can't listen" in p


def _gateway(tmp_path):
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw._media_dir = tmp_path
    gw._admit_chat = AsyncMock(return_value="ok")
    gw._remember_user = MagicMock()
    gw._with_reply = lambda update, prompt: prompt
    gw._send_reply = AsyncMock()
    gw._send_media_to_chat = AsyncMock()
    gw._telegram_log_context = MagicMock(return_value={})
    gw._chat_actions = MagicMock()
    gw._maybe_attach_continue = AsyncMock()
    gw.session = SimpleNamespace(handle_chat=AsyncMock())
    return gw


def _update(**audio_kw):
    downloaded = []

    async def download_to_drive(path):
        Path(path).write_bytes(b"ID3")
        downloaded.append(path)

    audio = SimpleNamespace(file_name="Life Left Burning.mp3", performer="energizingrecorder464",
                            title="Life Left Burning", duration=259, file_size=8_000_000,
                            mime_type="audio/mpeg",
                            get_file=AsyncMock(return_value=SimpleNamespace(
                                download_to_drive=download_to_drive)))
    audio.__dict__.update(audio_kw)
    msg = SimpleNamespace(audio=audio, caption="", message_id=77, reply_text=AsyncMock(),
                          chat=SimpleNamespace(send_action=AsyncMock()))
    return SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=1)), downloaded


@pytest.mark.asyncio
async def test_music_message_is_saved_and_handed_to_her(tmp_path):
    gw = _gateway(tmp_path)
    update, downloaded = _update()
    await gw.on_audio(update, None)
    assert downloaded == [str(tmp_path / "in_1_77.mp3")]
    prompt = gw.session.handle_chat.await_args.args[1]
    assert "audio file — saved to" in prompt and "Life Left Burning" in prompt


@pytest.mark.asyncio
async def test_too_big_file_gets_a_clear_answer_not_silence(tmp_path):
    gw = _gateway(tmp_path)
    update, downloaded = _update(file_size=int(47.6 * 1024 * 1024))
    await gw.on_audio(update, None)
    assert downloaded == []
    gw.session.handle_chat.assert_not_awaited()
    assert "20 MB" in update.message.reply_text.await_args.args[0]


def test_audio_handler_is_registered():
    import inspect

    from ophelia.channels.telegram_bot import TelegramGateway

    src = inspect.getsource(TelegramGateway)
    assert "filters.AUDIO, self._in_background(self.on_audio)" in src
