"""Tests for proactive outreach junk filtering."""

from ophelia.channels.proactive_filter import (
    is_outreach_junk,
    is_stillness_mood_label,
    is_tick_status_noise,
    proactive_chunks,
)
from ophelia.mind.consciousness import _soften_silent_tick


def test_skip_and_no_response_are_junk():
    assert is_outreach_junk("SKIP")
    assert is_outreach_junk("(no response)")
    assert is_outreach_junk("[consciousness] (no response)")
    assert is_outreach_junk("[inner] (no response)")


def test_channel_tagged_and_meta_diagnostics_are_junk():
    assert is_outreach_junk("[consciousness] Duplicate ambient prompts. Cycling.")
    assert is_outreach_junk(
        "[consciousness] Duplicate block — same ambient text delivered twice."
    )
    assert is_outreach_junk("[inner] holding stillness")


def test_real_messages_pass():
    assert not is_outreach_junk("hey, you around?")
    assert not is_outreach_junk("had a weird thought about the synth patch")


def test_proactive_chunks_drop_junk():
    assert proactive_chunks("SKIP") == []
    assert proactive_chunks("") == []
    chunks = proactive_chunks("hello [[break]] SKIP")
    assert chunks == ["hello"]


def test_tick_status_noise_detects_stillness_labels():
    assert is_tick_status_noise("holding stillness")
    assert is_tick_status_noise("Stillness")
    assert is_tick_status_noise("nothing to say this pulse")
    assert is_tick_status_noise("mid-thought again")
    assert is_stillness_mood_label("stillness")
    assert is_stillness_mood_label("quiet")
    assert not is_tick_status_noise("wondering if the synth patch needs a filter")
    assert not is_stillness_mood_label("curious")


def test_soften_silent_tick_strips_status_fluff():
    tick = {
        "action": "silent",
        "internal_thought": "holding stillness",
        "mood": {"valence": 0.1, "arousal": 0.2, "label": "stillness"},
        "feelings": ["stillness", "a little lonely"],
        "urges": ["waiting"],
        "outward_message": "SKIP",
    }
    out = _soften_silent_tick(tick, prior_mood_label="curious")
    assert out["internal_thought"] == ""
    assert out["mood"]["label"] == "curious"
    assert out["feelings"] == ["a little lonely"]
    assert out["urges"] == []
    assert out["outward_message"] == ""


def test_soften_silent_tick_leaves_real_silent_thought():
    tick = {
        "action": "silent",
        "internal_thought": "still turning over that joke from earlier",
        "mood": {"valence": 0.2, "arousal": 0.3, "label": "amused"},
    }
    out = _soften_silent_tick(tick, prior_mood_label="curious")
    assert "joke" in out["internal_thought"]
    assert out["mood"]["label"] == "amused"


def test_soften_does_not_touch_non_silent_actions():
    tick = {
        "action": "message",
        "internal_thought": "holding stillness is wrong here",
        "outward_message": "hey",
    }
    out = _soften_silent_tick(tick, prior_mood_label="curious")
    assert out["internal_thought"] == "holding stillness is wrong here"
