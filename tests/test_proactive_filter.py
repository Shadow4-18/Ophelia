"""Tests for proactive outreach junk filtering."""

from ophelia.channels.proactive_filter import (
    has_creative_tool_intent,
    is_consciousness_tick_payload,
    is_outreach_junk,
    is_stillness_mood_label,
    is_tick_status_noise,
    proactive_chunks,
    strip_consciousness_tick_leak,
)
from ophelia.mind.consciousness import _promote_declared_action, _soften_silent_tick


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


TICK_JSON = (
    "{\n"
    '  "internal_thought": "",\n'
    '  "mood": {"valence": 0.45, "arousal": 0.55, "label": "determined"},\n'
    '  "action": "silent"\n'
    "}"
)


def test_consciousness_tick_json_is_outreach_junk():
    assert is_consciousness_tick_payload(TICK_JSON)
    assert is_outreach_junk(TICK_JSON)
    assert is_outreach_junk("[consciousness]\n" + TICK_JSON)
    assert is_tick_status_noise(TICK_JSON)


def test_strip_tick_leak_keeps_prose_drops_json():
    mixed = (
        "Sent. This time I used Illustrious with explicit dark-gothic details.\n\n"
        f"[consciousness]\n{TICK_JSON}"
    )
    cleaned = strip_consciousness_tick_leak(mixed)
    assert "Illustrious" in cleaned
    assert "internal_thought" not in cleaned
    assert '"action"' not in cleaned
    assert not is_consciousness_tick_payload(cleaned)


def test_strip_tick_leak_pure_json_becomes_empty():
    assert strip_consciousness_tick_leak(TICK_JSON) == ""
    assert strip_consciousness_tick_leak("[consciousness] " + TICK_JSON) == ""


def test_proactive_chunks_strip_embedded_tick_json():
    mixed = f"hey check this [[break]] {TICK_JSON}"
    assert proactive_chunks(mixed) == ["hey check this"]


def test_has_creative_tool_intent():
    assert has_creative_tool_intent("generate_image of myself nsfw")
    assert has_creative_tool_intent("draw an image with Illustrious")
    assert has_creative_tool_intent("make a selfie")
    assert not has_creative_tool_intent("just thinking about music")


def test_promote_message_with_tool_intent_to_act():
    tick = {
        "action": "message",
        "internal_thought": "need a better selfie",
        "tool_intent": "generate_image nsfw gothic look",
        "outward_message": "Sent. regenerating now",
    }
    out = _promote_declared_action(tick)
    assert out["action"] == "act"


def test_promote_silent_with_explicit_creative_tool_intent():
    tick = {
        "action": "silent",
        "internal_thought": "",
        "tool_intent": "generate_image pony nsfw",
    }
    out = _promote_declared_action(tick)
    assert out["action"] == "act"


def test_promote_leaves_true_silent_alone():
    tick = {
        "action": "silent",
        "internal_thought": "quiet for a bit",
        "tool_intent": "",
    }
    out = _promote_declared_action(tick)
    assert out["action"] == "silent"


def test_promote_leaves_act_alone():
    tick = {"action": "act", "tool_intent": "generate_image"}
    assert _promote_declared_action(tick)["action"] == "act"
