"""Regression tests for Discord delivery honesty + chat-log gaps.

From live Termux/Discord debugging (2026-07-09):
- consciousness.error: DirectorDecision.urgency_burst_cap missing (fixed in
  director.py; covered in test_director_and_curator.py).
- Chat log / dm-* channels missing mid-turn text because set_message_sender
  used the raw gateway reply, not the logged wrapper.
- Media could be mirrored to dm-* even when Discord upload failed.
- Oversize Discord files were marked delivered without uploading, so tool
  results claimed "sent to the user" falsely.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401


def _session(tmp_path, *, run_turn_text: str = "final answer"):
    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.chat_log_enabled = True
    settings.is_owner_channel.return_value = True
    settings.voice_reply_default = False

    tools = MagicMock()
    tools.begin_turn_artifacts = MagicMock()
    tools.set_message_sender = MagicMock()
    tools.set_media_sender = MagicMock()
    tools.set_owner = MagicMock()
    tools.clear_message_sender = MagicMock()
    tools.clear_media_sender = MagicMock()
    tools.clear_owner = MagicMock()
    tools.consume_pending_artifacts = MagicMock(return_value=[])
    tools._delivered_artifacts = set()
    tools.is_artifact_delivered = MagicMock(return_value=False)
    tools._current_sender_channel = None

    agent = MagicMock()
    agent.settings = settings
    agent.tools = tools
    agent.director = None
    agent.humor = None
    agent.life = None
    agent.run_turn = AsyncMock(return_value=run_turn_text)

    signals = MagicMock()
    signals.set_user_talking = AsyncMock()
    signals.set_agent_thinking = AsyncMock()

    memory = MagicMock()
    memory.save_drives = AsyncMock()
    drives = MagicMock()

    session = ChannelSession(agent, signals, memory, drives)
    session._chat_logger = None
    session._log_hooks = []
    return session, tools, settings


@pytest.mark.asyncio
async def test_handle_chat_logs_mid_turn_message_sender(tmp_path, monkeypatch):
    """Mid-turn send_message / preamble must hit the chat log.

    Previously set_message_sender(reply) wired the raw Discord/Telegram
    reply, so mid-turn text reached the user DM but never dm-* mirrors.
    """
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.chat_log import ChatLogger

    session, tools, settings = _session(tmp_path)
    sent: list[str] = []

    async def gateway_reply(chunk: str) -> None:
        sent.append(chunk)

    await session.handle_chat("discord:420", "hi", gateway_reply)

    # set_message_sender must receive the logged wrapper, not raw gateway_reply.
    assert tools.set_message_sender.called
    mid_turn_sender = tools.set_message_sender.call_args.args[0]
    assert mid_turn_sender is not gateway_reply

    await mid_turn_sender("okay one sec")

    logger = ChatLogger.from_settings(settings)
    rows = await logger.query(direction="out", limit=20)
    texts = [r["text"] or "" for r in rows]
    assert any("okay one sec" in t for t in texts)
    assert any("final answer" in t for t in texts)
    assert "okay one sec" in sent
    assert "final answer" in sent


@pytest.mark.asyncio
async def test_logged_media_skips_mirror_on_failed_upload(tmp_path, monkeypatch):
    """Failed Discord uploads must not appear as successful media in the log."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.chat_log import ChatLogger

    media = tmp_path / "miss.png"
    media.write_bytes(b"\x89PNG")

    session, tools, settings = _session(tmp_path, run_turn_text="done")

    async def gateway_reply(chunk: str) -> None:
        return None

    async def failing_media(path: Path, caption: str) -> bool:
        return False

    await session.handle_chat(
        "discord:420", "hi", gateway_reply, media_reply=failing_media
    )

    assert tools.set_media_sender.called
    logged_media = tools.set_media_sender.call_args.args[0]
    ok = await logged_media(media, "")
    assert ok is False

    logger = ChatLogger.from_settings(settings)
    rows = await logger.query(direction="out", limit=20)
    media_rows = [r for r in rows if r.get("role") == "media"]
    assert media_rows == []


@pytest.mark.asyncio
async def test_discord_oversize_does_not_mark_delivered(tmp_path, monkeypatch):
    """Oversize Discord files must return False without marking delivered."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.channels.discord_bot import DiscordGateway

    path = tmp_path / "huge.png"
    path.write_bytes(b"png")

    tools = MagicMock()
    tools.is_artifact_delivered.return_value = False
    tools._mark_artifact_delivered = MagicMock()

    agent = MagicMock()
    agent.tools = tools

    session = MagicMock()
    session.agent = agent

    gw = DiscordGateway.__new__(DiscordGateway)
    gw.session = session
    gw.settings = MagicMock()

    message = MagicMock()
    message.channel.send = AsyncMock()

    with patch.object(Path, "is_file", return_value=True), patch.object(
        Path, "stat", return_value=MagicMock(st_size=26 * 1024 * 1024)
    ):
        ok = await gw._send_discord_file(message, path, "")

    assert ok is False
    tools._mark_artifact_delivered.assert_not_called()
    message.channel.send.assert_awaited()  # user-facing size warning


@pytest.mark.asyncio
async def test_finalize_media_honest_when_delivery_fails(tmp_path, monkeypatch):
    """Tool result must not claim 'sent to the user' when upload failed."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))

    from ophelia.tools.registry import ToolRegistry

    media = tmp_path / "x.png"
    media.write_bytes(b"png")

    reg = ToolRegistry.__new__(ToolRegistry)
    reg._pending_artifacts = []
    reg._delivered_artifacts = set()
    reg._media_sender = AsyncMock(return_value=False)
    reg.proactive_media_sender = None

    result = await reg._finalize_media_tool_result(
        f"Image saved to {media}", paths=[media]
    )
    assert "sent to the user" not in result.lower()
    assert "pending/failed" in result.lower() or "send_file" in result.lower()
