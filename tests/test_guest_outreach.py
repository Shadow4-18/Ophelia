"""Tests for guest outreach + persistent guest names.

Covers the user's request: "force and/or suggest ophelia to message the other
users in her guest list as well as remember their names."

Scope:
- Guest name precedence: owner-set > self-set > approval display name > channel.
- A guest can name themselves, but not anyone else.
- A guest's self-name is overridden if the owner later sets a name.
- list_guests returns the full roster with resolved names.
- resolve_guest_target handles channel form, bare id, and exact display name.
- guests_context_block excludes the owner and formats nicely.
- /tell relays verbatim (no agent turn).
- /suggest composes via the agent and CCs the owner.
- list_guests tool is owner-only (in GUEST_DENIED_TOOLS); set_guest_name is
  available to guests.
- compose_message stores only the assistant output, not the transient prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import ChannelSession first to fully load the import chain (avoids a circular
# import when importing from ophelia.tools.registry at module top level).
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.tools.registry import GUEST_DENIED_TOOLS


# ── Helpers ───────────────────────────────────────────────────────────────


def _settings_with(monkeypatch, tmp_path, **env):
    from ophelia.config import Settings

    home = tmp_path / "ophelia_home"
    home.mkdir()
    monkeypatch.setenv("OPHELIA_HOME", str(home))
    for k in (
        "OPHELIA_OWNER_ID",
        "OPHELIA_PRIMARY_CHANNEL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_ALLOWED_USERS",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER_IDS",
        "OPHELIA_TELEGRAM_ENABLED",
        "OPHELIA_DISCORD_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


def _seed_approvals(tmp_path, records: dict[str, dict]) -> None:
    """Write a pending_guests.json with the given records keyed by 'platform:id'."""
    data_dir = tmp_path / "ophelia_home" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "pending_guests.json").write_text(
        json.dumps(records), encoding="utf-8"
    )


# ── #1: Guest name precedence ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_name_beats_self_name(monkeypatch, tmp_path):
    from ophelia.memory.guests import get_guest_name, set_guest_name
    from ophelia.memory.store import MemoryStore

    db = tmp_path / "test.db"
    store = MemoryStore(db)
    await store.init()

    # Guest names themselves first
    await set_guest_name(store, "telegram", 222, "Alice", by_owner=False)
    # Owner overrides
    await set_guest_name(store, "telegram", 222, "Bob", by_owner=True)
    name = await get_guest_name(store, "telegram", 222)
    assert name == "Bob"


@pytest.mark.asyncio
async def test_self_name_used_when_no_owner_name(monkeypatch, tmp_path):
    from ophelia.memory.guests import get_guest_name, set_guest_name
    from ophelia.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    await set_guest_name(store, "telegram", 222, "Alice", by_owner=False)
    assert await get_guest_name(store, "telegram", 222) == "Alice"


@pytest.mark.asyncio
async def test_approval_display_name_used_when_no_set_name(monkeypatch, tmp_path):
    from ophelia.memory.guests import get_guest_name
    from ophelia.memory.store import MemoryStore

    home = tmp_path / "ophelia_home"
    data_dir = home / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "pending_guests.json").write_text(
        json.dumps({"telegram:222": {"display_name": "Carol", "status": "approved"}}),
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    name = await get_guest_name(store, "telegram", 222, data_dir=data_dir)
    assert name == "Carol"


@pytest.mark.asyncio
async def test_falls_back_to_channel_when_no_name_anywhere(monkeypatch, tmp_path):
    from ophelia.memory.guests import get_guest_name
    from ophelia.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    name = await get_guest_name(store, "telegram", 999)
    assert name is None


# ── #2: Guest can only name themselves ───────────────────────────────────


@pytest.mark.asyncio
async def test_set_guest_name_store_level_records_self_name():
    """At the store level, set_guest_name(by_owner=False) records a self-name.
    Self-only enforcement (guest can't name others) is the tool handler's job,
    not the store helper's."""
    from ophelia.memory.guests import get_guest_name, set_guest_name
    from ophelia.memory.store import MemoryStore

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "test.db")
        await store.init()
        result = await set_guest_name(
            store, "telegram", 333, "Alice", by_owner=False
        )
        assert "Alice" in result
        assert await get_guest_name(store, "telegram", 333) == "Alice"


@pytest.mark.asyncio
async def test_owner_override_replaces_self_name():
    """When the owner sets a name after the guest self-named, the owner name wins."""
    from ophelia.memory.guests import get_guest_name, set_guest_name
    from ophelia.memory.store import MemoryStore

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "test.db")
        await store.init()
        await set_guest_name(store, "telegram", 333, "Alice", by_owner=False)
        await set_guest_name(store, "telegram", 333, "Bob", by_owner=True)
        assert await get_guest_name(store, "telegram", 333) == "Bob"


@pytest.mark.asyncio
async def test_guest_blocked_when_owner_already_named():
    """If the owner has set a name, a guest's attempt to self-name is refused
    at the store level (returns a message, doesn't overwrite)."""
    from ophelia.memory.guests import get_guest_name, set_guest_name
    from ophelia.memory.store import MemoryStore

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td) / "test.db")
        await store.init()
        await set_guest_name(store, "telegram", 333, "Bob", by_owner=True)
        result = await set_guest_name(store, "telegram", 333, "Hacker", by_owner=False)
        assert "owner" in result.lower() or "ask them" in result.lower()
        assert await get_guest_name(store, "telegram", 333) == "Bob"


# ── #3: list_guests returns full roster ──────────────────────────────────


@pytest.mark.asyncio
async def test_list_guests_returns_roster_with_names(monkeypatch, tmp_path):
    from ophelia.memory.guests import list_guests, set_guest_name
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
        DISCORD_BOT_TOKEN="d",
        DISCORD_ALLOWED_USER_IDS="333",
    )
    _seed_approvals(
        tmp_path,
        {
            "telegram:222": {"display_name": "TG Guest", "status": "approved"},
            "discord:333": {"display_name": "DC Guest", "status": "approved"},
        },
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    await set_guest_name(store, "telegram", 222, "Alice", by_owner=True)

    roster = await list_guests(settings, store)
    channels = {g["channel"] for g in roster}
    assert channels == {"telegram:111", "telegram:222", "discord:333"}

    by_chan = {g["channel"]: g for g in roster}
    assert by_chan["telegram:222"]["name"] == "Alice"
    assert by_chan["telegram:222"]["name_source"] == "owner"
    assert by_chan["discord:333"]["name"] == "DC Guest"
    assert by_chan["discord:333"]["name_source"] == "approval"


# ── #4: resolve_guest_target ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_target_channel_form(monkeypatch, tmp_path):
    from ophelia.memory.guests import resolve_guest_target
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    resolved = await resolve_guest_target(settings, store, "telegram:111")
    assert resolved == ("telegram", 111)


@pytest.mark.asyncio
async def test_resolve_target_bare_numeric_id(monkeypatch, tmp_path):
    from ophelia.memory.guests import resolve_guest_target
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    resolved = await resolve_guest_target(settings, store, "111")
    assert resolved == ("telegram", 111)


@pytest.mark.asyncio
async def test_resolve_target_by_owner_set_name(monkeypatch, tmp_path):
    """Name resolution must check the memory store for owner-set names —
    not just pending_guests.json display names."""
    from ophelia.memory.guests import resolve_guest_target, set_guest_name
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    # Owner sets a name for guest 222
    await set_guest_name(store, "telegram", 222, "Bob", by_owner=True)
    resolved = await resolve_guest_target(settings, store, "Bob")
    assert resolved == ("telegram", 222)


@pytest.mark.asyncio
async def test_resolve_target_by_self_set_name(monkeypatch, tmp_path):
    """Name resolution must also find self-set names in the memory store."""
    from ophelia.memory.guests import resolve_guest_target, set_guest_name
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    await set_guest_name(store, "telegram", 222, "Alice", by_owner=False)
    resolved = await resolve_guest_target(settings, store, "Alice")
    assert resolved == ("telegram", 222)


@pytest.mark.asyncio
async def test_resolve_target_by_approval_display_name(monkeypatch, tmp_path):
    """Name resolution falls back to approval display names when no
    owner/self name is set."""
    from ophelia.memory.guests import resolve_guest_target
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
    )
    _seed_approvals(
        tmp_path,
        {"telegram:222": {"display_name": "Carol", "status": "approved"}},
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    resolved = await resolve_guest_target(settings, store, "Carol")
    assert resolved == ("telegram", 222)


@pytest.mark.asyncio
async def test_resolve_target_unknown_returns_none(monkeypatch, tmp_path):
    from ophelia.memory.guests import resolve_guest_target
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    assert await resolve_guest_target(settings, store, "nobody") is None


# ── #5: guests_context_block ─────────────────────────────────────────────


def test_guests_context_block_excludes_owner():
    from ophelia.memory.guests import guests_context_block

    roster = [
        {"channel": "telegram:111", "name": "Owner", "name_source": "self", "last_ts": None},
        {"channel": "telegram:222", "name": "Alice", "name_source": "owner", "last_ts": None},
        {"channel": "discord:333", "name": "Bob", "name_source": "approval", "last_ts": None},
    ]
    block = guests_context_block(roster, owner_channel="telegram:111")
    assert "telegram:111" not in block  # owner excluded
    assert "Alice" in block
    assert "Bob" in block
    assert block.startswith("# Guests you know")


def test_guests_context_block_empty_when_only_owner():
    from ophelia.memory.guests import guests_context_block

    roster = [{"channel": "telegram:111", "name": "Owner", "name_source": "self", "last_ts": None}]
    block = guests_context_block(roster, owner_channel="telegram:111")
    assert block == ""


# ── #6: Tool gating ──────────────────────────────────────────────────────


def test_list_guests_denied_for_guests():
    """list_guests is owner-only — guests can't enumerate the roster."""
    assert "list_guests" in GUEST_DENIED_TOOLS


def test_set_guest_name_allowed_for_guests():
    """Guests CAN set their own name (enforced in the handler to be self-only)."""
    assert "set_guest_name" not in GUEST_DENIED_TOOLS


# ── #7: /tell relays verbatim ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tell_relays_exact_message(monkeypatch, tmp_path):
    from ophelia.channels.session import ChannelSession
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
    )
    _seed_approvals(
        tmp_path,
        {"telegram:222": {"display_name": "Alice", "status": "approved"}},
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    agent = MagicMock()
    agent.settings = settings
    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session.memory = store
    session.hub = None

    sent: list[tuple] = []

    async def _send(platform, uid, msg):
        sent.append((platform, uid, msg))
        return True

    replies: list[str] = []

    async def _reply(t):
        replies.append(t)

    await session.cmd_tell(["Alice", "hi there"], _reply, send_to_guest=_send)
    assert sent == [("telegram", 222, "hi there")]
    assert any("Sent to" in r for r in replies)


@pytest.mark.asyncio
async def test_tell_unknown_guest_errors(monkeypatch, tmp_path):
    from ophelia.channels.session import ChannelSession
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111",
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    agent = MagicMock()
    agent.settings = settings
    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session.memory = store
    session.hub = None

    sent: list[tuple] = []

    async def _send(platform, uid, msg):
        sent.append((platform, uid, msg))
        return True

    replies: list[str] = []

    async def _reply(t):
        replies.append(t)

    await session.cmd_tell(["nobody", "hi"], _reply, send_to_guest=_send)
    assert sent == []  # nothing sent
    assert any("Couldn't resolve" in r for r in replies)


@pytest.mark.asyncio
async def test_tell_no_args_shows_usage(monkeypatch, tmp_path):
    from ophelia.channels.session import ChannelSession
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN="t", TELEGRAM_ALLOWED_USER_IDS="111"
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()
    agent = MagicMock()
    agent.settings = settings
    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session.memory = store
    session.hub = None

    async def _send(*a):
        return True

    replies: list[str] = []

    async def _reply(t):
        replies.append(t)

    await session.cmd_tell([], _reply, send_to_guest=_send)
    assert any("Usage" in r for r in replies)


# ── #8: /suggest composes and CCs owner ──────────────────────────────────


@pytest.mark.asyncio
async def test_suggest_composes_and_ccs_owner(monkeypatch, tmp_path):
    from ophelia.channels.session import ChannelSession
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
    )
    _seed_approvals(
        tmp_path,
        {"telegram:222": {"display_name": "Alice", "status": "approved"}},
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    agent = MagicMock()
    agent.settings = settings
    agent.compose_message = AsyncMock(return_value="Hey Alice, how's it going?")

    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session.memory = store
    session.hub = None

    sent: list[tuple] = []

    async def _send(platform, uid, msg):
        sent.append((platform, uid, msg))
        return True

    replies: list[str] = []

    async def _reply(t):
        replies.append(t)

    await session.cmd_suggest(["Alice", "check in on her"], _reply, send_to_guest=_send)
    assert sent == [("telegram", 222, "Hey Alice, how's it going?")]
    # Owner gets CC'd
    assert any("Hey Alice" in r for r in replies)
    agent.compose_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_suggest_empty_composition_errors(monkeypatch, tmp_path):
    from ophelia.channels.session import ChannelSession
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111,222",
    )
    _seed_approvals(
        tmp_path,
        {"telegram:222": {"display_name": "Alice", "status": "approved"}},
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    agent = MagicMock()
    agent.settings = settings
    agent.compose_message = AsyncMock(return_value="")

    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session.memory = store
    session.hub = None

    async def _send(*a):
        return True

    replies: list[str] = []

    async def _reply(t):
        replies.append(t)

    await session.cmd_suggest(["Alice", "topic"], _reply, send_to_guest=_send)
    assert any("didn't produce" in r.lower() for r in replies)


# ── #9: set_guest_name tool handler enforces self-only for guests ────────


@pytest.mark.asyncio
async def test_set_guest_name_owner_can_name_anyone(tmp_path):
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.is_owner_channel.return_value = True
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = store
    reg._is_owner = True
    reg._current_sender_channel = "telegram:111"

    result = await reg._set_guest_name("telegram", 222, "Alice")
    assert "Alice" in result
    assert "owner" in result.lower()


@pytest.mark.asyncio
async def test_set_guest_name_guest_can_name_self(tmp_path):
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = store
    reg._is_owner = False
    reg._current_sender_channel = "telegram:222"

    result = await reg._set_guest_name("telegram", 222, "MyName")
    assert "MyName" in result


@pytest.mark.asyncio
async def test_set_guest_name_guest_cannot_name_other(tmp_path):
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = store
    reg._is_owner = False
    reg._current_sender_channel = "telegram:222"

    result = await reg._set_guest_name("telegram", 333, "NotMe")
    assert "only set your own name" in result.lower()


# ── #10: compose_message stores assistant output but not the prompt ──────


@pytest.mark.asyncio
async def test_compose_message_stores_only_assistant_output(tmp_path, monkeypatch):
    """compose_message must NOT store the transient prompt as a user message
    in the guest's thread — only the resulting assistant message."""
    from ophelia.core.agent_loop import AgentLoop

    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path / "ophelia_home"))
    from ophelia.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = store
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = {"telegram:111"}
    agent.honcho = None
    agent._memory_entries = []
    agent._user_entries = []
    agent.body_status = ""
    agent.drives = MagicMock()
    agent.drives.to_context_block.return_value = ""
    agent.psyche = MagicMock()
    agent.psyche.to_context_block.return_value = ""
    agent.life = None
    agent.humor = None
    agent.tools = MagicMock()

    # Stub _build_messages to return a minimal message list
    agent._build_messages = AsyncMock(return_value=[{"role": "user", "content": "x"}])
    # Stub _complete to return a canned response and store it
    async def _fake_complete(messages, *, store_channel, role, is_owner):
        await store.append_guest_message(store_channel, "assistant", "Outbound msg")
        return "Outbound msg"

    agent._complete = _fake_complete

    result = await agent.compose_message(
        "telegram:222", "owner nudge", is_owner=False
    )
    assert result == "Outbound msg"
    # The prompt must NOT be in the guest's history
    guest_hist = await store.recent_guest("telegram:222", limit=10)
    roles = [m["role"] for m in guest_hist]
    contents = [m["content"] for m in guest_hist]
    assert "user" not in roles or "owner nudge" not in contents
    assert "Outbound msg" in contents


# ── #11: Tool definitions exist ──────────────────────────────────────────


def test_list_guests_tool_definition_exists():
    from ophelia.tools.registry import TOOL_DEFINITIONS

    names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "list_guests" in names
    assert "set_guest_name" in names


def test_set_guest_name_tool_parameters():
    from ophelia.tools.registry import TOOL_DEFINITIONS

    tool = next(
        t for t in TOOL_DEFINITIONS if t["function"]["name"] == "set_guest_name"
    )
    params = tool["function"]["parameters"]["properties"]
    assert set(params.keys()) == {"platform", "user_id", "name"}
    assert tool["function"]["parameters"]["required"] == ["platform", "user_id", "name"]


# ── #12: send_message_to_guest tool (natural-language messaging) ─────────


def test_send_message_to_guest_tool_definition_exists():
    from ophelia.tools.registry import TOOL_DEFINITIONS

    names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "send_message_to_guest" in names


def test_send_message_to_guest_denied_for_guests():
    """send_message_to_guest is owner-only — guests can't message other guests."""
    from ophelia.tools.registry import GUEST_DENIED_TOOLS

    assert "send_message_to_guest" in GUEST_DENIED_TOOLS


@pytest.mark.asyncio
async def test_send_message_to_guest_calls_guest_sender(tmp_path):
    """The tool must call guest_sender(platform, user_id, message) and report success."""
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.is_owner_channel.return_value = False  # telegram:222 is a guest, not the owner
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = store
    reg._is_owner = True

    sent: list[tuple] = []

    async def _guest_sender(platform, uid, msg):
        sent.append((platform, uid, msg))
        return True

    reg.guest_sender = _guest_sender

    result = await reg._send_message_to_guest("telegram", 222, "hey Bob")
    assert "Sent to" in result
    assert sent == [("telegram", 222, "hey Bob")]


@pytest.mark.asyncio
async def test_send_message_to_guest_reports_failure(tmp_path):
    """When guest_sender returns False, the tool must report the failure clearly."""
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.is_owner_channel.return_value = False  # telegram:222 is a guest, not the owner
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = store
    reg._is_owner = True

    async def _fail_sender(platform, uid, msg):
        return False

    reg.guest_sender = _fail_sender

    result = await reg._send_message_to_guest("telegram", 222, "hey")
    assert "Failed" in result
    assert "/start" in result  # Telegram-specific hint


@pytest.mark.asyncio
async def test_send_message_to_guest_no_sender_wired(tmp_path):
    """When no guest_sender is wired (e.g. CLI mode), report gracefully."""
    from ophelia.memory.store import MemoryStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.is_owner_channel.return_value = False  # telegram:222 is a guest
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.memory = store
    reg._is_owner = True
    reg.guest_sender = None

    result = await reg._send_message_to_guest("telegram", 222, "hey")
    assert "no cross-platform sender" in result.lower()


# ── #13: Cross-platform hub routing ─────────────────────────────────────


@pytest.mark.asyncio
async def test_hub_send_to_user_routes_to_correct_gateway():
    """ChannelHub.send_to_user must find the gateway matching the platform
    and delegate to its send_to_user method."""
    from ophelia.channels.hub import ChannelHub

    hub = ChannelHub.__new__(ChannelHub)

    tg = MagicMock()
    tg.platform = "telegram"
    tg.is_configured = MagicMock(return_value=True)
    tg.send_to_user = AsyncMock(return_value=True)

    dc = MagicMock()
    dc.platform = "discord"
    dc.is_configured = MagicMock(return_value=True)
    dc.send_to_user = AsyncMock(return_value=True)

    hub._gateways = [tg, dc]

    # Telegram target → telegram gateway
    await hub.send_to_user("telegram", 111, "hi")
    tg.send_to_user.assert_awaited_once_with(111, "hi")
    dc.send_to_user.assert_not_awaited()

    # Discord target → discord gateway
    await hub.send_to_user("discord", 222, "hi")
    dc.send_to_user.assert_awaited_once_with(222, "hi")


@pytest.mark.asyncio
async def test_hub_send_to_user_no_matching_gateway():
    """Returns False when no gateway matches the requested platform."""
    from ophelia.channels.hub import ChannelHub

    hub = ChannelHub.__new__(ChannelHub)
    tg = MagicMock()
    tg.platform = "telegram"
    tg.is_configured = MagicMock(return_value=True)
    hub._gateways = [tg]

    result = await hub.send_to_user("discord", 222, "hi")
    assert result is False


@pytest.mark.asyncio
async def test_cmd_tell_uses_hub_for_cross_platform(monkeypatch, tmp_path):
    """When the session has a hub, /tell must route through it so a Telegram
    owner can message a Discord guest (and vice versa)."""
    from ophelia.channels.session import ChannelSession
    from ophelia.memory.store import MemoryStore

    settings = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_ALLOWED_USER_IDS="111",
        DISCORD_BOT_TOKEN="d",
        DISCORD_ALLOWED_USER_IDS="222",
    )
    _seed_approvals(
        tmp_path,
        {"discord:222": {"display_name": "Bob", "status": "approved"}},
    )
    store = MemoryStore(tmp_path / "test.db")
    await store.init()

    agent = MagicMock()
    agent.settings = settings
    session = ChannelSession.__new__(ChannelSession)
    session.agent = agent
    session.memory = store
    session.hub = None
    session.hub = MagicMock()
    session.hub.send_to_user = AsyncMock(return_value=True)

    replies: list[str] = []

    async def _reply(t):
        replies.append(t)

    # Owner on Telegram messages a Discord guest by display name
    await session.cmd_tell(["Bob", "hi from TG"], _reply, send_to_guest=AsyncMock(return_value=False))
    session.hub.send_to_user.assert_awaited_once_with("discord", 222, "hi from TG")
    assert any("Sent to discord:222" in r for r in replies)
