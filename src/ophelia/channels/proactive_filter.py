"""Filter junk from proactive / spontaneous outreach (Discord DMs, Telegram, etc.).

Consciousness ticks, inner-mirror, and the send_message tool can emit status
placeholders like SKIP or "(no response)" that should stay internal — not ping
the owner every 90 seconds on every configured gateway.
"""

from __future__ import annotations

import re

from ophelia.channels.message_split import split_messages

# Exact-match placeholders the runtime or models emit when there's nothing to say.
_JUNK_EXACT = frozenset(
    {
        "skip",
        "(no response)",
        "no response",
        "[no response]",
    }
)

# Channel-tagged lines copied from cross-channel memory — never user-facing outreach.
_CHANNEL_TAG = re.compile(
    r"^\[(?:consciousness|inner|cli|saw|spontaneous)\]", re.IGNORECASE
)

# System/meta diagnostics the model emits when confused by tick loops.
_META_KEYWORDS = re.compile(
    r"\b(?:duplicate(?:\s+\w+){0,3}\s+prompt|duplicate block|"
    r"cycling the same|looping without|no owner engagement|"
    r"holding stillness|holding still)\b",
    re.IGNORECASE,
)


def is_outreach_junk(text: str) -> bool:
    """True if this text should not be pushed to the owner as outreach."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    if low in _JUNK_EXACT:
        return True
    if _CHANNEL_TAG.match(t):
        return True
    if _META_KEYWORDS.search(t):
        return True
    # Bare channel status: "[consciousness] (no response)" etc.
    if re.match(
        r"^\[(?:consciousness|inner)\]\s*(\(no response\)|no response)?\s*$",
        t,
        re.IGNORECASE,
    ):
        return True
    return False


def proactive_chunks(text: str, *, limit: int = 6) -> list[str]:
    """Split proactive text and drop junk/empty chunks."""
    return [c for c in split_messages(text, limit=limit) if not is_outreach_junk(c)]
