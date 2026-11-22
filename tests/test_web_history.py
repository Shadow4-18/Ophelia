"""Her browsing history: searches and pages, who they were for, /web."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.mind import web_history as wh

TAVILY = """Web search (Tavily): zerius lunar

Answer: LUNAR is a 2025 track by ZERIUS.
- LUNAR - ZERIUS - YouTube Music
  https://music.youtube.com/watch?v=abcdefghijk
  The song...
- ZERIUS | Spotify
  https://open.spotify.com/artist/xyz
  Listen...
"""
DEEPSEEK = """Web search (DeepSeek): weather

- Forecast (2026-11-22)
  https://weather.example/today
  Sunny.
"""


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, timezone="UTC")


def test_results_are_read_from_each_provider_format():
    got = wh.results_in(TAVILY)
    assert got == [{"title": "LUNAR - ZERIUS - YouTube Music",
                    "url": "https://music.youtube.com/watch?v=abcdefghijk"},
                   {"title": "ZERIUS | Spotify", "url": "https://open.spotify.com/artist/xyz"}]
    assert wh.results_in(DEEPSEEK) == [{"title": "Forecast",
                                        "url": "https://weather.example/today"}]
    assert wh.results_in("Summary: nothing with links") == []


def test_record_load_and_render(tmp_path):
    s = _settings(tmp_path)
    wh.record(s, "search", who="for you", query="zerius lunar", results=wh.results_in(TAVILY))
    wh.record(s, "read", who="on her own", url="https://example.com/x", ok=False)
    wh.record(s, "look", who="for guest Eri", url="https://site.test", note="is it ugly?")
    entries = wh.load(s)
    assert [e["kind"] for e in entries] == ["search", "read", "look"]
    text = wh.render(entries, s)
    assert 'for you: searched "zerius lunar"' in text
    assert "1. LUNAR - ZERIUS - YouTube Music" in text
    assert "https://music.youtube.com/watch?v=abcdefghijk" in text
    assert "on her own: read https://example.com/x (failed)" in text
    assert "for guest Eri: looked at https://site.test" in text and "is it ugly?" in text
    assert wh.recent(s, 2) == entries[-2:]
    assert wh.render([], s).startswith("She hasn't")


@pytest.mark.asyncio
async def test_listeners_hear_each_entry(tmp_path):
    seen = []

    async def listener(entry):
        seen.append(entry["query"])

    wh.subscribe(listener)
    wh.subscribe(listener)  # once only
    try:
        wh.record(_settings(tmp_path), "search", who="for you", query="q")
        await asyncio.sleep(0)
        assert seen == ["q"]
    finally:
        wh.unsubscribe(listener)


def _registry(tmp_path, *, owner=True, friend=False, channel="telegram:1"):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = MagicMock()
    reg.settings.data_dir = tmp_path
    reg.settings.timezone = "UTC"
    reg.memory = MagicMock()
    reg.set_owner(owner)
    reg.set_friend(friend)
    reg._current_sender_channel = channel
    return reg


@pytest.mark.asyncio
async def test_search_and_read_are_recorded_with_who(tmp_path, monkeypatch):
    from ophelia.tools import registry as r

    monkeypatch.setattr(r, "search_web", AsyncMock(return_value=TAVILY))
    monkeypatch.setattr(r, "fetch_url", AsyncMock(return_value="Fetch failed: 404"))
    monkeypatch.setattr("ophelia.memory.guests.get_guest_name", AsyncMock(return_value="Eri"))

    owner = _registry(tmp_path)
    assert await owner._web_search("zerius lunar") == TAVILY
    await owner._fetch_url("https://gone.example")
    guest = _registry(tmp_path, owner=False, channel="telegram:2")
    await guest._web_search("cats")
    friend = _registry(tmp_path, owner=False, friend=True, channel="telegram:3")
    await friend._web_search("dogs")
    own = _registry(tmp_path, channel=None)
    await own._web_search("rabbit hole")

    entries = wh.load(_settings(tmp_path))
    assert [(e["kind"], e["who"]) for e in entries] == [
        ("search", "for you"), ("read", "for you"), ("search", "for guest Eri"),
        ("search", "for friend Eri"), ("search", "on her own")]
    assert entries[0]["results"][0]["url"].startswith("https://music.youtube.com")
    assert entries[1]["ok"] is False


@pytest.mark.asyncio
async def test_her_own_time_counts_even_in_a_chat_context(tmp_path, monkeypatch):
    from ophelia.core.agent_loop import _AUTONOMOUS
    from ophelia.tools import registry as r

    monkeypatch.setattr(r, "search_web", AsyncMock(return_value="no links"))
    reg = _registry(tmp_path)
    token = _AUTONOMOUS.set(True)
    try:
        await reg._web_search("something she wondered about")
    finally:
        _AUTONOMOUS.reset(token)
    assert wh.load(_settings(tmp_path))[-1]["who"] == "on her own"


@pytest.mark.asyncio
async def test_recording_never_breaks_the_tool(tmp_path, monkeypatch):
    from ophelia.tools import registry as r

    monkeypatch.setattr(r, "search_web", AsyncMock(return_value="ok"))
    monkeypatch.setattr(wh, "record", MagicMock(side_effect=RuntimeError("disk full")))
    assert await _registry(tmp_path)._web_search("q") == "ok"


def test_web_command_is_in_the_menu():
    from ophelia.channels.telegram_bot import TelegramGateway

    assert "web" in dict(TelegramGateway.MENU_COMMANDS)


@pytest.mark.asyncio
async def test_web_command(tmp_path):
    from ophelia.channels.telegram_bot import TelegramGateway

    s = _settings(tmp_path)
    for i in range(3):
        wh.record(s, "search", who="for you", query=f"q{i}")
    gw = TelegramGateway.__new__(TelegramGateway)
    gw.settings = MagicMock(data_dir=tmp_path, timezone="UTC")
    gw.settings.is_owner_channel.side_effect = lambda ch: ch == "telegram:1"
    msg = MagicMock(reply_text=AsyncMock(), reply_document=AsyncMock())
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=msg)
    await gw.cmd_web(upd, SimpleNamespace(args=["2"]))
    sent = msg.reply_text.await_args.args[0]
    assert '"q1"' in sent and '"q2"' in sent and '"q0"' not in sent
    await gw.cmd_web(upd, SimpleNamespace(args=["file"]))
    doc = msg.reply_document.await_args.kwargs["document"]
    lines = doc.input_file_content.decode().splitlines()
    assert [json.loads(x)["query"] for x in lines] == ["q0", "q1", "q2"]
    # Not the owner: nothing.
    msg.reply_text.reset_mock()
    await gw.cmd_web(SimpleNamespace(effective_user=SimpleNamespace(id=9), message=msg),
                     SimpleNamespace(args=[]))
    msg.reply_text.assert_not_awaited()
