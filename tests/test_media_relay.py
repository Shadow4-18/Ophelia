"""Bidirectional media relay: owner↔guest images/videos."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_send_media_to_guest(tmp_path: Path, monkeypatch):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.tools.registry import ToolRegistry

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    settings = Settings()
    # Point data_dir at tmp so sandbox accepts the file.
    object.__setattr__(settings, "data_dir", tmp_path)
    art = tmp_path / "artifacts"
    art.mkdir()
    img = art / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    sent: list[tuple] = []

    async def fake_media(platform, user_id, path, caption=""):
        sent.append((platform, user_id, Path(path), caption))
        return True

    reg = ToolRegistry(settings, art)
    reg._is_owner = True
    reg.guest_media_sender = fake_media
    out = await reg._send_message_to_guest(
        "telegram", 222, message="for you", file=str(img)
    )
    assert "media" in out.lower()
    assert len(sent) == 1
    assert sent[0][0] == "telegram"
    assert sent[0][1] == 222
    assert sent[0][2] == img.resolve()
    assert sent[0][3] == "for you"


@pytest.mark.asyncio
async def test_relay_media_to_owner(tmp_path: Path, monkeypatch):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    settings = Settings()
    object.__setattr__(settings, "data_dir", tmp_path)
    media = tmp_path / "telegram_media"
    media.mkdir()
    img = media / "in_1.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)

    texts: list[str] = []
    files: list[tuple] = []

    async def fake_text(msg, **kwargs):
        texts.append(msg)

    async def fake_media(path, caption=""):
        files.append((Path(path), caption))
        return True

    store = MemoryStore(tmp_path / "m.db")
    await store.init()
    await store.set_fact("guest_name_owner:telegram:222", "Eri")

    reg = ToolRegistry(settings, tmp_path / "art", memory=store)
    reg._is_owner = False
    reg._current_sender_channel = "telegram:222"
    reg.proactive_sender = fake_text
    reg.proactive_media_sender = fake_media

    out = await reg._relay_to_owner(message="look at this", file=str(img))
    assert "Delivered" in out
    assert texts and "Eri" in texts[0]
    assert files and files[0][0] == img.resolve()


@pytest.mark.asyncio
async def test_refuse_path_outside_data_dir(tmp_path: Path, monkeypatch):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.tools.registry import ToolRegistry

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    settings = Settings()
    object.__setattr__(settings, "data_dir", tmp_path / "ophelia_data")
    (tmp_path / "ophelia_data").mkdir()
    outside = tmp_path / "evil.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    reg = ToolRegistry(settings, tmp_path / "art")
    reg._is_owner = True
    reg.guest_media_sender = AsyncMock(return_value=True)
    out = await reg._send_message_to_guest(
        "telegram", 222, file=str(outside)
    )
    assert "refusing" in out.lower() or "Can't send file" in out
    reg.guest_media_sender.assert_not_called()


def test_hub_has_send_file_to_user():
    from ophelia.channels.hub import ChannelHub

    assert hasattr(ChannelHub, "send_file_to_user")
