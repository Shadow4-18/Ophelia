"""Tests for ophelia self-update (git pull + reinstall helpers)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ophelia.setup.update import (
    UpdateResult,
    find_repo_root,
    run_update,
)


def test_find_repo_root_from_workspace():
    root = find_repo_root(Path(__file__).resolve())
    assert root is not None
    assert (root / ".git").exists()
    assert (root / "pyproject.toml").is_file()


def test_update_result_summary_ok():
    r = UpdateResult(
        ok=True,
        repo=Path("/tmp/Ophelia"),
        branch="main",
        before_sha="aaaaaaaa",
        after_sha="bbbbbbbb",
        changed=True,
        steps=["git fetch origin: ok", "pip install -e .: ok"],
        restart_scheduled=True,
    )
    text = r.summary()
    assert "Update OK" in text
    assert "main" in text
    assert "Restart scheduled" in text


def test_update_result_summary_fail():
    r = UpdateResult(ok=False, error="git fetch failed: nope")
    assert "Update failed" in r.summary()
    assert "nope" in r.summary()


def test_run_update_refuses_dirty(tmp_path: Path, monkeypatch):
    # Fake a mini repo
    repo = tmp_path / "Ophelia"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='ophelia'\n")
    (repo / ".git").mkdir()
    (repo / "src" / "ophelia").mkdir(parents=True)
    (repo / "src" / "ophelia" / "__init__.py").write_text("")

    monkeypatch.setattr("ophelia.setup.update.find_repo_root", lambda start=None: repo)

    def fake_git(repo_path, *args, timeout=120.0):
        if args[:2] == ("status", "--porcelain"):
            return 0, " M src/ophelia/cli.py"
        if args[:1] == ("rev-parse",):
            return 0, "deadbeef\n"
        return 0, ""

    monkeypatch.setattr("ophelia.setup.update._git", fake_git)
    monkeypatch.setattr(
        "ophelia.setup.update.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    result = run_update(allow_dirty=False, restart=False)
    assert not result.ok
    assert "uncommitted" in (result.error or "").lower()


def test_run_update_success_no_restart(tmp_path: Path, monkeypatch):
    repo = tmp_path / "Ophelia"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='ophelia'\n")
    (repo / ".git").mkdir()
    (repo / "src" / "ophelia").mkdir(parents=True)

    monkeypatch.setattr("ophelia.setup.update.find_repo_root", lambda start=None: repo)
    monkeypatch.setattr(
        "ophelia.setup.update.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" else "/usr/bin/pip",
    )

    calls: list[tuple] = []

    def fake_git(repo_path, *args, timeout=120.0):
        calls.append(("git",) + args)
        if args[:2] == ("status", "--porcelain"):
            return 0, ""
        if args == ("rev-parse", "HEAD"):
            # before vs after: change after pull
            if any(c[1:2] == ("pull",) for c in calls[:-1]):
                return 0, "bbbbbbbb"
            return 0, "aaaaaaaa"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return 0, "main"
        if args[:1] == ("fetch",):
            return 0, ""
        if args[:1] == ("pull",):
            return 0, "Updating aaaaaaaa..bbbbbbbb\nFast-forward"
        return 0, ""

    def fake_run(args, *, cwd, timeout=300.0):
        calls.append(tuple(args))
        return 0, "ok"

    monkeypatch.setattr("ophelia.setup.update._git", fake_git)
    monkeypatch.setattr("ophelia.setup.update._run", fake_run)

    result = run_update(restart=False, allow_dirty=False)
    assert result.ok
    assert result.changed
    assert result.before_sha == "aaaaaaaa"
    assert result.after_sha == "bbbbbbbb"
    assert any("pip" in str(c).lower() or "install" in str(c) for c in calls)


@pytest.mark.asyncio
async def test_session_cmd_update_parses_args(monkeypatch):
    from ophelia.channels.session import ChannelSession
    from ophelia.setup.update import UpdateResult

    captured = {}

    def fake_run_update(*, branch=None, restart=True, allow_dirty=False, remote="origin"):
        captured["branch"] = branch
        captured["restart"] = restart
        captured["allow_dirty"] = allow_dirty
        return UpdateResult(ok=True, changed=False, branch=branch or "main")

    monkeypatch.setattr("ophelia.setup.update.run_update", fake_run_update)
    # Also patch where session imports it inside the method
    import ophelia.setup.update as upd

    monkeypatch.setattr(upd, "run_update", fake_run_update)
    monkeypatch.setattr(upd, "request_process_exit_soon", lambda delay_sec=2.5: None)

    replies: list[str] = []

    class _Dummy:
        pass

    session = ChannelSession(
        agent=_Dummy(),  # type: ignore[arg-type]
        signals=_Dummy(),  # type: ignore[arg-type]
        memory=_Dummy(),  # type: ignore[arg-type]
        drives=_Dummy(),  # type: ignore[arg-type]
    )

    async def _reply(t: str) -> None:
        replies.append(t)

    await session.cmd_update(
        ["main", "dirty", "norestart"],
        _reply,
    )
    assert captured["branch"] == "main"
    assert captured["allow_dirty"] is True
    assert captured["restart"] is False
    assert any("Update" in r or "up to date" in r.lower() or "Updating" in r for r in replies)
