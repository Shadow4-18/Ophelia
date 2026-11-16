"""Split agent output into multiple sequential chat messages."""

from __future__ import annotations

import re

MESSAGE_BREAK = "[[break]]"
MAX_MESSAGES_PER_TURN = 6

_BREAK_RE = re.compile(r"\s*\[\[\s*break\s*\]\]\s*", re.IGNORECASE)


# Below Discord's 2000-character cap (Telegram's is 4096). Longer pieces
# used to be cut off silently by each gateway ([:2000] / [:4000]), which ate
# the end of long replies such as a full set of song lyrics.
MAX_CHARS = 1900


def split_messages(text: str, limit: int = MAX_MESSAGES_PER_TURN,
                   max_chars: int = MAX_CHARS) -> list[str]:
    parts = [p.strip() for p in _BREAK_RE.split(text or "") if p.strip()]
    if not parts:
        return [(text or "").strip() or "(no response)"]
    out: list[str] = []
    for part in parts[:limit]:
        out.extend(fit(part, max_chars))
    return out


def fit(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """``text`` as pieces of at most ``max_chars``, split at blank lines, then
    lines, then spaces. A ``` block cut in two is closed at the end of one
    piece and reopened at the start of the next, so it stays copyable."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    rest = text
    reopen = False
    while rest:
        prefix = "```\n" if reopen else ""
        room = max_chars - len(prefix) - 4  # space to close a fence
        if len(prefix) + len(rest) <= max_chars:
            pieces.append(prefix + rest)
            break
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = rest.rfind(sep, 0, room)
            if cut > room // 3:
                break
        if cut <= 0:
            cut = room
        head, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n ")
        piece = prefix + head
        open_fence = piece.count("```") % 2 == 1
        if open_fence:
            piece += "\n```"
        pieces.append(piece)
        reopen = open_fence
    return [p for p in pieces if p.strip()]
