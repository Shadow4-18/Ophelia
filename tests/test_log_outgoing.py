"""Tests for logging outgoing messages that bypass handle_chat.

The chat log (ChatLogger / chat_log.db) is the logging server's data
source. Originally only messages that flowed through handle_chat's
_logged_reply / _logged_media wrappers were recorded — so proactive
messages (consciousness ticks), guest DMs sent via send_message_to_guest,
spontaneous voice notes, and proactive media were all invisible to the
logging server even though they showed in the termux log.

session.log_outgoing() closes that gap by writing directly to the
ChatLogger for any outbound send that didn't come from a handle_chat
reply. These tests verify the helper writes the right row and that
chat_log_enabled=False disables it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure full import chain loads (avoid circular imports).
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_log_outgoing_writes_to_chat_log(tmp_path, monkeypatch):
    """log_outgoing should insert an 'out' row in chat_log.db."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.chat_log import ChatLogger
    from ophelia.channels.session import ChannelSession

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.chat_log_enabled = True
    settings.is_owner_channel.return_value = True  # owner channel

    agent = MagicMock()
    agent.settings = settings

    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session._chat_logger = None
    session._log_hooks = []

    await session.log_outgoing(
        channel="telegram:111",
        text="hey, thought you'd want to see this",
    )

    logger = ChatLogger.from_settings(settings)
    rows = await logger.query(direction="out", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == "out"
    assert row["channel"] == "telegram:111"
    assert "thought you'd want to see" in (row["text"] or "")
    assert row["role"] == "assistant"
    assert row["is_owner"] == 1


@pytest.mark.asyncio
async def test_log_outgoing_records_media(tmp_path, monkeypatch):
    """log_outgoing should record media_path + media_kind for media sends."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.chat_log import ChatLogger
    from ophelia.channels.session import ChannelSession

    # Create a fake media file so the logger copies it.
    media = tmp_path / "gen.png"
    media.write_bytes(b"\x89PNG fake")

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.chat_log_enabled = True
    settings.is_owner_channel.return_value = False  # guest channel

    agent = MagicMock()
    agent.settings = settings

    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session._chat_logger = None
    session._log_hooks = []

    await session.log_outgoing(
        channel="discord:222",
        text="[media sent: gen.png]",
        media_path=media,
        media_kind="photo",
        role="media",
    )

    logger = ChatLogger.from_settings(settings)
    rows = await logger.query(direction="out", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["media_path"] is not None
    assert row["media_kind"] == "photo"
    assert row["role"] == "media"
    assert row["is_owner"] == 0  # guest channel


@pytest.mark.asyncio
async def test_log_outgoing_respects_chat_log_disabled(tmp_path, monkeypatch):
    """When chat_log_enabled is False, log_outgoing should be a no-op."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.chat_log import ChatLogger
    from ophelia.channels.session import ChannelSession

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.chat_log_enabled = False
    settings.is_owner_channel.return_value = True

    agent = MagicMock()
    agent.settings = settings

    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session._chat_logger = None
    session._log_hooks = []

    await session.log_outgoing(channel="telegram:111", text="should not log")

    logger = ChatLogger.from_settings(settings)
    rows = await logger.query(direction="out", limit=10)
    assert rows == []


@pytest.mark.asyncio
async def test_log_outgoing_fires_log_hooks(tmp_path, monkeypatch):
    """log_outgoing should fire log hooks so the Discord/Telegram log
    viewers (which subscribe via add_log_hook) see proactive sends too."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.session import ChannelSession

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.chat_log_enabled = True
    settings.is_owner_channel.return_value = True

    agent = MagicMock()
    agent.settings = settings

    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session._chat_logger = None
    session._log_hooks = []

    seen: list[dict] = []

    async def hook(entry: dict) -> None:
        seen.append(entry)

    session.add_log_hook(hook)

    await session.log_outgoing(channel="telegram:111", text="proactive nudge")

    assert len(seen) == 1
    assert seen[0]["direction"] == "out"
    assert seen[0]["text"] == "proactive nudge"
