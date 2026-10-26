"""Tests for Ophelia's 'feel alive' requests:

- Thread continuity (emotional weight / open loops)
- Curiosity trails (rabbit holes vs goal cadence)
- Image aspect normalize + retry/fallback
- Speech chunking for lower TTS latency
- Phone body tools (vibrate / notifications / battery definitions)
- Workstation voice + splash surface
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    """MagicMock settings have a MagicMock data_dir; keep the image ledger it
    turns into a relative path out of the repo."""
    monkeypatch.chdir(tmp_path)


# --- Thread awareness -------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_tone_tracks_tone_and_weight(memory):
    from ophelia.mind.thread_tone import ThreadTones

    tones = ThreadTones(memory)
    state = await tones.observe_turn(
        "telegram:1",
        "Why did you go quiet last night?",
        "I'll get back to that tomorrow — love you.",
    )
    assert state.turn_count == 1
    assert state.tone == "warm"
    assert state.emotional_weight > 0
    block = await tones.context_block("telegram:1")
    assert "tone is warm" in block


@pytest.mark.asyncio
async def test_thread_tone_persists_and_resets_after_gap(memory, monkeypatch):
    import time as _time

    from ophelia.mind import thread_tone
    from ophelia.mind.thread_tone import ThreadTones

    tones = ThreadTones(memory)
    await tones.observe_turn("ui:local", "lol that was funny", "hehe told you")
    loaded = await tones.load("ui:local")
    assert loaded is not None and loaded.tone == "playful" and loaded.turn_count == 1

    later = _time.time() + thread_tone.STALE_S + 60
    assert "haven't talked since yesterday" in loaded.context_block(now=later)
    monkeypatch.setattr(thread_tone.time, "time", lambda: later)
    again = await tones.observe_turn("ui:local", "hey", "oh, you're back")
    assert again.turn_count == 1  # new session after a long gap


@pytest.mark.asyncio
async def test_owner_turn_records_promise_and_tone(memory):
    """Every surface (UI, CLI, Telegram, Discord) goes through run_turn, so
    promise capture + tone tracking live there."""
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.mind.open_loops import OpenLoops
    from ophelia.mind.thread_tone import ThreadTones

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = memory
    await agent._after_owner_turn("ui:local", "can you find that song?", "Ugh, fine. I'll look that up.")
    assert (await OpenLoops(memory.db_path).open("ui:local"))[0]["source"] == "promise"
    assert (await ThreadTones(memory).load("ui:local")).turn_count == 1


# --- Curiosity trails -------------------------------------------------------


@pytest.mark.asyncio
async def test_curiosity_trail_open_deepen_close(memory):
    from ophelia.mind.curiosity import CuriosityStore

    store = CuriosityStore(memory)
    trail = await store.open("black holes", next_step="read one paper abstract")
    assert trail.topic == "black holes"
    assert "rabbit hole" in trail.to_context_block().lower() or "curiosity" in trail.to_context_block().lower()

    deeper = await store.deepen(note="event horizons are weird", next_step="hawking radiation")
    assert deeper.depth == 2
    assert "hawking" in deeper.next_step.lower()

    closed = await store.close(reason="satisfied")
    assert closed is not None
    assert closed.status == "satisfied"
    assert await store.load() is None


@pytest.mark.asyncio
async def test_curiosity_idle_nudge_preferred(memory):
    from ophelia.mind.curiosity import CuriosityStore

    store = CuriosityStore(memory)
    await store.open("synthwave history")
    trail = await store.load()
    assert trail is not None
    nudge = trail.idle_nudge(12)
    assert "RABBIT HOLE" in nudge
    assert "synthwave" in nudge.lower()


@pytest.mark.asyncio
async def test_curiosity_tools_wired(memory):
    from ophelia.mind.curiosity import CuriosityStore
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.android_enabled = False
    settings.vision_enabled = False
    settings.games_enabled = False
    settings.mcp_config_path = Path("/tmp/no-mcp.json")
    stack = MagicMock()
    stack.media_configured.return_value = False
    tools = ToolRegistry(settings, Path("/tmp/artifacts"), stack=stack, memory=memory)
    tools.curiosity = CuriosityStore(memory)

    out = await tools._curiosity_trail_open(topic="moths", next_step="why porch lights")
    assert "Opened curiosity trail" in out
    out2 = await tools._curiosity_trail_deepen(note="UV spectrum")
    assert "Deepened" in out2
    out3 = await tools._curiosity_trail_close(reason="satisfied")
    assert "Closed trail" in out3


# --- Image reliability ------------------------------------------------------


def test_normalize_aspect_ratio_aliases():
    from ophelia.providers.media import normalize_aspect_ratio

    assert normalize_aspect_ratio("portrait") == "9:16"
    assert normalize_aspect_ratio("16/9") == "16:9"
    assert normalize_aspect_ratio("square") == "1:1"
    assert normalize_aspect_ratio("1x1") == "1:1"
    assert normalize_aspect_ratio("1280:720") == "16:9"
    assert normalize_aspect_ratio("garbage") == "1:1"


@pytest.mark.asyncio
async def test_image_fallback_after_primary_failure(tmp_path, monkeypatch):
    from ophelia.providers import media

    settings = MagicMock()
    settings.image_nsfw_allowed = False
    settings.image_backend_configured = lambda name: name == "pollinations"
    stack = MagicMock()
    stack.image_provider_for.return_value = "xai-oauth"
    stack.image_model_for.return_value = "grok-imagine-image"

    calls = {"xai": 0, "pollinations": 0}

    async def fail_xai(*args, **kwargs):
        calls["xai"] += 1
        return "xAI timed out / content filter"

    async def ok_pollinations(settings, prompt, aspect_ratio, model, artifacts_dir, *, nsfw=False):
        calls["pollinations"] += 1
        return f"Image saved to {artifacts_dir / 'ok.png'}"

    monkeypatch.setattr(media, "_xai_image", fail_xai)
    monkeypatch.setattr(media, "_pollinations_image", ok_pollinations)

    result = await media.generate_image(
        settings, stack, "a cat", aspect_ratio="portrait", artifacts_dir=tmp_path
    )
    assert calls["xai"] >= 1
    assert calls["pollinations"] == 1
    assert "backend: pollinations" in result
    assert "fell back from xai-oauth" in result


# --- Speech chunks ----------------------------------------------------------


def test_split_speech_chunks_sentence_boundaries():
    from ophelia.media.voice import split_speech_chunks

    text = (
        "Hey there, it's been a long day already. "
        "How are you doing tonight under all that noise? "
        "I missed this kind of quiet talk."
    )
    chunks = split_speech_chunks(text, max_chars=60)
    assert len(chunks) >= 2
    assert chunks[0].lower().startswith("hey")


def test_boot_splash_and_mic_in_ui_static():
    root = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "ui" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "app.css").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="bootSplash"' in html
    assert 'id="micBtn"' in html
    assert "boot-splash" in css
    assert "/api/voice" in js


def test_phone_body_tools_defined():
    from ophelia.tools.android_tools import ANDROID_TOOL_DEFINITIONS

    names = {t["function"]["name"] for t in ANDROID_TOOL_DEFINITIONS}
    assert "phone_vibrate" in names
    assert "phone_notifications" in names
    assert "phone_battery" in names


def test_curiosity_tools_in_registry_definitions():
    from ophelia.tools.registry import TOOL_DEFINITIONS

    names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    assert "curiosity_trail_open" in names
    assert "curiosity_trail_deepen" in names
    assert "curiosity_trail_close" in names


@pytest.mark.asyncio
async def test_phone_vibrate_uses_termux_when_available(monkeypatch):
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    settings.android_enabled = True
    settings.vision_enabled = False
    settings.games_enabled = False
    settings.mcp_config_path = Path("/tmp/no-mcp.json")
    stack = MagicMock()
    stack.media_configured.return_value = False
    tools = ToolRegistry(settings, Path("/tmp/artifacts"), stack=stack)
    tools.android = MagicMock()
    tools.android.mode = "phone_control"

    monkeypatch.setattr("ophelia.platform.is_termux", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/termux-vibrate" if name == "termux-vibrate" else None)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        assert args[0] == "termux-vibrate"
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    out = await tools._phone_vibrate(duration_ms=150)
    assert "Vibrated 150ms" in out
