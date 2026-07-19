"""Tests for Alive Free Will v1 — wakes, satiation, soft rate limits."""

from __future__ import annotations

import asyncio
import time

import pytest

# Break circular import (agent_loop ↔ channels) the same way other tests do.
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.core.signals import Signals
from ophelia.mind.consciousness import satiation_threshold_delta
from ophelia.mind.drives import DriveState
from ophelia.mind.initiative import InitiativeGovernor


def test_satiation_decays_with_half_life():
    now = 1_000_000.0
    half = 45.0
    at_action = satiation_threshold_delta(
        now, half_life_seconds=half, arousal=0.0, now=now
    )
    assert at_action == pytest.approx(0.35, abs=0.01)

    at_half = satiation_threshold_delta(
        now - half, half_life_seconds=half, arousal=0.0, now=now
    )
    assert at_half == pytest.approx(0.175, abs=0.02)

    at_far = satiation_threshold_delta(
        now - half * 6, half_life_seconds=half, arousal=0.0, now=now
    )
    assert at_far < 0.02


def test_satiation_arousal_shortens_half_life():
    now = 1_000_000.0
    # Same wall time since action — higher arousal => less satiation left
    calm = satiation_threshold_delta(
        now - 45.0, half_life_seconds=45.0, arousal=0.0, now=now
    )
    hyped = satiation_threshold_delta(
        now - 45.0, half_life_seconds=45.0, arousal=1.0, now=now
    )
    assert hyped < calm


def test_governor_impulse_override_when_over_cap():
    gov = InitiativeGovernor(max_spontaneous_per_hour=2, quiet_hours="")
    gov._recent = [time.time(), time.time()]
    ok, reason = gov.allow_outreach(pressure=0.3, threshold=0.32)
    assert ok is False
    assert reason == "rate_limit"
    ok2, reason2 = gov.allow_outreach(pressure=0.7, threshold=0.32)
    assert ok2 is True
    assert reason2 == "impulse_override"


def test_governor_quiet_hours_still_hard(monkeypatch):
    gov = InitiativeGovernor(max_spontaneous_per_hour=20, quiet_hours="0-24")
    monkeypatch.setattr(gov, "_local_hour", lambda: 3)
    # Force quiet: 0-24 means all day if start<=end... 0 <= 3 < 24 → quiet
    ok, reason = gov.allow_outreach(pressure=0.99, threshold=0.1)
    assert ok is False
    assert reason == "quiet_hours"


def test_drives_grow_faster_when_alone():
    d = DriveState(social=0.2, boredom=0.1, expressiveness=0.2)
    d.tick_idle(200, interval=45)
    assert d.social > 0.25
    assert d.boredom > 0.15
    assert d.expressiveness > 0.22


@pytest.mark.asyncio
async def test_request_wake_sets_event():
    s = Signals()
    assert not s.wake_event.is_set()
    s.request_wake("chat_ended")
    assert s.wake_event.is_set()
    reason, urgent = s.consume_wake()
    assert reason == "chat_ended"
    assert urgent is False
    assert not s.wake_event.is_set()


@pytest.mark.asyncio
async def test_interruptible_sleep_wakes_early():
    from ophelia.mind.consciousness import ConsciousnessLoop
    from ophelia.mind.psyche import PsycheState
    from ophelia.mind.goals import GoalStore
    from unittest.mock import MagicMock

    signals = Signals()
    # Minimal loop instance — only need interruptible sleep + signals
    loop = ConsciousnessLoop.__new__(ConsciousnessLoop)
    loop.signals = signals
    loop.initiative_threshold = 0.32
    loop.action_cooldown = 45

    async def _fire():
        await asyncio.sleep(0.05)
        signals.request_wake("chat_ended", urgent=True)

    asyncio.create_task(_fire())
    t0 = time.time()
    reason = await loop._interruptible_sleep(5.0)
    elapsed = time.time() - t0
    assert reason == "chat_ended"
    assert elapsed < 1.0


def test_inner_framing_not_tick():
    import inspect
    from ophelia.core import agent_loop
    from ophelia.mind import consciousness

    src = inspect.getsource(agent_loop.AgentLoop._build_messages)
    assert "[INNER]" in src
    assert "[TICK]" not in src
    assert "[INNER]" in consciousness.CONSCIOUSNESS_PROMPT
    assert "scheduled heartbeat" not in consciousness.CONSCIOUSNESS_PROMPT.lower()
