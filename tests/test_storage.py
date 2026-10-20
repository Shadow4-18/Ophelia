"""Housekeeping: old vision screenshots pruned; chat-log media only with an
explicit retention; chat-log entries hard-link instead of copying."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ophelia.storage import HousekeepingLoop, housekeep, human_bytes


def _old(p: Path, hours: float, size: int = 100) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = time.time() - hours * 3600
    os.utime(p, (t, t))
    return p


def test_screenshots_pruned_but_calibration_kept(tmp_path):
    data = tmp_path / "data"
    old = _old(data / "screenshots" / "screen_1.png", 48)
    fresh = _old(data / "screenshots" / "screen_2.png", 1)
    calib = _old(data / "screenshots" / "calibrate_1.png", 500)
    res = housekeep(data, screenshot_hours=24, chat_media_days=0)
    assert not old.exists() and fresh.exists() and calib.exists()
    assert res.by_dir["screenshots"] == (1, 100)


def test_chat_media_kept_unless_retention_chosen(tmp_path):
    data = tmp_path / "data"
    media = _old(data / "logs" / "media" / "in_x.jpg", 24 * 90, size=500)
    housekeep(data, screenshot_hours=24, chat_media_days=0)
    assert media.exists()  # default: keep forever
    preview = housekeep(data, screenshot_hours=24, chat_media_days=30, dry_run=True)
    assert media.exists() and preview.by_dir["chat-log media"] == (1, 500)
    housekeep(data, screenshot_hours=24, chat_media_days=30)
    assert not media.exists()


def test_loop_uses_settings(tmp_path):
    data = tmp_path / "data"
    old = _old(data / "screenshots" / "screen_9.png", 30)
    loop = HousekeepingLoop(
        SimpleNamespace(data_dir=data, screenshot_keep_hours=24, chat_log_media_days=0)
    )
    assert loop.run_once().files == 1 and not old.exists()


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(3 * 1024**3) == "3.0 GB"


@pytest.mark.asyncio
async def test_chat_log_media_is_hard_linked(tmp_path):
    from ophelia.channels.chat_log import ChatLogger

    src = tmp_path / "artifact.png"
    src.write_bytes(b"png" * 1000)
    logger = ChatLogger(db_path=tmp_path / "logs" / "chat_log.db", media_dir=tmp_path / "logs" / "media")
    await logger.log(channel="telegram:1", direction="out", media_path=src, media_kind="photo")
    (copy,) = list((tmp_path / "logs" / "media").iterdir())
    assert os.path.samefile(copy, src)  # same inode: no extra space used
