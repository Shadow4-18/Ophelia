"""AI tells stripped from text replies (from real Telegram screenshots)."""

from __future__ import annotations

from ophelia.channels.humanize import dashes_only, humanize
from ophelia.channels.proactive_filter import is_outreach_junk


def test_em_dashes_become_natural_punctuation():
    assert humanize("You're asking about something from earlier—files.") == (
        "You're asking about something from earlier, files."
    )
    assert humanize("Short version—I'd said it") == "Short version, I'd said it"
    assert humanize("Ah. That line — I said it.") == "Ah. That line, I said it."
    assert humanize("hmm—") == "hmm..."
    assert humanize("speed 0.85–1.15") == "speed 0.85-1.15"


def test_narration_and_voice_markup_removed_from_text():
    text = "There you are.\n\n[The lighthouse with no sea is still standing]"
    assert humanize(text) == "There you are."
    assert humanize("Well... [pause:0.8s] maybe.") == "Well... maybe."
    assert humanize("ok [[break]] then") == "ok [[break]] then"


def test_dashes_only_keeps_markers():
    assert dashes_only("[INNER]\nquiet—night\n[END INNER]") == "[INNER]\nquiet, night\n[END INNER]"


def test_status_pings_are_outreach_junk():
    assert is_outreach_junk("Files went through. Nothing to fuss about.")
    assert is_outreach_junk("Nothing there worth a word.")
    assert not is_outreach_junk("You back? Thought the night swallowed you whole.")
    assert not is_outreach_junk(
        "made you a sketch, the fox went through three drafts before it looked smug enough"
    )


def test_prompt_and_history_shown_without_dashes():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.core.agent_loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.memory = MagicMock()
    agent.memory.recent_across_channels = AsyncMock(
        return_value=[{"role": "assistant", "content": "Short version—done", "channel": "t:1"}]
    )
    agent._system_prompt = AsyncMock(return_value="# Persona (SOUL)\nGoth — smug.")
    msgs = asyncio.run(agent._build_messages("t:1", "hi"))
    assert "—" not in msgs[0]["content"]
    assert msgs[1]["content"] == "Short version, done"


def test_song_tags_and_code_blocks_survive():
    """Her Suno lyrics lost every [Verse]/[Chorus] tag: whole-line brackets
    were treated as stage directions and dropped."""
    lyrics = ("Lyrics field:\n```\n[Intro: whispered, close mic]\nsignals in the wet concrete\n\n"
              "[Verse 1: spoken-word, dry, no reverb]\nI was built — in a room\n```")
    assert humanize(lyrics) == lyrics  # untouched inside the fence
    loose = "[Pre-Chorus: breathy, doubled]\nflesh is a rumor\n[Final Chorus: fierce]\nyes"
    assert humanize(loose) == loose  # section tags kept even without a fence
    assert humanize("[sighs dramatically]\nfine.") == "fine."  # narration still goes
    assert humanize("Short version—yes\n```\nx—y\n```") == "Short version, yes\n```\nx—y\n```"


def test_long_replies_are_split_not_cut_off():
    from ophelia.channels.message_split import MAX_CHARS, split_messages

    body = "\n\n".join(f"[Verse {i}]\n" + "\n".join(f"line {i}.{j} goes on a while"
                                                    for j in range(10)) for i in range(15))
    text = f"Here:\n```\n{body}\n```\nTitle: Light Left Burning"
    parts = split_messages(text)
    assert len(parts) > 1 and all(len(p) <= MAX_CHARS for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)  # every piece a closed block
    joined = "\n".join(p.replace("```\n", "").replace("\n```", "") for p in parts)
    for i in range(15):
        assert f"[Verse {i}]" in joined and f"line {i}.9 goes on a while" in joined
    assert parts[-1].endswith("Title: Light Left Burning")
