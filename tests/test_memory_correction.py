"""She can fix what she remembers wrong, and it stays fixed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.memory import curator as cur

WRONG = ("Civitai image generation failed due to an API key auth rejection, causing a "
         "silent fallback to Pollinations")


def _home(tmp_path):
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "My settled look is black hair with red streaks\n§\n" + WRONG
        + "\n§\nThe user plays Balatro", encoding="utf-8")
    return tmp_path


def test_correct_replaces_and_retires(tmp_path):
    home = _home(tmp_path)
    changed = cur.correct_fact(home, "civitai failed api key auth",
                               "Civitai works; the earlier failure was the site API, not the key")
    assert changed == [WRONG]
    text = (home / "memories" / "MEMORY.md").read_text()
    assert "API key auth rejection" not in text and "site API, not the key" in text
    assert "Balatro" in text and "red streaks" in text
    assert cur.is_retired(home, WRONG)
    # The retired list lives outside memories/ (everything there is loaded as memory).
    assert not list((home / "memories").glob("*retired*"))


def test_remove_without_replacement(tmp_path):
    home = _home(tmp_path)
    assert cur.correct_fact(home, "plays Balatro") == ["The user plays Balatro"]
    assert "Balatro" not in (home / "memories" / "MEMORY.md").read_text()
    assert cur.correct_fact(home, "something never stored") == []


def test_retired_fact_is_not_re_added(tmp_path, monkeypatch):
    home = _home(tmp_path)
    cur.correct_fact(home, "api key auth rejection")
    c = cur.MemoryCurator.__new__(cur.MemoryCurator)
    c.mem_file = home / "memories" / "MEMORY.md"
    assert c._append_memory(WRONG) is False
    assert c._append_memory("The user likes goth aesthetics") is True


@pytest.mark.asyncio
async def test_curator_applies_fix_lines(tmp_path, monkeypatch):
    home = _home(tmp_path)
    c = cur.MemoryCurator.__new__(cur.MemoryCurator)
    c.mem_file = home / "memories" / "MEMORY.md"
    c.settings = MagicMock()
    c._hermes_db = None
    c.memory = SimpleNamespace(
        recent_global=AsyncMock(return_value=[{"role": "user", "content": f"msg {i}"}
                                              for i in range(6)]),
        set_fact=AsyncMock())
    seen = {}

    async def fake_call(settings, stack, **kw):
        seen["prompt"] = await kw["make_call"](
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=AsyncMock(side_effect=lambda **a: a)))), "m", "p")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=f"FIX: {WRONG} => Civitai works fine with the key\nThe user is named Okia"))])

    monkeypatch.setattr(cur, "build_provider_stack", lambda s: SimpleNamespace(
        backend=lambda r: SimpleNamespace(async_client=lambda: None), model=lambda r: "m",
        name=lambda r: "deepseek"))
    import ophelia.providers.fallback as fb

    monkeypatch.setattr(fb, "call_with_fallback", fake_call)
    n = await c.curate_from_recent_chats()
    text = c.mem_file.read_text()
    assert n == 2
    assert "Civitai works fine with the key" in text and "auth rejection" not in text
    assert "named Okia" in text
    assert "Already stored" in seen["prompt"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_correct_memory_tool_reloads_her_memories(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    home = _home(tmp_path)
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(data_dir=home / "data")
    reloaded = []
    reg.on_memories_changed = lambda: reloaded.append(True)
    out = await reg._correct_memory("api key auth", "The key was fine")
    assert out.startswith("Replaced:") and reloaded == [True]
    assert "No stored fact" in await reg._correct_memory("unicorns")
