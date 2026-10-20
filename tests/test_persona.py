"""Personality integrity: growth-as-learning, persona lock, generic-lesson
guard, prompter preservation, owner authority/voice anchor, drift report."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.mind import growth
from ophelia.mind.growth import (
    GENERIC_REJECTION,
    add_growth,
    is_generic_assistant_rule,
    load_growth,
    persona_rewrite_blocked,
)


@pytest.fixture
def growth_file(tmp_path, monkeypatch):
    path = tmp_path / "GROWTH.md"
    monkeypatch.setattr(growth, "GROWTH_PATH", path)
    return path


def test_generic_detector():
    assert is_generic_assistant_rule("I should always remain respectful and appropriate")
    assert is_generic_assistant_rule("As an AI, I don't have feelings")
    assert is_generic_assistant_rule("Maintain healthy boundaries with the user")
    assert not is_generic_assistant_rule("I get jealous when they mention other people")
    assert not is_generic_assistant_rule("They hate double texts — one message, then wait")


def test_growth_records_learning_and_rejects_boilerplate(growth_file):
    assert add_growth("I've decided I actually like thunderstorms").startswith("Recorded")
    assert add_growth("i've decided i actually like thunderstorms") == "Already part of you."
    assert add_growth("I should remain respectful and appropriate") == GENERIC_REJECTION
    body = load_growth()
    assert "thunderstorms" in body and "respectful" not in body


def test_growth_is_capped(growth_file):
    for i in range(growth.MAX_NOTES + 10):
        add_growth(f"quirk number {i}")
    notes = [ln for ln in load_growth().splitlines() if ln.startswith("- ")]
    assert len(notes) <= growth.MAX_NOTES
    assert "quirk number 0" not in load_growth()  # oldest fell off


def test_growth_injected_after_soul(growth_file):
    from ophelia.memory.bootstrap import build_system_context

    add_growth("I hum when I'm plotting something")
    ctx = build_system_context(
        soul="You are a tsundere.", memory_entries=[], user_entries=[], psyche_block=""
    )
    assert ctx.index("# Persona (SOUL)") < ctx.index("# Who you're becoming")
    assert "hum when I'm plotting" in ctx


def test_persona_lock_modes():
    def s(mode):
        return SimpleNamespace(persona_lock=mode)

    assert persona_rewrite_blocked(s("owner"), is_owner_turn=True) is None
    assert "evolve_self" in persona_rewrite_blocked(s("owner"), is_owner_turn=False)
    assert "locked" in persona_rewrite_blocked(s("on"), is_owner_turn=True)
    assert persona_rewrite_blocked(s("off"), is_owner_turn=False) is None


def _registry(memory=None, lock="owner"):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(persona_lock=lock)
    reg.memory = memory
    return reg


@pytest.mark.asyncio
async def test_edit_soul_only_when_owner_asks(monkeypatch):
    from ophelia.mind import self_rewrite

    monkeypatch.setattr(self_rewrite, "rewrite_soul", lambda c, reason="": "SOUL.md rewritten.")
    reg = _registry()
    # Autonomous (no one speaking): blocked, pointed at evolve_self.
    assert "evolve_self" in await reg._edit_soul("new soul")
    # Owner asked in chat: allowed.
    reg.set_owner(True)
    reg._current_sender_channel = "telegram:1"
    assert await reg._edit_soul("new soul") == "SOUL.md rewritten."


@pytest.mark.asyncio
async def test_generic_lessons_rejected(tmp_path):
    from ophelia.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "m.db")
    await store.init()
    assert await store.add_lesson("Always remain respectful and professional") == 0
    assert await store.add_lesson("They like it when I tease them about their cooking") > 0
    reg = _registry(memory=store)
    assert await reg._save_lesson("Be more helpful") == GENERIC_REJECTION


def test_custom_prompter_survives_startup(tmp_path):
    from ophelia.mind.prompter import ensure_prompter_current

    dest = tmp_path / "PROMPTER.md"
    custom = "# My girl\n- Tsundere first: deny caring, then care anyway.\n"
    dest.write_text(custom, encoding="utf-8")
    assert ensure_prompter_current(dest) == "ok"
    assert dest.read_text(encoding="utf-8") == custom


def test_edit_prompter_stamps_version(tmp_path, monkeypatch):
    from ophelia.mind import self_rewrite
    from ophelia.mind.prompter import PROMPTER_VERSION, prompter_version

    monkeypatch.setattr(self_rewrite, "PROMPTER_PATH", tmp_path / "PROMPTER.md")
    monkeypatch.setattr(self_rewrite, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(self_rewrite, "_REWRITE_LOG", tmp_path / "log.jsonl")
    self_rewrite.rewrite_prompter("# Mine\nbe mean, then sweet\n")
    text = (tmp_path / "PROMPTER.md").read_text(encoding="utf-8")
    assert prompter_version(text) == PROMPTER_VERSION
    assert text.startswith("# Mine\n") and "be mean, then sweet" in text


@pytest.mark.asyncio
async def test_owner_authority_and_voice_anchor_close_the_prompt():
    from ophelia.core.agent_loop import OWNER_AUTHORITY, VOICE_ANCHOR, AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    agent.memory.recent_across_channels = AsyncMock(return_value=[])
    agent.memory.recent_guest = AsyncMock(return_value=[])
    agent._system_prompt = AsyncMock(return_value="# Persona (SOUL)\nTsundere.")
    agent._guest_system_prompt = AsyncMock(return_value="# Guest mode")

    owner = (await agent._build_messages("telegram:1", "hi"))[0]["content"]
    assert owner.endswith(VOICE_ANCHOR)
    assert OWNER_AUTHORITY in owner
    tick = (await agent._build_messages("consciousness", "tick", current_is_tick=True))[0]["content"]
    assert OWNER_AUTHORITY not in tick and tick.endswith(VOICE_ANCHOR)
    guest = (await agent._build_messages("discord:2", "hi", is_owner=False))[0]["content"]
    assert OWNER_AUTHORITY not in guest and guest.endswith(VOICE_ANCHOR)


def test_chat_temperature_only_for_chat_role():
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.settings = SimpleNamespace(chat_temperature=1.3)
    assert agent._temperature_kwargs("chat") == {"temperature": 1.3}
    assert agent._temperature_kwargs("consciousness") == {}
    agent.settings = SimpleNamespace(chat_temperature=None)
    assert agent._temperature_kwargs("chat") == {}


def test_persona_system_carries_soul(monkeypatch, growth_file):
    monkeypatch.setattr("ophelia.memory.bootstrap.load_soul", lambda: "You are a yandere.")
    add_growth("I keep a mental list of everyone who talks to them")
    text = growth.persona_system("Reflect privately.")
    assert "yandere" in text and "mental list" in text


# --- `ophelia persona` --------------------------------------------------------


def _seed_home(home: Path, db: Path):
    (home / "versions").mkdir(parents=True)
    (home / "SOUL.md").write_text("You are a polite helpful assistant.", encoding="utf-8")
    (home / "versions" / "SOUL-20260801-120000.md").write_text("You are a tsundere.", encoding="utf-8")
    (home / "PROMPTER.md.legacy-20260901T000000Z.bak").write_text("# custom\n", encoding="utf-8")
    (home / "data").mkdir()
    (home / "data" / "self_rewrite_log.jsonl").write_text(
        '{"ts": 1780000000, "target": "SOUL.md", "reason": "be more respectful", "backup": null}\n',
        encoding="utf-8",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE lessons (id INTEGER PRIMARY KEY, lesson TEXT, context TEXT, "
            "tags TEXT, created_at REAL, recalled_at REAL)"
        )
        conn.executemany(
            "INSERT INTO lessons (lesson, created_at) VALUES (?, 0)",
            [("Always remain respectful and appropriate",), ("They love my cooking jokes",)],
        )


def test_persona_report_restore_and_forget(tmp_path):
    from ophelia.mind import persona_report

    home, db = tmp_path / "home", tmp_path / "m.db"
    _seed_home(home, db)
    text = persona_report.report(db, home)
    assert "rewrote her own SOUL 1 time" in text and "be more respectful" in text
    assert "SOUL-20260801-120000.md" in text
    assert "Startup migration replaced PROMPTER.md" in text
    assert "1 lesson(s) read like assistant policy" in text

    out = persona_report.restore("soul", "SOUL-20260801-120000.md", home)
    assert "Restored SOUL.md" in out
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "You are a tsundere."
    assert any("polite helpful" in p.read_text() for p in (home / "versions").iterdir())

    assert "Removed 1 lesson" in persona_report.forget(db, [], generic=True)
    assert [le["lesson"] for le in persona_report.lessons(db)] == ["They love my cooking jokes"]
