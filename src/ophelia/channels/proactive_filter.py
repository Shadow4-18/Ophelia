"""Filter junk from proactive / spontaneous outreach (Discord DMs, Telegram, etc.).

Consciousness ticks, inner-mirror, and the send_message tool can emit status
placeholders like SKIP or "(no response)" that should stay internal — not ping
the owner every 90 seconds.
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

# Channel-tagged status lines echoed from cross-channel memory context.
_CHANNEL_STATUS = re.compile(
    r"^\[(?:consciousness|inner|cli|saw|spontaneous)\]\s*(\(no response\)|no response)?\s*$",
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
    if _CHANNEL_STATUS.match(t):
        return True
    return False


def proactive_chunks(text: str, *, limit: int = 6) -> list[str]:
    """Split proactive text and drop junk/empty chunks."""
    return [c for c in split_messages(text, limit=limit) if not is_outreach_junk(c)]
