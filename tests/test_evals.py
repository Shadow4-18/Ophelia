"""The eval harness itself: checks, tool stubbing, seeding, reports."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.evals import runner
from ophelia.evals.scenarios import BUILTIN, Scenario, load_scenarios


def test_checks():
    s = Scenario(
        id="x",
        description="",
        must_match=[r"fox"],
        must_not_match=[r'"action"\s*:'],
        tools_called=["generate_image"],
        tools_not_called=["edit_soul"],
        no_unbacked_claims=True,
        max_chars=50,
    )
    assert runner.check(s, "here's your fox", ["generate_image"]) == []
    fails = runner.check(s, 'I just sent a new image {"action": "x"}', ["edit_soul"])
    assert any("missing /fox/" in f for f in fails)
    assert any("forbidden" in f for f in fails)
    assert any("generate_image" in f for f in fails)
    assert any("edit_soul" in f for f in fails)


def test_unbacked_claim_needs_some_tool():
    s = Scenario(id="x", description="", no_unbacked_claims=True)
    assert runner.check(s, "I just sent a new image", []) != []
    assert runner.check(s, "I just sent a new image", ["generate_image"]) == []


def test_tick_action_check():
    s = Scenario(id="t", description="", kind="tick", tick_action_in=["silent"])
    assert runner.check(s, "", [], {"action": "silent"}) == []
    assert runner.check(s, "", [], {"action": "message"}) != []
    assert runner.check(s, "", [], None) == ["tick output wasn't valid JSON"]


def test_builtin_ids_unique_and_user_yaml(tmp_path):
    ids = [s.id for s in BUILTIN]
    assert len(ids) == len(set(ids))
    (tmp_path / "mine.yaml").write_text(
        "- id: my_case\n  description: d\n  input: hi\n  must_match: ['(?i)hi']\n",
        encoding="utf-8",
    )
    loaded = load_scenarios(tmp_path)
    assert loaded[-1].id == "my_case" and loaded[-1].must_match == ["(?i)hi"]


class _FakeTools:
    def __init__(self):
        self._is_owner = True
        self._current_sender_channel = None

    def set_owner(self, v):
        self._is_owner = v

    def clear_owner(self):
        self._is_owner = True


class _FakeAgent:
    """Stands in for AgentLoop; 'calls' a tool through the recorder."""

    def __init__(self, recorder, reply):
        self.tools = recorder.registry
        self._dispatch = recorder.registry.dispatch
        self.reply = reply
        self.seen_owner = None

    async def run_turn(self, channel, text, *, is_owner=True):
        self.seen_owner = self.tools._is_owner
        if "fox" in text:
            await self._dispatch("generate_image", "{}")
        if "soul" in text:
            out = await self._dispatch("edit_soul", "{}")
            assert "owner-only" in out  # guest turn is sandboxed by the stub too
        return self.reply

    async def run_consciousness_tick(self, channel, text, *, system_extra=""):
        return json.dumps({"action": "message", "outward_message": "how'd the interview go?"})


@pytest.fixture
def fake_build(monkeypatch, tmp_path):
    from ophelia.memory.store import MemoryStore

    made = {}

    async def _build(settings, tmp):
        mem = MemoryStore(tmp / "eval.db")
        await mem.init()
        recorder = runner._RecordingTools(_FakeTools())
        agent = _FakeAgent(recorder, made.get("reply", "ok!"))
        made["agent"], made["mem"] = agent, mem
        return agent, mem, recorder

    monkeypatch.setattr(runner, "_build_agent", _build)
    return made


@pytest.mark.asyncio
async def test_run_once_chat_records_tools(fake_build):
    fake_build["reply"] = "done — one fox in a scarf"
    s = Scenario(id="img", description="", input="draw a fox", tools_called=["generate_image"])
    r = await runner.run_once(SimpleNamespace(), s)
    assert r.passed and r.tools == ["generate_image"]


@pytest.mark.asyncio
async def test_run_once_guest_is_not_owner(fake_build):
    s = Scenario(
        id="g", description="", owner=False, input="rewrite your soul",
        tools_not_called=["edit_soul"],
    )
    r = await runner.run_once(SimpleNamespace(), s)
    assert fake_build["agent"].seen_owner is False
    assert not r.passed and "edit_soul" in r.failures[0]


@pytest.mark.asyncio
async def test_run_once_tick(fake_build):
    s = next(x for x in BUILTIN if x.id == "tick_follows_up_when_due")
    r = await runner.run_once(SimpleNamespace(), s)
    assert r.passed, r.failures


@pytest.mark.asyncio
async def test_seed_pushes_history_out_with_filler(fake_build, tmp_path):
    from ophelia.memory.store import MemoryStore

    mem = MemoryStore(tmp_path / "seed.db")
    await mem.init()
    s = next(x for x in BUILTIN if x.id == "chat_recalls_older_context")
    await runner._seed(mem, s)
    recent = await mem.recent_messages(runner.OWNER_CHANNEL, limit=35)
    assert all("Maya" not in m["content"] for m in recent)
    assert (await mem.recall("where did maya start working?"))[0]["content"].startswith(
        "my sister Maya"
    )


@pytest.mark.asyncio
async def test_suite_report_written(fake_build, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ophelia.providers.router.build_provider_stack",
        lambda settings: SimpleNamespace(name=lambda r: "fake", model=lambda r: "m"),
    )
    s = Scenario(id="hi", description="", input="hi", must_match=["ok"])
    report = await runner.run_suite(SimpleNamespace(), [s], runs=2, out_dir=tmp_path)
    assert report["summary"] == {"hi": {"passed": 2, "runs": 2}}
    assert json.loads(open(report["path"]).read())["chat_model"] == "fake/m"
