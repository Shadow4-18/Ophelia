"""Friends: guests with a few more tools, scoped to their own things, and
nothing of theirs moves when they're promoted or demoted."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels import friends
from ophelia.memory.store import MemoryStore
from ophelia.tools.registry import ToolRegistry

OWNER = "telegram:1"
ERI = "telegram:2"
BOB = "telegram:3"


def _settings(tmp_path, friend_ids=""):
    s = MagicMock()
    s.data_dir = tmp_path
    s.friend_ids = friend_ids
    s.is_owner_channel.side_effect = lambda ch: ch == OWNER
    s.owner_channels.return_value = {OWNER}
    return s


async def _memory(tmp_path):
    m = MemoryStore(tmp_path / "m.db")
    await m.init()
    return m


def _registry(settings, memory, *, channel=ERI, owner=False, friend=True):
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._mcp_ready = True
    reg.mcp = MagicMock()
    reg.mcp.dispatch = AsyncMock(return_value=None)
    reg.settings = settings
    reg.memory = memory
    reg.episodes = None
    reg._handlers = {
        "recall_memory": reg._recall_memory,
        "edit_soul": AsyncMock(return_value="soul edited"),
        "phone_tap": AsyncMock(return_value="tapped"),
        "phone_see_screen": AsyncMock(return_value="screen"),
        "follow_up_done": reg._follow_up_done,
    }
    reg.set_owner(owner)
    reg.set_friend(friend)
    reg._current_sender_channel = channel
    return reg


@pytest.mark.asyncio
async def test_who_is_a_friend(tmp_path):
    mem = await _memory(tmp_path)
    s = _settings(tmp_path, friend_ids="discord:9")
    assert await friends.is_friend(s, mem, "discord:9")
    assert not await friends.is_friend(s, mem, ERI)
    await friends.set_friend(mem, ERI, True)
    assert await friends.is_friend(s, mem, ERI)
    assert not await friends.is_friend(s, mem, OWNER)  # the owner is never "a friend"
    await friends.set_friend(mem, ERI, False)
    assert not await friends.is_friend(s, mem, ERI)


@pytest.mark.asyncio
async def test_friend_gets_the_extras_and_nothing_else(tmp_path):
    mem = await _memory(tmp_path)
    reg = _registry(_settings(tmp_path), mem)
    assert "screen" == await reg.dispatch("phone_see_screen", "{}")
    assert "owner-only" in await reg.dispatch("phone_tap", "{}")
    assert "owner-only" in await reg.dispatch("edit_soul", "{}")
    reg.set_friend(False)  # a plain guest
    assert "owner-only" in await reg.dispatch("phone_see_screen", "{}")
    assert "owner-only" in await reg.dispatch("recall_memory", '{"query": "x"}')


@pytest.mark.asyncio
async def test_friend_tool_list(tmp_path, monkeypatch):
    from ophelia.tools import registry as r

    names = ("web_search", "recall_memory", "phone_see_screen", "phone_tap",
             "phone_notifications", "edit_soul", "sqlite_exec", "list_guests")
    monkeypatch.setattr(r, "all_tool_definitions",
                        lambda settings, stack=None: [{"function": {"name": n}} for n in names])
    reg = _registry(_settings(tmp_path), None)
    got = {t["function"]["name"] for t in await reg.tool_definitions(for_guest=True)}
    assert got == {"web_search", "recall_memory", "phone_see_screen"}


@pytest.mark.asyncio
async def test_friend_recall_is_only_their_own_chat(tmp_path):
    mem = await _memory(tmp_path)
    await mem.append_guest_message(ERI, "user", "my cat is called Mochi")
    await mem.append_guest_message(BOB, "user", "my cat is called Secret")
    await mem.append_message(OWNER, "user", "my cat is called Private")
    reg = _registry(_settings(tmp_path), mem)
    out = await reg.dispatch("recall_memory", '{"query": "cat"}')
    assert "Mochi" in out and "Secret" not in out and "Private" not in out


@pytest.mark.asyncio
async def test_friend_sees_only_files_they_sent(tmp_path):
    media = tmp_path / "telegram_media"
    media.mkdir()
    for n in ("in_2_10.jpg", "in_3_11.jpg", "in_1_12.jpg", "in_13.jpg"):
        (media / n).write_bytes(b"x")
    reg = _registry(_settings(tmp_path), None)
    out = await reg._list_inbox_images()
    assert "in_2_10.jpg" in out
    assert not any(n in out for n in ("in_3_11", "in_1_12", "in_13"))
    reg.set_owner(True)
    out = await reg._list_inbox_images()
    assert all(n in out for n in ("in_2_10", "in_3_11", "in_1_12", "in_13"))


@pytest.mark.asyncio
async def test_friend_cant_close_someone_elses_follow_up(tmp_path):
    from ophelia.mind.open_loops import OpenLoops

    mem = await _memory(tmp_path)
    loops = OpenLoops(mem.db_path)
    his = await loops.add(OWNER, "ask about the dentist", due_in_hours=1)
    hers = await loops.add(ERI, "ask how the exam went", due_in_hours=1)
    reg = _registry(_settings(tmp_path), mem)
    assert "No open follow-up" in await reg.dispatch("follow_up_done", json.dumps({"id": his}))
    assert "closed" in await reg.dispatch("follow_up_done", json.dumps({"id": hers}))


def test_her_day_for_a_friend_leaves_out_the_details(tmp_path):
    from ophelia.mind import day_log

    s = SimpleNamespace(data_dir=tmp_path, timezone="UTC", owner_timezone="UTC")
    day_log.record(s, "image", "made {n} image{s} because your owner asked",
                   detail="something private")
    day_log.record_chat(s, "your owner", "secret opener")
    out = day_log.shareable_day(s, "today")
    assert "made 1 image" in out and "talked with your owner" in out
    assert "something private" not in out and "secret opener" not in out


@pytest.mark.asyncio
async def test_promoting_moves_nothing(tmp_path):
    """Her history, name and notes with a guest are all still there after
    they become a friend (and after they go back to being a guest)."""
    from ophelia.tools.guest_tools import GuestToolsMixin  # noqa: F401

    mem = await _memory(tmp_path)
    await mem.append_guest_message(ERI, "user", "hi it's eri")
    await mem.set_fact(f"guest_rapport:{ERI}", "likes cats")
    s = _settings(tmp_path)
    reg = _registry(s, mem, channel=OWNER, owner=True, friend=False)
    import ophelia.memory.guests as g

    async def resolve(settings, memory, q):
        return ("telegram", 2)

    async def name(*a, **k):
        return "Eri"

    g_resolve, g_name = g.resolve_guest_target, g.get_guest_name
    g.resolve_guest_target, g.get_guest_name = resolve, name
    try:
        assert "is a friend now" in await reg._set_friend("Eri", True)
        assert await friends.is_friend(s, mem, ERI)
        assert [m["content"] for m in await mem.recent_guest(ERI)] == ["hi it's eri"]
        assert await mem.get_fact(f"guest_rapport:{ERI}") == "likes cats"
        assert "back to being a guest" in await reg._set_friend("Eri", False)
        assert [m["content"] for m in await mem.recent_guest(ERI)] == ["hi it's eri"]
        reg.set_owner(False)
        assert "Only your owner" in await reg._set_friend("Bob", True)
    finally:
        g.resolve_guest_target, g.get_guest_name = g_resolve, g_name


@pytest.mark.asyncio
async def test_friend_prompt_and_continuity(tmp_path, monkeypatch):
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.mind.open_loops import OpenLoops
    from ophelia.mind.people import PeopleBook

    mem = await _memory(tmp_path)
    s = _settings(tmp_path)
    agent = AgentLoop.__new__(AgentLoop)
    agent.settings, agent.memory = s, mem
    agent._identity_block = lambda ch, is_owner: ""
    agent._owner_channel_label = lambda ch: "Telegram"
    monkeypatch.setattr("ophelia.core.agent_loop.load_soul", lambda: "soul")
    monkeypatch.setattr("ophelia.memory.guests.get_guest_name",
                        AsyncMock(return_value="Eri"))
    await PeopleBook(mem.db_path).remember("Eri", "has an exam friday")
    await OpenLoops(mem.db_path).add(ERI, "ask how the exam went", due_in_hours=1)

    plain = await agent._guest_system_prompt("", ERI)
    assert "Friend mode" not in plain and "exam" not in plain
    await friends.set_friend(mem, ERI, True)
    text = await agent._guest_system_prompt("", ERI)
    assert "Friend mode" in text and "Guest mode" in text
    assert "has an exam friday" in text and "ask how the exam went" in text
    assert "never tap" in text


@pytest.mark.asyncio
async def test_friends_get_owner_media_and_guests_stay_clamped(tmp_path, monkeypatch):
    from ophelia.tools import registry as r

    seen = {}

    async def fake_video(settings, stack, prompt, **kw):
        seen.update(kw)
        return "Video saved to /x.mp4"

    monkeypatch.setattr(r, "generate_video", fake_video)
    reg = _registry(_settings(tmp_path), None)
    reg.stack = None
    reg.artifacts_dir = tmp_path
    reg._finalize_media_tool_result = AsyncMock(side_effect=lambda t, **k: t)
    await reg._generate_video("a clip", aspect_ratio="16:9", resolution="720p")
    assert (seen["aspect_ratio"], seen["resolution"]) == ("16:9", "720p")
    reg.set_friend(False)
    await reg._generate_video("a clip", aspect_ratio="16:9", resolution="720p")
    assert (seen["aspect_ratio"], seen["resolution"]) == ("1:1", "480p")

    captured = {}
    from ophelia.providers import image_routing

    real = image_routing.prepare_job

    def spy(settings, job, *, is_owner):
        captured["full"] = is_owner
        return real(settings, job, is_owner=is_owner)

    monkeypatch.setattr(image_routing, "prepare_job", spy)
    monkeypatch.setattr(r, "run_image_job", AsyncMock(return_value="refused"))
    for friend, want in ((True, True), (False, False)):
        reg.set_friend(friend)
        await reg._generate_image("a cat")
        assert captured["full"] is want


def test_owner_level_image_job_for_friends():
    from ophelia.providers.image_job import ImageJob
    from ophelia.providers.image_routing import prepare_job

    s = SimpleNamespace(image_nsfw_allowed=True)
    job = ImageJob(prompt="explicit sex, nude woman", aspect_ratio="16:9", count=3,
                   attempts=2)
    full = prepare_job(s, job, is_owner=True)  # owner or friend
    assert (full.aspect_ratio, full.count, full.attempts) == ("16:9", 3, 2)
    assert full.nsfw and full.fallback_mature
    clamped = prepare_job(s, job, is_owner=False)
    assert (clamped.aspect_ratio, clamped.count, clamped.attempts) == ("1:1", 1, 1)


@pytest.mark.asyncio
async def test_guests_and_friends_cant_be_sent_private_files(tmp_path):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    (arts / "img.png").write_bytes(b"x")
    (tmp_path / ".env").write_text("XAI_API_KEY=secret")
    media = tmp_path / "telegram_media"
    media.mkdir()
    (media / "in_2_5.jpg").write_bytes(b"x")
    (media / "in_3_6.jpg").write_bytes(b"x")
    reg = _registry(_settings(tmp_path), None)
    reg.artifacts_dir = arts
    sent = []

    async def deliver(p, caption):
        sent.append(p.name)
        return True

    reg._deliver_artifact = deliver
    for name in (".env", "telegram_media/in_3_6.jpg"):
        assert "isn't one you can send" in await reg._send_file(str(tmp_path / name))
    assert "Sent" in await reg._send_file(str(arts / "img.png"))
    assert "Sent" in await reg._send_file(str(media / "in_2_5.jpg"))
    reg.set_owner(True)
    assert "Sent" in await reg._send_file(str(tmp_path / ".env"))  # the owner's call
    assert sent == ["img.png", "in_2_5.jpg", ".env"]
