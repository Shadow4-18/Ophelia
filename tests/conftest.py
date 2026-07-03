"""Pytest configuration + shared fixtures for Ophelia life-loop tests.

Tier C #15: these tests guard the most regression-prone "alive" behaviors —
humor scoring, life-context inference, wake-word availability — so changes
to the soul don't silently break what makes her feel real in Telegram.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Make `ophelia` importable when running from a source checkout without an
# installed package (e.g. `pytest tests/` from the repo root).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Point OPHELIA_HOME at a temp dir so tests never touch real config/data."""
    home = tmp_path / "ophelia_home"
    home.mkdir()
    monkeypatch.setenv("OPHELIA_HOME", str(home))
    # Force a clean .env load path so the test settings don't inherit the
    # developer's real ~/.ophelia/.env.
    monkeypatch.setenv("OPHELIA_TIMEZONE", "UTC")
    monkeypatch.setenv("OPHELIA_WORK_DAYS", "mon,tue,wed,thu,fri")
    monkeypatch.setenv("OPHELIA_WORK_HOURS", "9-17")
    monkeypatch.setenv("OPHELIA_SLEEP_HOURS", "0-7")
    return home


@pytest_asyncio.fixture
async def memory(isolated_env):
    """A fresh MemoryStore backed by a temp SQLite DB."""
    from ophelia.memory.store import MemoryStore

    store = MemoryStore(isolated_env / "data" / "test.db")
    await store.init()
    yield store


@pytest.fixture
def settings(isolated_env):
    """Settings instance scoped to the temp OPHELIA_HOME."""
    from ophelia.config import Settings

    return Settings()


@pytest.fixture
def signals():
    """A fresh Signals state object (no shared state between tests)."""
    from ophelia.core.signals import Signals

    return Signals()


class ResumeStub:
    """Faithful replica of AgentLoop's resume bookkeeping, without importing
    AgentLoop (which would trigger a channels circular import at module load).

    Mirrors pending_resume_for + run_autonomous_continuation + MAX_CONTINUATIONS
    so the cap logic is tested against the same contract.
    """

    MAX_CONTINUATIONS = 6

    def __init__(self, settings):
        self.settings = settings
        self._pending_resume = {}
        self._continuation_count = {}

    def pending_resume_for(self, channel):
        if not self.settings.tool_loop_resume:
            return None
        if self._continuation_count.get(channel, 0) >= self.MAX_CONTINUATIONS:
            return None
        pending = self._pending_resume.get(channel)
        if not pending or pending.get("stuck"):
            return None
        return pending


@pytest.fixture
def resume_stub(settings):
    return ResumeStub(settings)
