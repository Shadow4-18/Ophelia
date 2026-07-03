"""Tests for LifeContext owner-state inference (Tier C #15).

The owner-state inference (asleep / at_work / around / away) drives sleep mode,
work-hour outreach suppression, and TTS speed. A regression here makes her
ping at 3am or stay silent at noon. These tests freeze the clock and pin the
inference for the canonical cases.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytestmark = pytest.mark.asyncio


def _make_life(settings, signals):
    from ophelia.mind.life_context import LifeContext

    life = LifeContext(settings, signals)
    return life


async def _set_silent_hours(life, hours: float):
    """Stub the telegram-silent-hours calculation by monkeypatching."""
    life._telegram_silent_hours = lambda: hours  # type: ignore[assignment]


async def test_sleep_hours_returns_asleep(settings, signals, monkeypatch):
    life = _make_life(settings, signals)
    # Freeze time at 2am UTC (inside sleep_hours 0-7).
    fixed = datetime(2026, 7, 3, 2, 0, 0, tzinfo=life.tz())
    monkeypatch.setattr(life, "now", lambda: fixed)
    await _set_silent_hours(life, 3.0)
    assert life.infer_owner_state() == "asleep"
    assert life.is_sleep_mode() is True


async def test_workday_workhours_returns_at_work(settings, signals, monkeypatch):
    life = _make_life(settings, signals)
    # Friday 2026-07-03 at 11am UTC, work_days mon-fri, work_hours 9-17.
    fixed = datetime(2026, 7, 3, 11, 0, 0, tzinfo=life.tz())
    monkeypatch.setattr(life, "now", lambda: fixed)
    await _set_silent_hours(life, 2.0)
    assert life.infer_owner_state() == "at_work"
    assert life.is_owner_at_work() is True
    assert life.should_minimize_outreach() is True


async def test_active_messaging_returns_active_here(settings, signals, monkeypatch):
    life = _make_life(settings, signals)
    # Saturday 2026-07-04 at 11am (weekend, not work hours).
    fixed = datetime(2026, 7, 4, 11, 0, 0, tzinfo=life.tz())
    monkeypatch.setattr(life, "now", lambda: fixed)
    await _set_silent_hours(life, 0.01)  # owner just messaged
    assert life.infer_owner_state() == "active_here"


async def test_long_silence_returns_long_absence(settings, signals, monkeypatch):
    life = _make_life(settings, signals)
    fixed = datetime(2026, 7, 4, 11, 0, 0, tzinfo=life.tz())
    monkeypatch.setattr(life, "now", lambda: fixed)
    await _set_silent_hours(life, 6.0)
    assert life.infer_owner_state() == "long_absence"


async def test_learned_quiet_sharpens_away(settings, signals, monkeypatch):
    """Tier B #6: when the learned schedule says quiet + owner silent, infer
    likely_away_quiet_hour even outside static work hours."""
    life = _make_life(settings, signals)
    # Sunday 2026-07-05 at 8pm — not work hours, not sleep.
    fixed = datetime(2026, 7, 5, 20, 0, 0, tzinfo=life.tz())
    monkeypatch.setattr(life, "now", lambda: fixed)
    await _set_silent_hours(life, 2.0)
    # Without learned-quiet, 2h silence → away_or_busy.
    life._last_learned_quiet = False
    assert life.infer_owner_state() == "away_or_busy"
    # With learned-quiet, it sharpens to likely_away_quiet_hour.
    life._last_learned_quiet = True
    assert life.infer_owner_state() == "likely_away_quiet_hour"


async def test_voice_speed_slows_in_sleep_mode(settings, signals, monkeypatch):
    life = _make_life(settings, signals)
    fixed = datetime(2026, 7, 3, 2, 0, 0, tzinfo=life.tz())
    monkeypatch.setattr(life, "now", lambda: fixed)
    await _set_silent_hours(life, 3.0)
    base = settings.kokoro_tts_speed
    speed = life.voice_speed()
    assert speed <= base
    assert speed <= 0.88 + 1e-6  # sleep mode caps speed at 0.88
