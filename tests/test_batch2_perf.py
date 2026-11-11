"""Audit batch 2: cache-friendly prompt order, cached ledger reads, ledger
trimming, lookups run together."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ophelia.memory.bootstrap import build_system_context


def test_stable_parts_come_before_what_changes_every_turn(monkeypatch):
    monkeypatch.setattr("ophelia.mind.growth.load_growth", lambda: "")
    monkeypatch.setattr("ophelia.memory.bootstrap.load_prompter", lambda: "POLICY")

    def build(mood, now):
        return build_system_context(
            soul="SOUL", memory_entries=["m1", "m2"], user_entries=["u1"],
            psyche_block=f"Mood: {mood}", stable_extra="IDENTITY", extra=f"Now: {now}")

    a, b = build("happy", "10:01"), build("sulky", "10:02")
    order = [a.index(x) for x in ("SOUL", "u1", "m1", "POLICY", "IDENTITY", "Mood:", "Now:")]
    assert order == sorted(order)
    # Two turns share everything up to the mood: that's what the cache reuses.
    shared = 0
    while shared < min(len(a), len(b)) and a[shared] == b[shared]:
        shared += 1
    assert a[:shared].endswith("Mood: ") and "IDENTITY" in a[:shared]


def test_ledger_parsed_once_per_change(tmp_path, monkeypatch):
    from ophelia.providers.civitai import feedback

    path = tmp_path / "l.jsonl"
    path.write_text("\n".join(json.dumps({"i": i}) for i in range(10)))
    feedback._JSONL_CACHE.clear()
    calls = []
    real = type(path).read_text

    def counting(self, *a, **k):
        calls.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", counting)
    for _ in range(5):
        assert len(feedback._read_jsonl(path, 4000)) == 10
    assert len(calls) == 1
    time.sleep(0.01)
    with path.open("a") as f:
        f.write("\n" + json.dumps({"i": 10}))
    assert len(feedback._read_jsonl(path, 4000)) == 11 and len(calls) == 2


def test_ledger_is_trimmed_when_it_gets_big(tmp_path, monkeypatch):
    from ophelia.providers.civitai import pipeline

    monkeypatch.setattr(pipeline, "_LEDGER_MAX_BYTES", 2000)
    monkeypatch.setattr(pipeline, "_LEDGER_KEEP_LINES", 5)
    s = SimpleNamespace(data_dir=tmp_path)
    for i in range(60):
        pipeline.write_ledger(s, {"n": i, "pad": "x" * 40})
    lines = pipeline.ledger_path(s).read_text().splitlines()
    assert len(lines) <= 30 and json.loads(lines[-1])["n"] == 59


@pytest.mark.asyncio
async def test_lookups_in_one_round_run_together():
    from unittest.mock import AsyncMock

    from tests.test_tool_tiers import _agent, _resp, _tc

    agent = _agent("deepseek")
    running, peak, order = 0, 0, []

    async def dispatch(name, arguments):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.2)
        running -= 1
        order.append(name)
        return f"result of {name}"

    agent.tools.dispatch = dispatch
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(tool_calls=[_tc("recall_memory", '{"query": "a"}', "c1"),
                          _tc("web_search", '{"query": "b"}', "c2")]),
        _resp(content="done"),
    ])
    started = time.monotonic()
    out = await agent._complete([{"role": "user", "content": "what did we say about x?"}],
                                store_channel="telegram:1")
    assert out == "done" and peak == 2
    assert time.monotonic() - started < 0.35  # ~one lookup's time, not two
    sent = agent._call_with_fallback.await_args_list[1].args[0] \
        if agent._call_with_fallback.await_args_list[1].args else \
        agent._call_with_fallback.await_args_list[1].kwargs.get("messages")
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]  # in order
    assert tool_msgs[0]["content"] == "result of recall_memory"


@pytest.mark.asyncio
async def test_actions_stay_one_at_a_time():
    from unittest.mock import AsyncMock

    from tests.test_tool_tiers import _agent, _resp, _tc

    agent = _agent("deepseek")
    running, peak = 0, 0

    async def dispatch(name, arguments):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return "ok"

    agent.tools.dispatch = dispatch
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(tool_calls=[_tc("send_message", '{"text": "a"}', "c1"),
                          _tc("generate_image", '{"prompt": "b"}', "c2")]),
        _resp(content="done"),
    ])
    await agent._complete([{"role": "user", "content": "hi"}], store_channel="telegram:1")
    assert peak == 1
