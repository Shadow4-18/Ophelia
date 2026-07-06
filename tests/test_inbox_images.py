"""Inbound image path wiring.

The agent needs the absolute path of a photo the user sent so it can pass
it to generate_video for image-to-video. Two surfaces are tested here:

1. ChannelSession._extract_inbound_media — must handle both the new
   "saved to /abs/path" prompt form and the legacy "saved in_123.jpg" form.
2. ToolRegistry._list_inbox_images — must list recent inbound images from
   telegram_media/ and discord_media/, sorted newest-first.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ophelia.channels.session import ChannelSession


class _FakeSettings:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


def _touch(path: Path, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_extract_inbound_media_absolute_path(tmp_path):
    settings = _FakeSettings(tmp_path)
    img = tmp_path / "telegram_media" / "in_42.jpg"
    _touch(img)
    text = f"[User sent a photo — saved to {img}]\nCaption: hi"
    assert ChannelSession._extract_inbound_media(text, settings) == str(img)


def test_extract_inbound_media_legacy_filename(tmp_path):
    """Old gateways wrote only the filename; resolve under telegram_media."""
    settings = _FakeSettings(tmp_path)
    img = tmp_path / "telegram_media" / "in_7.png"
    _touch(img)
    text = "[User sent a photo — saved in_7.png]\nCaption: hi"
    assert ChannelSession._extract_inbound_media(text, settings) == str(img)


def test_extract_inbound_media_missing_returns_token(tmp_path):
    settings = _FakeSettings(tmp_path)
    text = "[User sent a photo — saved to /no/such/in_99.jpg]"
    # Absolute but missing — returns the token unchanged.
    assert ChannelSession._extract_inbound_media(text, settings) == "/no/such/in_99.jpg"


@pytest.mark.asyncio
async def test_list_inbox_images_empty(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.artifacts_dir = tmp_path / "artifacts"
    out = await reg._list_inbox_images()
    assert "No inbound images" in out


@pytest.mark.asyncio
async def test_list_inbox_images_lists_recent(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.artifacts_dir = tmp_path / "artifacts"

    now = time.time()
    # Three inbound images across telegram + discord, plus an outbound one
    # that must NOT show up (no `in_` prefix).
    tg_old = tmp_path / "telegram_media" / "in_1.jpg"
    tg_new = tmp_path / "telegram_media" / "in_2.jpg"
    dc_new = tmp_path / "discord_media" / "in_3.png"
    out_img = tmp_path / "telegram_media" / "image_999.png"
    _touch(tg_old, mtime=now - 48 * 3600)  # outside default 24h window
    _touch(tg_new, mtime=now - 60)
    _touch(dc_new, mtime=now - 30)
    _touch(out_img, mtime=now - 5)

    out = await reg._list_inbox_images()
    assert "in_2.jpg" in out
    assert "in_3.png" in out
    # Old image outside the 24h window must not appear.
    assert "in_1.jpg" not in out
    # Generated/outbound image must not appear.
    assert "image_999.png" not in out
    # Newest first.
    assert out.index("in_3.png") < out.index("in_2.jpg")


@pytest.mark.asyncio
async def test_list_inbox_images_within_hours_override(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.data_dir = tmp_path
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.artifacts_dir = tmp_path / "artifacts"

    now = time.time()
    old = tmp_path / "telegram_media" / "in_1.jpg"
    new = tmp_path / "telegram_media" / "in_2.jpg"
    _touch(old, mtime=now - 48 * 3600)
    _touch(new, mtime=now - 60)

    out = await reg._list_inbox_images(within_hours=72.0)
    assert "in_1.jpg" in out
    assert "in_2.jpg" in out
