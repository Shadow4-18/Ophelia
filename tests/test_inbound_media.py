"""Tests for inbound attachment classification + inbox listing."""

from __future__ import annotations

from pathlib import Path

import pytest

from ophelia.channels.inbound_media import classify_attachment, safe_inbound_ext


@pytest.mark.parametrize(
    "filename,mime,expected",
    [
        ("clip.mp4", "video/mp4", "video"),
        ("clip.MOV", "", "video"),
        ("pack.zip", "application/zip", "file"),
        ("shot.png", "image/png", "image"),
        ("notes.pdf", "application/pdf", "file"),
        ("weird.exe", "application/octet-stream", None),
    ],
)
def test_classify_attachment(filename, mime, expected):
    assert classify_attachment(filename=filename, mime=mime) == expected


def test_safe_inbound_ext():
    assert safe_inbound_ext("a.MP4", kind="video") == ".mp4"
    assert safe_inbound_ext("", kind="video") == ".mp4"
    assert safe_inbound_ext("pack.zip", kind="file") == ".zip"


@pytest.mark.asyncio
async def test_list_inbox_files_finds_video_and_zip(isolated_env, settings, tmp_path):
    from ophelia.tools.registry import ToolRegistry

    tg = settings.data_dir / "telegram_media"
    tg.mkdir(parents=True)
    (tg / "in_1.mp4").write_bytes(b"fake-video")
    (tg / "in_2.zip").write_bytes(b"PK\x03\x04")
    (tg / "in_3.jpg").write_bytes(b"\xff\xd8\xff")
    (tg / "out_ignore.mp4").write_bytes(b"nope")  # not inbound prefix

    tools = ToolRegistry(settings, tmp_path / "art")
    all_out = await tools._list_inbox_files(kind="all", within_hours=24)
    assert "in_1.mp4" in all_out
    assert "in_2.zip" in all_out
    assert "in_3.jpg" in all_out
    assert "out_ignore" not in all_out

    vids = await tools._list_inbox_files(kind="video", within_hours=24)
    assert "in_1.mp4" in vids
    assert "in_2.zip" not in vids

    zips = await tools._list_inbox_files(kind="file", within_hours=24)
    assert "in_2.zip" in zips
