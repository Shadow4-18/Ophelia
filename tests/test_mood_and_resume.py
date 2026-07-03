"""Tests for mood→behavior knobs (Tier A #5) and autonomous resume (Tier C #14).

These pin the contracts added in the Tier A/C roadmap so a refactor of
mood_knobs or the agent-loop resume stash doesn't silently change how she
sounds or break long autonomous sessions.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_hyped_mood_speeds_up_and_shortens_bursts():
    from ophelia.mind.mood_behavior import mood_knobs
    from ophelia.mind.psyche import Mood, PsycheState

    psyche = PsycheState(mood=Mood(valence=0.6, arousal=0.85, label="hyped"))
    k = mood_knobs(psyche)
    assert k.tts_speed > 1.0
    assert k.burst_max_chars <= 200
    assert k.pace_tag == "hyped"


async def test_reflective_mood_slows_and_lengthens():
    from ophelia.mind.mood_behavior import mood_knobs
    from ophelia.mind.psyche import Mood, PsycheState

    psyche = PsycheState(mood=Mood(valence=0.1, arousal=0.15, label="reflective"))
    k = mood_knobs(psyche)
    assert k.tts_speed < 1.0
    assert k.burst_max_chars >= 500
    assert k.pace_tag == "reflective"


async def test_negative_valence_raises_outreach_threshold():
    """Low mood should make her less likely to reach out, not more."""
    from ophelia.mind.mood_behavior import mood_knobs
    from ophelia.mind.psyche import Mood, PsycheState

    low = PsycheState(mood=Mood(valence=-0.6, arousal=0.3, label="low"))
    neutral = PsycheState(mood=Mood(valence=0.0, arousal=0.3, label="neutral"))
    assert mood_knobs(low).outreach_threshold_delta > mood_knobs(neutral).outreach_threshold_delta


async def test_apply_speed_clamps():
    from ophelia.mind.mood_behavior import mood_knobs
    from ophelia.mind.psyche import Mood, PsycheState

    k = mood_knobs(PsycheState(mood=Mood(valence=0.6, arousal=0.85)))
    # Even with a huge base, the clamped result stays <= 2.0.
    assert k.apply_speed(10.0) <= 2.0
    # And never below 0.5.
    assert k.apply_speed(0.01) >= 0.5


async def test_mood_system_hint_empty_when_neutral():
    from ophelia.mind.mood_behavior import mood_system_hint
    from ophelia.mind.psyche import Mood, PsycheState

    neutral = PsycheState(mood=Mood(valence=0.0, arousal=0.3, label="neutral"))
    assert mood_system_hint(neutral) == ""


async def test_pending_resume_for_returns_none_when_unconfigured(resume_stub):
    """tool_loop_resume defaults off; no resume should be surfaced."""
    assert resume_stub.pending_resume_for("telegram:123") is None


async def test_pending_resume_for_returns_stashed_chain(settings, resume_stub):
    resume_stub._pending_resume = {
        "telegram:123": {
            "messages": [{"role": "assistant", "content": "mid-step"}],
            "rounds": 4,
            "stuck": False,
        }
    }
    # tool_loop_resume must be enabled for the resume to surface.
    settings.__dict__["tool_loop_resume"] = True
    pending = resume_stub.pending_resume_for("telegram:123")
    assert pending is not None
    assert pending["rounds"] == 4
    # A stuck chain does NOT surface.
    resume_stub._pending_resume["telegram:123"]["stuck"] = True
    assert resume_stub.pending_resume_for("telegram:123") is None


async def test_continuation_cap_blocks_resume_after_six(settings, resume_stub):
    """Tier C #14 follow-up: after 6 consecutive continuations, the resume
    no longer surfaces so a stuck task can't monopolize every tick."""
    settings.__dict__["tool_loop_resume"] = True
    resume_stub._pending_resume = {
        "telegram:123": {"messages": [], "rounds": 3, "stuck": False}
    }
    # 5 continuations → still resumable.
    resume_stub._continuation_count["telegram:123"] = 5
    assert resume_stub.pending_resume_for("telegram:123") is not None
    # 6 continuations → capped, no longer surfaces.
    resume_stub._continuation_count["telegram:123"] = 6
    assert resume_stub.pending_resume_for("telegram:123") is None
