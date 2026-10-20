"""Backups: what's included, consistency of live DBs, pruning, restore, and
the daily/off-phone loop."""

from __future__ import annotations

import io
import sqlite3
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia import backup


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "data" / "artifacts").mkdir(parents=True)
    (home / "data" / "logs" / "media").mkdir(parents=True)
    (home / "memories").mkdir()
    (home / "versions").mkdir()
    (home / "avatar").mkdir()
    (home / "SOUL.md").write_text("You are a tsundere.", encoding="utf-8")
    (home / "GROWTH.md").write_text("# Who I'm becoming\n- likes storms\n", encoding="utf-8")
    (home / ".env").write_text("DEEPSEEK_API_KEY=secret", encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text("owner works nights", encoding="utf-8")
    (home / "versions" / "SOUL-1.md").write_text("old soul", encoding="utf-8")
    (home / "data" / "artifacts" / "big.png").write_bytes(b"x" * 100)
    (home / "data" / "logs" / "media" / "pic.jpg").write_bytes(b"x" * 100)
    (home / "avatar" / "model.vrm").write_bytes(b"x" * 100)
    (home / "data" / "ophelia.log").write_text("noise", encoding="utf-8")
    with sqlite3.connect(home / "data" / "memory.db") as db:
        db.execute("CREATE TABLE messages (content TEXT)")
        db.execute("INSERT INTO messages VALUES ('my sister Maya works at Lark Bakery')")
    return home


def _names(archive: Path) -> set[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


def test_backup_contents(tmp_path):
    home = _home(tmp_path)
    out = backup.create_backup(tmp_path / "bk", home=home)
    names = _names(out)
    assert {"SOUL.md", "GROWTH.md", "memories/MEMORY.md", "versions/SOUL-1.md",
            "data/memory.db", backup.MANIFEST} <= names
    # Secrets, bulky media, avatar models and logs stay out.
    assert ".env" not in names
    assert not any("artifacts" in n or "media" in n or n.endswith(".vrm") or n.endswith(".log")
                   for n in names)
    assert ".env" in _names(backup.create_backup(tmp_path / "bk2", home=home, include_secrets=True))


def test_live_database_snapshot_is_consistent(tmp_path):
    home = _home(tmp_path)
    # A writer holds an open transaction while we back up.
    conn = sqlite3.connect(home / "data" / "memory.db")
    conn.execute("INSERT INTO messages VALUES ('uncommitted')")
    out = backup.create_backup(tmp_path / "bk", home=home)
    conn.rollback()
    conn.close()
    restored = tmp_path / "check.db"
    with tarfile.open(out, "r:gz") as tar:
        restored.write_bytes(tar.extractfile("data/memory.db").read())
    with sqlite3.connect(restored) as db:
        rows = [r[0] for r in db.execute("SELECT content FROM messages")]
    assert rows == ["my sister Maya works at Lark Bakery"]


def test_prune_keeps_newest(tmp_path):
    d = tmp_path / "bk"
    d.mkdir()
    for i in range(5):
        (d / f"{backup.ARCHIVE_PREFIX}2026010{i}-000000.tar.gz").write_bytes(b"x")
    backup.prune(d, keep=2)
    assert [p.name for p in backup.list_backups(d)] == [
        f"{backup.ARCHIVE_PREFIX}20260104-000000.tar.gz",
        f"{backup.ARCHIVE_PREFIX}20260103-000000.tar.gz",
    ]


def test_restore_roundtrip_with_safety_copy(tmp_path):
    home = _home(tmp_path)
    archive = backup.create_backup(tmp_path / "bk", home=home)
    (home / "SOUL.md").write_text("You are a polite assistant.", encoding="utf-8")
    (home / "GROWTH.md").unlink()

    msg = backup.restore_backup(archive, home=home, safety_dir=tmp_path / "safety")
    assert "Restored" in msg
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "You are a tsundere."
    assert "likes storms" in (home / "GROWTH.md").read_text(encoding="utf-8")
    # The state we restored over was saved first (including secrets).
    safety = backup.list_backups(tmp_path / "safety")
    assert safety and "polite assistant" in tarfile.open(safety[0]).extractfile("SOUL.md").read().decode()
    assert ".env" in _names(safety[0])


def test_restore_refuses_path_traversal(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        for name, data in ((backup.MANIFEST, b'{"files": []}'), ("../escape.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="unsafe"):
        backup.restore_backup(evil, home=tmp_path / "home2", safety_dir=tmp_path / "s")
    assert not (tmp_path / "escape.txt").exists()


def _settings(tmp_path, **kw):
    base = dict(
        backup_dir=str(tmp_path / "bk"),
        backup_interval_hours=24,
        backup_keep=7,
        backup_offsite="none",
        backup_offsite_days=7,
        backup_include_secrets=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_loop_runs_daily_and_sends_offsite_weekly(tmp_path):
    home = _home(tmp_path)
    sender = AsyncMock(return_value=True)
    loop = backup.BackupLoop(
        _settings(tmp_path, backup_offsite="owner"), home=home, offsite_sender=sender
    )
    t0 = time.time()
    assert await loop.run_once(now=t0) is not None
    sender.assert_awaited_once()
    assert sender.await_args.args[0].name.startswith(backup.ARCHIVE_PREFIX)
    assert await loop.run_once(now=t0 + 3600) is None  # not due yet
    assert await loop.run_once(now=t0 + 25 * 3600) is not None  # next day: local only
    assert sender.await_count == 1
    await loop.run_once(now=t0 + 8 * 86400)  # a week later: off-phone again
    assert sender.await_count == 2


@pytest.mark.asyncio
async def test_failed_offsite_retries_next_time(tmp_path):
    home = _home(tmp_path)
    sender = AsyncMock(return_value=False)
    loop = backup.BackupLoop(
        _settings(tmp_path, backup_offsite="owner"), home=home, offsite_sender=sender
    )
    t0 = time.time()
    await loop.run_once(now=t0)
    await loop.run_once(now=t0 + 25 * 3600)
    assert sender.await_count == 2


@pytest.mark.asyncio
async def test_offsite_off_by_default(tmp_path):
    sender = AsyncMock(return_value=True)
    loop = backup.BackupLoop(_settings(tmp_path), home=_home(tmp_path), offsite_sender=sender)
    await loop.run_once()
    sender.assert_not_awaited()


def test_termux_default_dir_is_shared_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("ophelia.platform.is_termux", lambda: True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # No shared storage set up yet → falls back to ~/.ophelia/backups.
    assert backup.default_backup_dir() == backup.OPHELIA_HOME / "backups"
    (tmp_path / "storage" / "shared").mkdir(parents=True)
    assert backup.default_backup_dir() == tmp_path / "storage" / "shared" / "Ophelia" / "backups"


@pytest.mark.asyncio
async def test_hub_owner_document_reports_any_success(tmp_path):
    from ophelia.channels.hub import ChannelHub

    class _GW:
        platform = "x"

        def __init__(self, ok):
            self.ok = ok
            self.sent = []

        def is_configured(self):
            return True

        async def send_owner_document(self, path, caption=""):
            self.sent.append(path)
            if self.ok is None:
                raise RuntimeError("down")
            return self.ok

    hub = ChannelHub.__new__(ChannelHub)
    f = tmp_path / "b.tar.gz"
    f.write_bytes(b"x")
    hub._gateways = [_GW(None), _GW(False)]
    assert await hub.send_owner_document(f) is False
    hub._gateways = [_GW(None), _GW(True)]
    assert await hub.send_owner_document(f, "keep safe") is True
