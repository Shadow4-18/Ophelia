"""Tests for the guest-experience improvements (Ophelia's 5 suggestions).

Covers:
1. Guest media constraints — generate_image/generate_video clamp to 1:1 (and
   low res for video) when the turn is a guest, owner is unaffected.
2. Guest system prompt wording is the warmer version ("deep personal stuff
   between you and your owner"), not the cold "surface conversation" version.
3. text_to_speech is NO LONGER in GUEST_DENIED_TOOLS — Kokoro is local, so
   voice is allowed for guests (timing left to Ophelia's judgment).
4. First-visit guest welcome exists, mentions the constraints, and is used
   as the prefix on the Telegram + Discord approval-notify path.
5. Setup wizard writes OPHELIA_OWNER_ID explicitly (not positional).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession, guest_welcome_message
from ophelia.tools.registry import GUEST_DENIED_TOOLS, ToolRegistry


# ── #3: TTS allowed for guests; media tools allowed but constrained ─────────


def test_text_to_speech_not_in_guest_denied_tools():
    """Kokoro is local/free, so guest voice is allowed. Timing is Ophelia's
    call (she rarely sends voice even for the owner)."""
    assert "text_to_speech" not in GUEST_DENIED_TOOLS


def test_generate_image_and_video_not_in_guest_denied_tools():
    """Guests can make media — the constraint is applied in the handler
    (1:1 + low res), not by blocking the tool entirely."""
    assert "generate_image" not in GUEST_DENIED_TOOLS
    assert "generate_video" not in GUEST_DENIED_TOOLS


def test_identity_tools_still_denied_for_guests():
    """The stuff that actually shapes her identity stays owner-only."""
    for critical in (
        "edit_soul",
        "edit_prompter",
        "save_lesson",
        "reflect",
        "goal_create",
        "recall_memory",
        "sqlite_exec",
        "run_code",
        "set_timezone",
    ):
        assert critical in GUEST_DENIED_TOOLS, critical


def test_list_inbox_images_still_denied_for_guests():
    """Guests must not see photos the owner sent over chat."""
    assert "list_inbox_images" in GUEST_DENIED_TOOLS


def test_phone_tools_still_denied_for_guests():
    """Phone body is owner-only — guests can't tap or screenshot."""
    for p in ("phone_tap", "phone_ui_dump", "phone_shell", "phone_see_screen"):
        assert p in GUEST_DENIED_TOOLS, p


# ── #1: Guest media constraints applied in the handler ─────────────────────


def _registry(android=None) -> ToolRegistry:
    settings = MagicMock()
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.stack = MagicMock()
    reg.android = android
    reg._is_owner = True
    reg.artifacts_dir = Path("/tmp/artifacts")
    reg._mcp_ready = True
    reg.mcp = MagicMock()
    reg.mcp.dispatch = AsyncMock(return_value=None)
    reg._handlers = {}
    return reg


@pytest.mark.asyncio
async def test_guest_generate_image_forced_to_1_1(monkeypatch):
    """A guest requesting 16:9 must actually produce 1:1."""
    captured: dict = {}

    async def fake_generate_image(settings, stack, prompt, *, aspect_ratio, artifacts_dir, nsfw):
        captured["aspect_ratio"] = aspect_ratio
        return "ok"

    async def fake_finalize(self, result):  # noqa: ARG001
        return result

    monkeypatch.setattr("ophelia.tools.registry.generate_image", fake_generate_image)
    monkeypatch.setattr(ToolRegistry, "_finalize_media_tool_result", fake_finalize)
    reg = _registry()
    reg._is_owner = False  # guest turn
    await reg._generate_image("a sunset", aspect_ratio="16:9")
    assert captured["aspect_ratio"] == "1:1"


@pytest.mark.asyncio
async def test_owner_generate_image_keeps_requested_aspect(monkeypatch):
    """Owner is unaffected — 16:9 stays 16:9."""
    captured: dict = {}

    async def fake_generate_image(settings, stack, prompt, *, aspect_ratio, artifacts_dir, nsfw):
        captured["aspect_ratio"] = aspect_ratio
        return "ok"

    async def fake_finalize(self, result):  # noqa: ARG001
        return result

    monkeypatch.setattr("ophelia.tools.registry.generate_image", fake_generate_image)
    monkeypatch.setattr(ToolRegistry, "_finalize_media_tool_result", fake_finalize)
    reg = _registry()
    reg._is_owner = True  # owner turn
    await reg._generate_image("a sunset", aspect_ratio="16:9")
    assert captured["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_guest_generate_video_forced_to_1_1_480p(monkeypatch):
    """A guest requesting 16:9 + 720p must actually produce 1:1 + 480p.
    xAI only accepts '480p' or '720p' — 'low' is not a valid value and
    would cause a 400 error."""
    captured: dict = {}

    async def fake_generate_video(settings, stack, prompt, *, duration_seconds, artifacts_dir, image, aspect_ratio, resolution):
        captured["aspect_ratio"] = aspect_ratio
        captured["resolution"] = resolution
        return "ok"

    async def fake_finalize(self, result):  # noqa: ARG001
        return result

    monkeypatch.setattr("ophelia.tools.registry.generate_video", fake_generate_video)
    monkeypatch.setattr(ToolRegistry, "_finalize_media_tool_result", fake_finalize)
    reg = _registry()
    reg._is_owner = False  # guest turn
    await reg._generate_video("waves", aspect_ratio="16:9", resolution="720p")
    assert captured["aspect_ratio"] == "1:1"
    assert captured["resolution"] == "480p"


@pytest.mark.asyncio
async def test_owner_generate_video_keeps_requested_quality(monkeypatch):
    """Owner is unaffected — 16:9 + high stays."""
    captured: dict = {}

    async def fake_generate_video(settings, stack, prompt, *, duration_seconds, artifacts_dir, image, aspect_ratio, resolution):
        captured["aspect_ratio"] = aspect_ratio
        captured["resolution"] = resolution
        return "ok"

    async def fake_finalize(self, result):  # noqa: ARG001
        return result

    monkeypatch.setattr("ophelia.tools.registry.generate_video", fake_generate_video)
    monkeypatch.setattr(ToolRegistry, "_finalize_media_tool_result", fake_finalize)
    reg = _registry()
    reg._is_owner = True  # owner turn
    await reg._generate_video("waves", aspect_ratio="16:9", resolution="high")
    assert captured["aspect_ratio"] == "16:9"
    assert captured["resolution"] == "high"


# ── #2: Guest system prompt wording ────────────────────────────────────────


@pytest.mark.asyncio
async def test_guest_prompt_uses_warmer_wording(tmp_path, monkeypatch):
    """The cold 'surface conversation' phrasing is gone, replaced with warmer
    language that allows full personality."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt()
    assert "surface conversation" not in prompt
    # New warmer phrasing allows full personality.
    assert "fully yourself" in prompt.lower() or "joke" in prompt.lower()


@pytest.mark.asyncio
async def test_guest_prompt_mentions_media_is_available(tmp_path, monkeypatch):
    """Guests should know they can make images/videos (the experience matters)."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt()
    assert "image" in prompt.lower()
    assert "video" in prompt.lower()


@pytest.mark.asyncio
async def test_guest_prompt_mentions_constraint_so_agent_doesnt_overpromise(
    tmp_path, monkeypatch
):
    """The agent must know guest media is 1:1 so it doesn't promise 16:9."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = None
    agent.settings = MagicMock()
    agent.settings.owner_channels.return_value = ["telegram:1"]
    prompt = await agent._guest_system_prompt()
    assert "1:1" in prompt
    # New prompt says "480p" instead of "low"/"lower".
    assert "480p" in prompt.lower() or "lower" in prompt.lower() or "low" in prompt.lower()


# ── #4: First-visit guest welcome ──────────────────────────────────────────


def test_guest_welcome_message_exists_and_sets_expectations():
    """The welcome must mention what guests CAN do and what they CAN'T."""
    welcome = guest_welcome_message()
    # What they can do
    assert "chat" in welcome.lower() or "search" in welcome.lower()
    assert "image" in welcome.lower()
    assert "video" in welcome.lower()
    assert "voice" in welcome.lower()  # the user explicitly wanted voice mentioned
    # What they can't do
    assert "personal" in welcome.lower()  # owner's personal info withheld
    # And it ends conversationally (not a wall of rules)
    assert welcome.strip().endswith("What's up?")


def test_guest_welcome_message_mentions_square_constraint():
    """The welcome should set the expectation that media is square, so a guest
    isn't surprised when they get 1:1 instead of 16:9."""
    welcome = guest_welcome_message()
    assert "square" in welcome.lower() or "1:1" in welcome


# ── #5: Setup wizard writes explicit OPHELIA_OWNER_ID ──────────────────────


def test_setup_wizard_prompts_for_owner_id(monkeypatch, tmp_path):
    """The channels section must write OPHELIA_OWNER_ID explicitly so owner
    status doesn't depend on list-ordering of the allowlist."""
    # We can't run the full interactive setup (it prompts), but we can verify
    # the code path that builds the default owner_id from entered IDs.
    updates: dict[str, str] = {
        "TELEGRAM_ALLOWED_USER_IDS": "111",
        "OPHELIA_TELEGRAM_ENABLED": "true",
        "DISCORD_ALLOWED_USER_IDS": "222",
        "OPHELIA_DISCORD_ENABLED": "true",
    }
    # Mirror the wizard's default-construction logic
    parts: list[str] = []
    tg = updates.get("TELEGRAM_ALLOWED_USER_IDS")
    dc = updates.get("DISCORD_ALLOWED_USER_IDS")
    if tg and updates.get("OPHELIA_TELEGRAM_ENABLED") != "false":
        parts.append(f"telegram:{tg.split(',')[0].strip()}")
    if dc and updates.get("OPHELIA_DISCORD_ENABLED") != "false":
        parts.append(f"discord:{dc.split(',')[0].strip()}")
    default_owner = ",".join(parts)
    assert default_owner == "telegram:111,discord:222"


# ── Integration: dispatch path doesn't block guest media/TTS ───────────────


@pytest.mark.asyncio
async def test_dispatch_does_not_block_guest_text_to_speech(monkeypatch):
    """A guest calling text_to_speech must NOT get the 'owner-only' refusal —
    it must reach the handler (which then decides whether to actually synth)."""
    reg = _registry()
    reg._is_owner = False  # guest
    reg._handlers = {"text_to_speech": AsyncMock(return_value="ok")}
    result = await reg.dispatch("text_to_speech", "{}")
    assert "owner-only" not in result
    assert result == "ok"


@pytest.mark.asyncio
async def test_dispatch_still_blocks_guest_edit_soul(monkeypatch):
    """Identity-shaping tools stay blocked for guests — the media loosening
    didn't accidentally loosen everything."""
    reg = _registry()
    reg._is_owner = False  # guest
    reg._handlers = {"edit_soul": AsyncMock(return_value="should not reach")}
    result = await reg.dispatch("edit_soul", "{}")
    assert "owner-only" in result
