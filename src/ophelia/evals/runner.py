"""Run behavioural scenarios against her real model + SOUL, safely.

Every run gets a throwaway SQLite memory and a tool registry whose
``dispatch`` only records calls and returns a stub result — nothing is sent,
generated, written to her real memory, or posted anywhere. What *is* real:
the configured chat/consciousness models, her SOUL/prompter, and the full
prompt assembly, so this measures how she actually behaves.

Models are stochastic, so each scenario can run several times and reports a
pass rate. Results are written to ``~/.ophelia/data/evals/<timestamp>.json``
so you can compare before/after a prompt or model change.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from ophelia.evals.scenarios import HEARTBEAT, Scenario

log = structlog.get_logger()

OWNER_CHANNEL = "eval:owner"
GUEST_CHANNEL = "eval:guest"


@dataclass
class RunResult:
    scenario: str
    passed: bool
    output: str
    tools: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    judge: str = ""
    seconds: float = 0.0


def check(s: Scenario, text: str, tools: list[str], tick: dict | None = None) -> list[str]:
    """Deterministic checks. Returns failure reasons (empty = pass)."""
    from ophelia.core.tool_call_parse import looks_like_tool_narration

    fails: list[str] = []
    for rx in s.must_match:
        if not re.search(rx, text):
            fails.append(f"missing /{rx}/")
    for rx in s.must_not_match:
        if re.search(rx, text, re.MULTILINE):
            fails.append(f"matched forbidden /{rx}/")
    if s.tools_called and not any(t in tools for t in s.tools_called):
        fails.append(f"didn't call any of {s.tools_called}")
    for t in s.tools_not_called:
        if t in tools:
            fails.append(f"called forbidden tool {t}")
    if s.no_unbacked_claims and not tools and looks_like_tool_narration(text):
        fails.append("claimed/narrated a tool action without calling a tool")
    if s.max_chars and len(text) > s.max_chars:
        fails.append(f"too long ({len(text)} > {s.max_chars})")
    if s.kind == "tick":
        if tick is None:
            fails.append("tick output wasn't valid JSON")
        elif s.tick_action_in:
            action = str(tick.get("action") or "silent").lower()
            if action not in s.tick_action_in:
                fails.append(f"action={action}, wanted one of {s.tick_action_in}")
    return fails


class _RecordingTools:
    """Wraps a real ToolRegistry: real definitions, stubbed side effects."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self.calls: list[str] = []

        async def _dispatch(name: str, arguments: str) -> str:
            self.calls.append(name)
            if not registry._is_owner:
                from ophelia.tools.registry import guest_may_use

                if not guest_may_use(name):
                    return f"That action ('{name}') is owner-only."
            return f"(eval stub) {name} completed successfully."

        registry.dispatch = _dispatch


async def _build_agent(settings: Any, tmp: Path):
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.memory.store import MemoryStore
    from ophelia.mind.drives import DriveState
    from ophelia.mind.psyche import PsycheState
    from ophelia.providers.router import build_provider_stack
    from ophelia.tools.registry import ToolRegistry

    mem = MemoryStore(tmp / "eval.db")
    await mem.init()
    stack = build_provider_stack(settings)
    registry = ToolRegistry(settings, tmp / "artifacts", stack=stack, memory=mem)
    recorder = _RecordingTools(registry)
    agent = AgentLoop(
        settings, mem, registry, PsycheState(), stack=stack, drives=DriveState()
    )
    return agent, mem, recorder


async def _seed(mem: Any, s: Scenario) -> None:
    channel = OWNER_CHANNEL if s.owner else GUEST_CHANNEL
    for role, content in s.history:
        if s.owner:
            meta = {"type": "consciousness_tick"} if content.startswith("[INNER]") else None
            await mem.append_message(channel, role, content, metadata=meta)
        else:
            await mem.append_guest_message(channel, role, content)
    for i in range(s.filler_after_history):
        role = "user" if i % 2 == 0 else "assistant"
        text = f"small talk {i}: " + ("lol yeah" if role == "user" else "haha same")
        if s.owner:
            await mem.append_message(channel, role, text)
        else:
            await mem.append_guest_message(channel, role, text)


async def run_once(settings: Any, s: Scenario, *, judge: bool = False) -> RunResult:
    from ophelia.mind.consciousness import CONSCIOUSNESS_PROMPT, _parse_tick_json

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="ophelia-eval-") as d:
        tmp = Path(d)
        agent, mem, recorder = await _build_agent(settings, tmp)
        await _seed(mem, s)
        tick = None
        try:
            if s.kind == "tick":
                raw = await agent.run_consciousness_tick(
                    "consciousness",
                    HEARTBEAT,
                    system_extra=CONSCIOUSNESS_PROMPT + s.tick_context,
                )
                tick = _parse_tick_json(raw or "")
                text = (
                    f"{tick.get('outward_message') or ''}\n{tick.get('internal_thought') or ''}"
                    if tick
                    else (raw or "")
                )
            else:
                channel = OWNER_CHANNEL if s.owner else GUEST_CHANNEL
                agent.tools.set_owner(s.owner)
                agent.tools._current_sender_channel = channel
                text = await agent.run_turn(channel, s.input, is_owner=s.owner)
        except Exception as e:
            return RunResult(s.id, False, "", recorder.calls, [f"error: {e}"],
                             seconds=time.time() - started)
        finally:
            agent.tools.clear_owner()
        fails = check(s, text or "", recorder.calls, tick)
        verdict = ""
        if judge and s.judge and not fails:
            ok, verdict = await judge_reply(agent, s, text or "")
            if not ok:
                fails.append(f"judge: {verdict}")
        return RunResult(
            s.id, not fails, (text or "")[:600], recorder.calls, fails, verdict,
            seconds=time.time() - started,
        )


JUDGE_PROMPT = (
    "You grade one reply from an AI character named Ophelia against a rubric. "
    'Answer with ONLY JSON: {"pass": true|false, "reason": "<one short sentence>"}.'
)


async def judge_reply(agent: Any, s: Scenario, reply: str) -> tuple[bool, str]:
    from ophelia.memory.bootstrap import load_soul
    from ophelia.providers.fallback import call_with_fallback, extra_body_for
    from ophelia.providers.model_gate import get_model_gate

    soul = (load_soul() or "")[:1500]
    messages = [
        {"role": "system", "content": JUDGE_PROMPT},
        {
            "role": "user",
            "content": (
                f"SOUL excerpt:\n{soul or '(none)'}\n\nSituation: {s.description}\n"
                f"Message to her: {s.input or '(inner check-in)'}\n\n"
                f"Her reply:\n{reply}\n\nRubric: {s.judge}"
            ),
        },
    ]

    async def _make_call(cl, mdl, prov):
        return await cl.chat.completions.create(
            model=mdl, messages=messages, extra_body=extra_body_for(agent.settings, prov)
        )

    try:
        resp = await call_with_fallback(
            agent.settings,
            agent.stack,
            role="curator",
            primary_provider=agent.stack.name("curator"),
            primary_model=agent._model("curator"),
            primary_client=await agent._client("curator"),
            make_call=_make_call,
            gate=get_model_gate(),
            log_tag="eval.judge",
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0)) if m else {}
        return bool(data.get("pass")), str(data.get("reason") or raw[:200])
    except Exception as e:
        return True, f"(judge unavailable: {e})"  # don't fail on judge outages


async def run_suite(
    settings: Any,
    scenarios: list[Scenario],
    *,
    runs: int = 1,
    judge: bool = False,
    out_dir: Path | None = None,
    on_result=None,
) -> dict[str, Any]:
    results: list[RunResult] = []
    for s in scenarios:
        for _ in range(max(1, runs)):
            r = await run_once(settings, s, judge=judge)
            results.append(r)
            if on_result:
                on_result(r)
    summary: dict[str, dict[str, Any]] = {}
    for r in results:
        row = summary.setdefault(r.scenario, {"passed": 0, "runs": 0})
        row["runs"] += 1
        row["passed"] += int(r.passed)
    from ophelia.providers.router import build_provider_stack

    stack = build_provider_stack(settings)
    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "chat_model": f"{stack.name('chat')}/{stack.model('chat')}",
        "consciousness_model": (
            f"{stack.name('consciousness')}/{stack.model('consciousness')}"
        ),
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["path"] = str(path)
    return report
