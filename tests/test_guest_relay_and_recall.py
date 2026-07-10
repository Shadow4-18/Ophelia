"""Guest→owner relay and owner recall of guest chat history."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def test_relay_to_owner_tool_exists_and_allowed_for_guests():
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.tools.registry import GUEST_DENIED_TOOLS, TOOL_DEFINITIONS

    names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    assert "relay_to_owner" in names
    assert "relay_to_owner" not in GUEST_DENIED_TOOLS


def test_recall_guest_chat_owner_only():
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.tools.registry import GUEST_DENIED_TOOLS, TOOL_DEFINITIONS

    names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    assert "recall_guest_chat" in names
    assert "recall_guest_chat" in GUEST_DENIED_TOOLS


@pytest.mark.asyncio
async def test_search_guest_messages(tmp_path: Path):
    from ophelia.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "m.db")
    await store.init()
    await store.append_guest_message("telegram:222", "user", "tell Okia he smells")
    await store.append_guest_message("telegram:222", "assistant", "I'll pass that along")
    await store.append_guest_message("telegram:333", "user", "unrelated")

    hits = await store.search_guest_messages("smells", channel="telegram:222")
    assert len(hits) == 1
    assert "smells" in hits[0]["content"]

    recent = await store.search_guest_messages("", channel="telegram:222", limit=10)
    assert len(recent) == 2


@pytest.mark.asyncio
async def test_relay_to_owner_delivers(tmp_path: Path, monkeypatch):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    settings = Settings()
    store = MemoryStore(tmp_path / "m.db")
    await store.init()
    await store.set_fact("guest_name_owner:telegram:222", "Eri")

    sent: list[str] = []

    async def fake_proactive(text: str, **kwargs):
        sent.append(text)

    reg = ToolRegistry(settings, tmp_path / "art", memory=store)
    reg.proactive_sender = fake_proactive
    reg._is_owner = False
    reg._current_sender_channel = "telegram:222"

    result = await reg._relay_to_owner("tell Okia he smells")
    assert "Delivered" in result
    assert len(sent) == 1
    assert "Eri" in sent[0]
    assert "smells" in sent[0]

    hits = await store.search_messages("relay from Eri")
    assert hits


@pytest.mark.asyncio
async def test_relay_to_owner_refuses_when_owner(tmp_path: Path):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.tools.registry import ToolRegistry

    settings = Settings()
    reg = ToolRegistry(settings, tmp_path / "art")
    reg._is_owner = True
    reg.proactive_sender = AsyncMock()
    out = await reg._relay_to_owner("hi")
    assert "already talking to the owner" in out.lower()
    reg.proactive_sender.assert_not_called()


@pytest.mark.asyncio
async def test_recall_guest_chat_returns_history(tmp_path: Path, monkeypatch):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111,222")
    settings = Settings()
    store = MemoryStore(tmp_path / "m.db")
    await store.init()
    await store.set_fact("guest_name_owner:telegram:222", "Eri")
    await store.append_guest_message("telegram:222", "user", "Oi tell Okia he smells")
    await store.append_guest_message(
        "telegram:222", "assistant", "Okay I'll tell him"
    )

    reg = ToolRegistry(settings, tmp_path / "art", memory=store)
    reg._is_owner = True
    out = await reg._recall_guest_chat("Eri", query="smells")
    assert "GUEST:" in out
    assert "smells" in out
    assert "Eri" in out


@pytest.mark.asyncio
async def test_recall_guest_chat_denied_for_guest(tmp_path: Path):
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.config import Settings
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = Settings()
    reg = ToolRegistry(
        settings, tmp_path / "art", memory=MemoryStore(tmp_path / "m.db")
    )
    reg._is_owner = False
    out = await reg._recall_guest_chat("Eri")
    assert "Only the owner" in out


def test_guest_prompt_mentions_relay():
    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "core" / "agent_loop.py"
    body = src.read_text(encoding="utf-8")
    assert "relay_to_owner" in body
    assert "RELAY TO OWNER" in body
