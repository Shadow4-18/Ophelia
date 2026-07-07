"""Tests for proactive outreach junk filtering."""

from ophelia.channels.proactive_filter import is_outreach_junk, proactive_chunks


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
