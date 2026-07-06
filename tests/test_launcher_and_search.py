"""Launcher state detection + web search error clarity.

The launcher menu used to trust the heartbeat alone, so a stale-but-recent
heartbeat (left over from a crash) trapped the user out of the Start menu
on Termux — Stop/Restart were shown but Stop no-op'd and Start wasn't
offered. _is_running() now cross-checks the tmux session on Termux, and a
cleanup action clears the trapped state.

Web search's DDG fallback used to return "No results" both when there were
genuinely no results and when both endpoints errored. It now surfaces the
error reason so the agent can tell the user the backend is down.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ophelia.setup import launcher
from ophelia.tools import web_search


def _write_heartbeat(path: Path, ts: float, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": ts, **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_is_running_no_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: False)
    assert launcher._is_running() is False


def test_is_running_fresh_heartbeat_non_termux(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: False)
    _write_heartbeat(tmp_path / "hb.json", time.time())
    assert launcher._is_running() is True


def test_is_running_stale_heartbeat_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: False)
    _write_heartbeat(tmp_path / "hb.json", time.time() - 300)
    assert launcher._is_running() is False


def test_is_running_termux_requires_tmux_session(tmp_path, monkeypatch):
    """On Termux, a fresh heartbeat alone is NOT enough — the tmux session
    must also be alive. This is what prevents the trapped state."""
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: True)
    _write_heartbeat(tmp_path / "hb.json", time.time())
    # tmux session dead (e.g. Ophelia crashed) — must report not running.
    monkeypatch.setattr(launcher, "_tmux_session_active", lambda: False)
    assert launcher._is_running() is False
    # And the trapped state must be flagged.
    assert launcher._stale_state() is True


def test_is_running_termux_heartbeat_and_tmux_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: True)
    _write_heartbeat(tmp_path / "hb.json", time.time())
    monkeypatch.setattr(launcher, "_tmux_session_active", lambda: True)
    assert launcher._is_running() is True
    assert launcher._stale_state() is False


def test_stale_state_only_meaningful_on_termux(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: False)
    _write_heartbeat(tmp_path / "hb.json", time.time())
    assert launcher._stale_state() is False


def test_menu_offers_cleanup_when_stale(tmp_path, monkeypatch):
    """When the heartbeat says running but tmux is dead, the menu must offer
    a Clear-stale-state action so the user isn't trapped."""
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: True)
    _write_heartbeat(tmp_path / "hb.json", time.time())
    monkeypatch.setattr(launcher, "_tmux_session_active", lambda: False)
    items = launcher._menu_items()
    labels = [label for label, _, _ in items]
    assert any("Clear stale state" in label for label in labels)
    assert any("Start Ophelia" in label for label in labels)


def test_menu_no_cleanup_when_cleanly_stopped(tmp_path, monkeypatch):
    """When there's no heartbeat and no tmux, only Start is offered."""
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: True)
    monkeypatch.setattr(launcher, "_tmux_session_active", lambda: False)
    items = launcher._menu_items()
    labels = [label for label, _, _ in items]
    assert any("Start Ophelia" in label for label in labels)
    assert not any("Clear stale state" in label for label in labels)


@pytest.mark.asyncio
async def test_web_search_surfaces_backend_errors(monkeypatch):
    """When all DDG endpoints error, the result must say 'failed' with the
    reason — not 'No results' (which means the query had no hits)."""
    import httpx

    async def fake_get(self, url, **_):
        raise httpx.ConnectError("simulated outage")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    out = await web_search._duckduckgo_search("anything", 5)
    assert "failed" in out.lower()
    assert "simulated outage" in out or "ConnectError" in out


@pytest.mark.asyncio
async def test_web_search_no_results_when_endpoints_return_empty(monkeypatch):
    """When the endpoints respond 200 but yield nothing, that's a genuine
    'no results' — distinct from a backend failure."""
    import httpx

    class _FakeResp:
        status_code = 200

        def json(self):
            return {}

        @property
        def text(self):
            return ""

    async def fake_get(self, url, **_):
        return _FakeResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    out = await web_search._duckduckgo_search("obscure-query-xyz", 5)
    assert "No results" in out
    assert "failed" not in out.lower()
