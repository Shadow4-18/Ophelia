"""Daily caps on images, videos and voice notes for plain guests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels import guest_caps
from ophelia.tools.registry import ToolRegistry

GUEST = "telegram:2"


def _settings(tmp_path, images=2, videos=0, voice=-1):
    return SimpleNamespace(data_dir=tmp_path, timezone="UTC", owner_timezone="UTC",
                           guest_daily_images=images, guest_daily_videos=videos,
                           guest_daily_voice=voice,
                           is_owner_channel=lambda ch: ch == "telegram:1")


def _registry(settings, *, owner=False, friend=False, image_result="Image saved to /a.png"):
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._mcp_ready = True
    reg.mcp = MagicMock()
    reg.mcp.dispatch = AsyncMock(return_value=None)
    reg.settings = settings
    reg._handlers = {
        "generate_image": AsyncMock(return_value=image_result),
        "generate_video": AsyncMock(return_value="Video saved to /a.mp4"),
        "text_to_speech": AsyncMock(return_value="TTS saved to /a.mp3"),
    }
    reg._remember_doing = lambda *a: None
    reg.set_owner(owner)
    reg.set_friend(friend)
    reg._current_sender_channel = GUEST
    return reg


@pytest.mark.asyncio
async def test_guest_hits_the_daily_image_cap(tmp_path):
    reg = _registry(_settings(tmp_path))
    for _ in range(2):
        assert "saved to" in await reg.dispatch("generate_image", '{"prompt": "cat"}')
    out = await reg.dispatch("generate_image", '{"prompt": "cat"}')
    assert "Daily limit reached" in out and "2 images" in out
    assert reg._handlers["generate_image"].await_count == 2


@pytest.mark.asyncio
async def test_failures_are_free(tmp_path):
    reg = _registry(_settings(tmp_path), image_result="Refused: no")
    for _ in range(5):
        await reg.dispatch("generate_image", '{"prompt": "cat"}')
    assert reg._handlers["generate_image"].await_count == 5
    assert guest_caps.used(reg.settings, GUEST, "generate_image") == 0


@pytest.mark.asyncio
async def test_zero_blocks_and_negative_is_unlimited(tmp_path):
    reg = _registry(_settings(tmp_path))
    assert "can't have videos" in await reg.dispatch("generate_video", '{"prompt": "x"}')
    for _ in range(20):
        assert "saved to" in await reg.dispatch("text_to_speech", '{"text": "hi"}')


@pytest.mark.asyncio
async def test_friends_and_owner_are_never_capped(tmp_path):
    for kw in ({"friend": True}, {"owner": True}):
        reg = _registry(_settings(tmp_path, images=0, videos=0, voice=0), **kw)
        assert "saved to" in await reg.dispatch("generate_image", '{"prompt": "cat"}')
        assert "saved to" in await reg.dispatch("generate_video", '{"prompt": "x"}')


@pytest.mark.asyncio
async def test_caps_are_per_guest_and_per_day(tmp_path, monkeypatch):
    s = _settings(tmp_path, images=1)
    reg = _registry(s)
    await reg.dispatch("generate_image", '{"prompt": "cat"}')
    assert "Daily limit" in await reg.dispatch("generate_image", '{"prompt": "cat"}')
    reg._current_sender_channel = "telegram:3"
    assert "saved to" in await reg.dispatch("generate_image", '{"prompt": "cat"}')
    monkeypatch.setattr(guest_caps, "_today", lambda settings: "2099-01-01")
    reg._current_sender_channel = GUEST
    assert "saved to" in await reg.dispatch("generate_image", '{"prompt": "cat"}')


def test_counting_what_was_made():
    assert guest_caps.made("Image saved to /a.png\nImage saved to /b.png") == 2
    assert guest_caps.made("Pending: a slow image is on its way") == 1
    assert guest_caps.made("Refused: explicit") == 0
