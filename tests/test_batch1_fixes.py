"""Audit batch 1: history budget, run_code kill, client reuse, background
tasks, supervised loops, atomic state writes."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.core import background
from ophelia.fileio import atomic_write_json, atomic_write_text


# --- 1. her inner check-ins no longer push the conversation out ------------


def _agent(rows_by_channel):
    from ophelia.core.agent_loop import AgentLoop

    async def recent(channels, limit=35):
        rows = [r for c in channels for r in rows_by_channel.get(c, [])]
        rows.sort(key=lambda r: r["created_at"])
        return rows[-limit:]

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    agent.memory.recent_across_channels = AsyncMock(side_effect=recent)
    return agent


@pytest.mark.asyncio
async def test_chat_history_keeps_its_own_budget():
    chat = [{"channel": "telegram:1", "role": "user" if i % 2 else "assistant",
             "content": f"chat {i}", "metadata": {}, "created_at": float(i)}
            for i in range(40)]
    ticks = []
    for i in range(60):  # she thought a lot after the chat
        t = 100.0 + i
        ticks.append({"channel": "consciousness", "role": "user", "content": f"tick {i}",
                      "metadata": {"type": "consciousness_tick"}, "created_at": t})
        ticks.append({"channel": "consciousness", "role": "assistant",
                      "content": '{"internal_thought": "x", "action": "reflect"}',
                      "metadata": {"type": "consciousness_json"}, "created_at": t + 0.5})
    agent = _agent({"telegram:1": chat, "consciousness": ticks})
    rows = await agent._history(["telegram:1", "consciousness"], "telegram:1",
                                current_is_tick=False)
    talk = [r for r in rows if r["channel"] == "telegram:1"]
    inner = [r for r in rows if r["channel"] == "consciousness"]
    assert len(talk) == agent.HISTORY_LIMIT  # all 35 conversation turns kept
    assert len(inner) == agent.INNER_IN_CHAT and all(r["role"] == "user" for r in inner)
    assert [r["created_at"] for r in rows] == sorted(r["created_at"] for r in rows)
    # A tick turn sees its full inner history.
    rows = await agent._history(["consciousness", "telegram:1"], "consciousness",
                                current_is_tick=True)
    assert sum(r["channel"] == "consciousness" for r in rows) == agent.HISTORY_LIMIT
    assert sum(r["channel"] == "telegram:1" for r in rows) == agent.TALK_IN_TICK


# --- 2. run_code kills what it times out ------------------------------------


@pytest.mark.asyncio
async def test_run_code_timeout_kills_the_process(monkeypatch):
    from ophelia.tools.registry import ToolRegistry

    procs = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        p = await real(sys.executable, *args[1:], **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    real_wait_for = asyncio.wait_for

    async def short(aw, timeout):
        return await real_wait_for(aw, timeout=min(timeout, 0.5))

    monkeypatch.setattr(asyncio, "wait_for", short)
    reg = ToolRegistry.__new__(ToolRegistry)
    out = await reg._run_code("import time\ntime.sleep(60)")
    assert "timed out" in out and "stopped" in out
    assert procs and procs[0].returncode is not None  # killed and reaped


# --- 3. one xAI client per token --------------------------------------------


@pytest.mark.asyncio
async def test_xai_client_reused_until_token_changes(monkeypatch):
    from ophelia.providers import router

    router._XAI_CLIENTS.clear()
    backend = router.XAIBackend(SimpleNamespace(xai_base_url="https://api.x.ai/v1"),
                                prefer_oauth=False)
    tokens = iter(["t1", "t1", "t2"])
    monkeypatch.setattr(backend, "bearer_fresh", AsyncMock(side_effect=lambda: next(tokens)))
    a = await backend.async_client_fresh()
    b = await backend.async_client_fresh()
    c = await backend.async_client_fresh()
    assert a is b and c is not a and c.api_key == "t2"


# --- 4/5. background tasks are kept and loops come back ---------------------


@pytest.mark.asyncio
async def test_spawn_keeps_and_logs():
    done = asyncio.Event()

    async def work():
        await asyncio.sleep(0.01)
        done.set()

    t = background.spawn(work(), name="w")
    assert t in background._TASKS
    await asyncio.wait_for(done.wait(), 1)
    await asyncio.sleep(0)
    assert t not in background._TASKS

    async def boom():
        raise RuntimeError("x")

    bad = background.spawn(boom(), name="boom")
    await asyncio.sleep(0.01)
    assert bad.done() and bad not in background._TASKS


@pytest.mark.asyncio
async def test_supervised_loop_restarts_after_a_crash_and_gives_up_eventually():
    runs = []

    async def loop():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("crash")

    await asyncio.wait_for(background.supervise("x", loop, backoff=0.001), 2)
    assert len(runs) == 3  # crashed twice, came back, then ended normally

    always = []

    async def broken():
        always.append(1)
        raise RuntimeError("always")

    await asyncio.wait_for(background.supervise("y", broken, backoff=0.001, max_restarts=2), 2)
    assert len(always) == 3  # first run + 2 restarts, then it stops trying


# --- 6. atomic state files ---------------------------------------------------


def test_atomic_write_leaves_no_half_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}

    def explode(*a, **k):
        raise OSError("killed mid-write")

    import os

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        atomic_write_text(path, '{"a": 2, "half')
    assert json.loads(path.read_text()) == {"a": 1}  # old file intact
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp left
