"""She kept "seeing" a clock frozen at 5 at night: the screenshot of a
locked, off screen is a stale frame / the always-on display. Ambient glances
now wait for an unlocked, awake screen."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.android.shizuku import parse_screen_state

AWAKE = "  mWakefulness=Awake\nDisplay Power: state=ON"
ASLEEP = "  mWakefulness=Asleep\nDisplay Power: state=OFF"
AOD = "  mWakefulness=Dozing\nDisplay Power: state=DOZE"


def test_parse_screen_state():
    assert parse_screen_state(ASLEEP) == "off"
    assert parse_screen_state(AOD) == "off"  # Samsung always-on display clock
    assert parse_screen_state(AWAKE, "    mDreamingLockscreen=true mDreamingSleepToken=null") \
        == "locked"
    assert parse_screen_state(AWAKE, "  KeyguardController:\n    mKeyguardShowing=true") \
        == "locked"
    assert parse_screen_state(AWAKE, "    mDreamingLockscreen=false") == "on"
    assert parse_screen_state("rish error 1: denied") == "unknown"


def _loop(state):
    from ophelia.mind.ambient_commentary import AmbientCommentaryLoop

    loop = AmbientCommentaryLoop.__new__(AmbientCommentaryLoop)
    loop.vision = SimpleNamespace(explore_cycle=AsyncMock(return_value="x" * 50),
                                  android=SimpleNamespace(screen_state=AsyncMock(
                                      return_value=state)))
    loop._screen_state = ""
    loop._recent = []
    loop.settings = MagicMock()
    loop.agent = MagicMock()
    loop.agent.run_turn = AsyncMock(return_value="")
    return loop


@pytest.mark.asyncio
@pytest.mark.parametrize("state,looks", [("off", False), ("locked", False), ("on", True),
                                         ("unknown", True)])
async def test_no_glance_at_a_dark_or_locked_screen(state, looks, monkeypatch):
    loop = _loop(state)
    from ophelia.mind import quiet

    monkeypatch.setattr(quiet, "active", lambda ch: None)
    await loop._maybe_comment()
    assert loop.vision.explore_cycle.await_count == (1 if looks else 0)
