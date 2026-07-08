"""Tests for inner thought mirroring — full text to Discord, short preview to chat."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_inner_write_passes_full_thought_to_notify(tmp_path: Path):
    from ophelia.mind.inner_log import InnerMonologue

    notify = AsyncMock()
    inner = InnerMonologue(log_path=tmp_path / "inner.md", notify=notify)
    long_thought = "Dream: " + ("x" * 800)

    await inner.write(long_thought, kind="dream")

    notify.assert_awaited_once_with(long_thought)


@pytest.mark.asyncio
async def test_notify_inner_mirrors_full_text_but_truncates_proactive():
    from ophelia.core.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.hub = MagicMock()
    orch.hub.mirror_inner_thought = AsyncMock()
    orch.hub.broadcast_proactive = AsyncMock()
    orch.settings = MagicMock(inner_mirror_telegram=True)
    orch.signals = MagicMock(inner_mirror=False)

    long_text = "Dream: " + ("y" * 800)
    await orch._notify_inner(long_text)

    orch.hub.mirror_inner_thought.assert_awaited_once_with(long_text)
    orch.hub.broadcast_proactive.assert_awaited_once_with(long_text[:500])
