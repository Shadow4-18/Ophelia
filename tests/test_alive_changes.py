"""Tests for the 'more alive' changes:

- Wave 1a: ambient commentary no longer instructs "reply exactly: SKIP"
- Wave 1b: PROMPTER.example.md rewritten toward tendencies + contradiction tolerance
- Wave 1c: /revoke command removes guest from allowlist + marks denied
- Wave 2a: PsycheState.drift() — continuous mood nudge with organic noise
- Wave 2c: fast inner-tick mode skips LLM when pressure is low
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure full import chain loads (avoid circular imports).
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.mind.psyche import Mood, PsycheState


# --- Wave 1a: ambient commentary prompt no longer says "reply exactly: SKIP" ---


def test_ambient_commentary_no_skip_instruction():
    """The literal 'reply exactly: SKIP' instruction caused Ophelia to
    mechanically reproduce SKIP as a compliance token. It must be gone."""
    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "mind" / "ambient_commentary.py"
    text = src.read_text(encoding="utf-8")
    assert "reply exactly: SKIP" not in text
    assert "reply exactly" not in text
    # The rigid [Ambient screen glance] template label should also be gone.
    assert "[Ambient screen glance]" not in text


def test_ambient_commentary_has_silence_is_fine_language():
    """The new prompt should explicitly make silence a valid, unannotated
    choice — not a compliance token."""
    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "mind" / "ambient_commentary.py"
    text = src.read_text(encoding="utf-8").lower()
    assert "silence" in text


# --- Wave 1b: PROMPTER.example.md rewritten toward tendencies ---


def test_prompter_example_has_contradiction_tolerance():
    """The new PROMPTER should explicitly permit contradiction."""
    src = Path(__file__).resolve().parents[1] / "PROMPTER.example.md"
    text = src.read_text(encoding="utf-8").lower()
    assert "contradict" in text


def test_prompter_example_conversation_presence_and_tick_heartbeat():
    """PROMPTER: presence in conversation; quiet heartbeat on autonomous ticks."""
    src = Path(__file__).resolve().parents[1] / "PROMPTER.example.md"
    text = src.read_text(encoding="utf-8").lower()
    assert "presence is the default" in text
    assert "heartbeat, not a summons" in text
    assert "output is the default" not in text


def test_prompter_example_no_skip_token_instruction():
    """The new PROMPTER should NOT instruct producing SKIP tokens."""
    src = Path(__file__).resolve().parents[1] / "PROMPTER.example.md"
    text = src.read_text(encoding="utf-8")
    # "SKIP" may appear in the context of "don't say SKIP" — that's fine.
    # But there should be no instruction to *produce* SKIP.
    assert "reply exactly: SKIP" not in text
    assert 'reply "SKIP"' not in text
    assert "reply exactly" not in text


def test_base_prompt_has_presence_language():
    """BASE_PROMPT: contradiction ok; ticks are a heartbeat; no SKIP tokens."""
    from ophelia.core.agent_loop import BASE_PROMPT

    low = BASE_PROMPT.lower()
    assert "contradict" in low
    assert "heartbeat" in low
    assert "stillness" in low
    # Should explicitly call out compliance tokens like SKIP as unwanted.
    assert "skip" in low or "compliance token" in low


def test_base_prompt_requires_real_tool_calls_not_narration():
    """BASE_PROMPT must tell her to emit actual tool calls instead of
    narrating '*fires the tool*' in prose. Without this, she claims to
    generate images but never calls generate_image, so nothing is
    actually produced."""
    from ophelia.core.agent_loop import BASE_PROMPT

    low = BASE_PROMPT.lower()
    # Must have a section about tools being real actions.
    assert "tool" in low
    assert "narrat" in low or "do not write" in low
    # Must explicitly say prose doesn't do the work — the call does.
    assert "tool call" in low
    assert "generate_image" in low or "generate an image" in low


def test_default_prompter_requires_real_tool_calls():
    """DEFAULT_PROMPTER must also reinforce that tools are real calls,
    not narration — so the policy layer backs up the base prompt."""
    from ophelia.mind.prompter import DEFAULT_PROMPTER

    low = DEFAULT_PROMPTER.lower()
    assert "tool" in low
    assert "narrat" in low or "do not write" in low


# --- Wave 1c: /revoke command ---


def test_remove_user_from_allowlist_removes_present_user(tmp_path, monkeypatch):
    """remove_user_from_allowlist should drop the user from the live settings
    and persist to the .env file."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.channels.guest_approval import (
        append_user_to_allowlist,
        remove_user_from_allowlist,
    )

    settings = MagicMock()
    settings.telegram_allowed_user_ids = "100,200"

    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_ALLOWED_USER_IDS=100,200\n", encoding="utf-8")

    removed = remove_user_from_allowlist(settings, "telegram", 200, env_path=env_path)
    assert removed is True
    # Live settings updated.
    assert "200" not in settings.telegram_allowed_user_ids
    assert "100" in settings.telegram_allowed_user_ids
    # Persisted to .env.
    persisted = env_path.read_text(encoding="utf-8")
    assert "200" not in persisted.split("=", 1)[1]
    assert "100" in persisted


def test_remove_user_from_allowlist_returns_false_for_absent(tmp_path, monkeypatch):
    """If the user isn't in the allowlist, return False (no change)."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    from ophelia.channels.guest_approval import remove_user_from_allowlist

    settings = MagicMock()
    settings.telegram_allowed_user_ids = "100"
    env_path = tmp_path / ".env"
    removed = remove_user_from_allowlist(settings, "telegram", 999, env_path=env_path)
    assert removed is False


@pytest.mark.asyncio
async def test_cmd_revoke_blocks_guest(tmp_path, monkeypatch):
    """/revoke should remove the guest from the allowlist and mark them denied."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path / "ophelia_home"))
    from ophelia.channels.session import ChannelSession

    session = ChannelSession.__new__(ChannelSession)
    session.agent = MagicMock()
    session.agent.settings = MagicMock()
    session.agent.settings.is_owner_channel.return_value = False
    # Simulate the guest being in the allowlist.
    session.agent.settings.telegram_allowed_user_ids = "111,222"
    session.memory = MagicMock()
    session.hub = None

    # resolve_guest_target returns (platform, user_id).
    with patch(
        "ophelia.memory.guests.resolve_guest_target",
        new=AsyncMock(return_value=("telegram", 222)),
    ):
        with patch(
            "ophelia.channels.guest_approval.remove_user_from_allowlist",
            return_value=True,
        ) as mock_remove:
            guest_approvals = MagicMock()
            replied: list[str] = []

            async def reply(t: str) -> None:
                replied.append(t)

            await session.cmd_revoke(
                ["222"],
                reply,
                guest_approvals=guest_approvals,
            )

            mock_remove.assert_called_once()
            guest_approvals.set_status.assert_called_once_with("telegram", 222, "denied")
            assert any("222" in r and "blocked" in r.lower() for r in replied)


@pytest.mark.asyncio
async def test_cmd_revoke_refuses_to_revoke_owner(tmp_path, monkeypatch):
    """/revoke must refuse to revoke the owner (don't lock yourself out)."""
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path / "ophelia_home"))
    from ophelia.channels.session import ChannelSession

    session = ChannelSession.__new__(ChannelSession)
    session.agent = MagicMock()
    session.agent.settings = MagicMock()
    session.agent.settings.is_owner_channel.return_value = True
    session.agent.settings.telegram_allowed_user_ids = "111"
    session.memory = MagicMock()
    session.hub = None

    with patch(
        "ophelia.memory.guests.resolve_guest_target",
        new=AsyncMock(return_value=("telegram", 111)),
    ):
        replied: list[str] = []

        async def reply(t: str) -> None:
            replied.append(t)

        await session.cmd_revoke(
            ["111"],
            reply,
            guest_approvals=MagicMock(),
        )
        assert any("can't revoke the owner" in r.lower() for r in replied)


# --- Wave 2a: PsycheState.drift() ---


def test_drift_moves_mood_toward_baseline():
    """drift() should pull mood toward baseline (homeostasis)."""
    psyche = PsycheState(
        mood=Mood(
            valence=-0.8,
            arousal=0.9,
            baseline_valence=0.15,
            baseline_arousal=0.3,
        )
    )
    initial_v = psyche.mood.valence
    initial_a = psyche.mood.arousal
    # Simulate several drift calls (as the drift loop would do).
    for _ in range(20):
        psyche.drift(5.0)
    # Should have moved toward baseline.
    assert psyche.mood.valence > initial_v
    assert psyche.mood.arousal < initial_a


def test_drift_does_not_overshoot_baseline_dramatically():
    """drift() should be gentle — after many calls, still near baseline, not
    pegged to extremes."""
    psyche = PsycheState(
        mood=Mood(
            valence=0.0,
            arousal=0.3,
            baseline_valence=0.15,
            baseline_arousal=0.3,
        )
    )
    for _ in range(100):
        psyche.drift(5.0)
    # Should be near baseline, not at +1 or -1.
    assert -0.2 < psyche.mood.valence < 0.5
    assert 0.1 < psyche.mood.arousal < 0.6


def test_drift_clamps_to_valid_range():
    """Even with many drift calls, mood must stay in [-1, 1] / [0, 1]."""
    psyche = PsycheState(
        mood=Mood(valence=0.99, arousal=0.99, baseline_valence=0.99, baseline_arousal=0.99)
    )
    for _ in range(200):
        psyche.drift(5.0)
    assert -1.0 <= psyche.mood.valence <= 1.0
    assert 0.0 <= psyche.mood.arousal <= 1.0


def test_drift_with_zero_dt_is_noop():
    """drift(0) should be a no-op."""
    psyche = PsycheState(
        mood=Mood(valence=0.5, arousal=0.5, baseline_valence=0.0, baseline_arousal=0.0)
    )
    before_v = psyche.mood.valence
    psyche.drift(0.0)
    assert psyche.mood.valence == before_v


# --- Wave 2b: drift loop is decoupled from LLM tick ---


def test_consciousness_loop_has_drift_task():
    """The ConsciousnessLoop should start a separate drift task in run()."""
    import inspect

    from ophelia.mind.consciousness import ConsciousnessLoop

    src = inspect.getsource(ConsciousnessLoop.run)
    assert "_drift_loop" in src
    assert "create_task" in src


def test_consciousness_stop_cancels_drift_task():
    """stop() should cancel the drift task."""
    import inspect

    from ophelia.mind.consciousness import ConsciousnessLoop

    src = inspect.getsource(ConsciousnessLoop.stop)
    assert "_drift_task" in src
    assert "cancel" in src


# --- Wave 2c: fast inner-tick mode skips LLM when pressure is low ---


def test_fast_tick_skip_logic_present():
    """The _tick method should have a fast-tick-skip path for low pressure."""
    import inspect

    from ophelia.mind.consciousness import ConsciousnessLoop

    src = inspect.getsource(ConsciousnessLoop._tick)
    assert "fast_tick_skip" in src
    assert "pressure" in src
    # Heartbeat band is wider than the old 0.22 summons threshold.
    assert "0.35" in src


def test_consciousness_prompt_is_heartbeat_not_summons():
    from ophelia.mind.consciousness import CONSCIOUSNESS_PROMPT

    assert "heartbeat" in CONSCIOUSNESS_PROMPT.lower()
    assert "not a summons" in CONSCIOUSNESS_PROMPT.lower() or "not a demand" in CONSCIOUSNESS_PROMPT.lower()
    assert "presence is the default" not in CONSCIOUSNESS_PROMPT
    assert "stillness" in CONSCIOUSNESS_PROMPT.lower()


def test_prompter_marks_ticks_as_heartbeat():
    from ophelia.mind.prompter import DEFAULT_PROMPTER, PROMPTER_VERSION

    assert PROMPTER_VERSION >= 3
    assert "heartbeat, not a summons" in DEFAULT_PROMPTER.lower()
    assert "Output is the default" not in DEFAULT_PROMPTER
