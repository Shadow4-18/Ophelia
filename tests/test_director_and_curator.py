"""Tests for Director decision parsing + curator reconcile parsing (F#4).

These pin the JSON-parsing contracts that sit between an LLM response and a
behavior change — the regression-prone paths where a malformed/noisy model
output could silently change her behavior. Both parsers are pure (no LLM),
so we test them directly against realistic failure modes.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ── Director._parse_decision ────────────────────────────────────────────────


def _director(settings):
    from ophelia.mind.director import Director

    return Director(settings, agent=None, psyche=None, drives=None)


async def test_director_parses_clean_json(settings):
    d = _director(settings)
    raw = '{"action": "speak", "urgency": "high", "pace_hint": "short punch", "reason": "goal due"}'
    dec = d._parse_decision(raw)
    assert dec.action == "speak"
    assert dec.urgency == "high"
    assert dec.pace_hint == "short punch"
    assert dec.should_speak is True
    assert dec.is_fast_reaction is False


async def test_director_parses_json_wrapped_in_prose(settings):
    """Models often wrap JSON in prose — the parser should still find it."""
    d = _director(settings)
    raw = 'Here is my decision:\n{"action": "react", "urgency": "normal", "pace_hint": "quip", "reason": "owner joked"}\nThanks!'
    dec = d._parse_decision(raw)
    assert dec.action == "react"
    assert dec.is_fast_reaction is True
    assert dec.should_speak is True


async def test_director_defaults_to_defer_on_unparseable(settings):
    d = _director(settings)
    dec = d._parse_decision("I think she should probably speak now")
    assert dec.action == "defer"
    assert dec.should_speak is False


async def test_director_clamps_unknown_action_to_defer(settings):
    """An action outside the allowed set must not crash or pass through."""
    d = _director(settings)
    dec = d._parse_decision('{"action": "SHOUT", "urgency": "EXTREME"}')
    assert dec.action == "defer"
    assert dec.urgency == "normal"  # unknown urgency clamps to normal


async def test_director_clamps_unknown_urgency_to_normal(settings):
    d = _director(settings)
    dec = d._parse_decision('{"action": "skip", "urgency": "EXTREME"}')
    assert dec.urgency == "normal"


async def test_director_disabled_state_returns_speak_for_owner(settings):
    """When the director is disabled (default), decide() returns a sensible
    default — speak for owner-active, defer otherwise — so existing behavior
    is unchanged. We test decide() directly since it short-circuits before
    any LLM call when disabled."""
    d = _director(settings)
    assert d.enabled is False
    dec_owner = await d.decide(trigger="user_message", owner_active=True)
    assert dec_owner.action == "speak"
    dec_tick = await d.decide(trigger="tick", owner_active=False)
    assert dec_tick.action == "defer"


async def test_director_urgency_speed_and_burst(settings):
    """The urgency→speed/burst knobs must stay monotonic in the right direction."""
    d = _director(settings)
    from ophelia.mind.director import DirectorDecision

    d.last = DirectorDecision(urgency="high")
    assert d.urgency_speed_mult() > 1.0
    assert d.urgency_burst_cap(400) <= 200
    # Consciousness calls these on the decision object directly — must work.
    assert d.last.urgency_burst_cap(400) <= 200
    assert d.last.urgency_speed_mult() > 1.0
    d.last = DirectorDecision(urgency="low")
    assert d.urgency_speed_mult() < 1.0
    assert d.urgency_burst_cap(400) > 400
    assert d.last.urgency_burst_cap(400) > 400
    d.last = None
    assert d.urgency_speed_mult() == 1.0
    assert d.urgency_burst_cap(400) == 400


async def test_director_decision_urgency_burst_cap_standalone():
    """Regression: consciousness.error was
    'DirectorDecision' object has no attribute 'urgency_burst_cap'."""
    from ophelia.mind.director import DirectorDecision

    high = DirectorDecision(urgency="high")
    assert high.urgency_burst_cap(400) == 200
    low = DirectorDecision(urgency="low")
    assert low.urgency_burst_cap(400) == int(400 * 1.4)
    normal = DirectorDecision(urgency="normal")
    assert normal.urgency_burst_cap(400) == 400


# ── MemoryCurator._apply_reconcile_actions ─────────────────────────────────


async def test_curator_reconcile_parses_clean_json():
    from ophelia.memory.curator import MemoryCurator

    entries = {"Owner lives in Seattle", "Owner works 9-5", "Owner likes cats"}
    raw = (
        '[{"action":"remove","original":"Owner lives in Seattle"},'
        '{"action":"correct","original":"Owner works 9-5","corrected":"Owner works 8-4"},'
        '{"action":"keep","original":"Owner likes cats"}]'
    )
    new_entries, changed = MemoryCurator._apply_reconcile_actions(raw, entries)
    assert changed == 2
    assert "Owner lives in Seattle" not in new_entries
    assert "Owner works 8-4" in new_entries
    assert "Owner works 9-5" not in new_entries
    assert "Owner likes cats" in new_entries


async def test_curator_reconcile_parses_json_wrapped_in_prose():
    from ophelia.memory.curator import MemoryCurator

    entries = {"Owner is 30 years old"}
    raw = (
        "Here are my decisions:\n"
        '[{"action":"correct","original":"Owner is 30 years old",'
        '"corrected":"Owner is 31 years old"}]\n'
        "Let me know if you need anything else."
    )
    new_entries, changed = MemoryCurator._apply_reconcile_actions(raw, entries)
    assert changed == 1
    assert "Owner is 31 years old" in new_entries
    assert "Owner is 30 years old" not in new_entries


async def test_curator_reconcile_ignores_actions_for_unknown_facts():
    """An action referencing a fact not in the stored set must be skipped,
    not crash, and not invent new entries."""
    from ophelia.memory.curator import MemoryCurator

    entries = {"Real fact one"}
    raw = '[{"action":"remove","original":"Fact that does not exist"}]'
    new_entries, changed = MemoryCurator._apply_reconcile_actions(raw, entries)
    assert changed == 0
    assert new_entries == entries


async def test_curator_reconcile_handles_empty_and_malformed():
    from ophelia.memory.curator import MemoryCurator

    entries = {"Some fact"}
    # Empty output → no change.
    assert MemoryCurator._apply_reconcile_actions("", entries) == (entries, 0)
    # No JSON array present → no change.
    assert MemoryCurator._apply_reconcile_actions("Nothing to reconcile.", entries) == (entries, 0)
    # Malformed JSON → no change, no crash.
    assert MemoryCurator._apply_reconcile_actions("[{bad json", entries) == (entries, 0)
    # JSON object instead of array → no change.
    assert MemoryCurator._apply_reconcile_actions('{"action":"remove"}', entries) == (entries, 0)


async def test_curator_reconcile_correct_with_empty_corrected_is_ignored():
    """A 'correct' action with an empty corrected string should not delete the
    original — that would be a silent data loss."""
    from ophelia.memory.curator import MemoryCurator

    entries = {"Important fact"}
    raw = '[{"action":"correct","original":"Important fact","corrected":""}]'
    new_entries, changed = MemoryCurator._apply_reconcile_actions(raw, entries)
    assert changed == 0
    assert "Important fact" in new_entries
