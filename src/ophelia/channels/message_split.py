"""Split agent output into multiple sequential chat messages."""

from __future__ import annotations

import re

MESSAGE_BREAK = "[[break]]"
MAX_MESSAGES_PER_TURN = 6

_BREAK_RE = re.compile(r"\s*\[\[\s*break\s*\]\]\s*", re.IGNORECASE)


def split_messages(text: str, limit: int = MAX_MESSAGES_PER_TURN) -> list[str]:
    parts = [p.strip() for p in _BREAK_RE.split(text or "") if p.strip()]
    if not parts:
        return [(text or "").strip() or "(no response)"]
    return parts[:limit]
