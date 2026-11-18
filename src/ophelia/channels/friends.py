"""Friends: guests the owner trusts a little more.

A friend is still a guest in every way that matters for storage: their chat
lives in guest_messages, their name, rapport notes and follow-ups are keyed
by their channel. So promoting a guest (or demoting a friend) moves nothing:
all of it carries straight over. What changes is which tools she may use in
their chat (FRIEND_EXTRA_TOOLS, each one scoped to that friend's own things)
and a warmer note in her prompt.

Who's a friend: OPHELIA_FRIEND_IDS (channel-style, comma-separated) plus
whoever the owner named in chat (set_friend → the "friends" fact).
"""

from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger()

_FACT = "friends"

# On top of the guest allow-list. Read-only phone tools only: no taps, typing,
# shell, opening apps or game actions, and no notifications (his messages).
FRIEND_EXTRA_TOOLS: frozenset[str] = frozenset({
    "look_at_web_page",
    "list_inbox_images",
    "list_inbox_files",
    "recall_memory",
    "recall_day",
    "remember_person",
    "follow_up",
    "follow_up_done",
    "go_quiet",
    "site_list_pages",
    "site_get_page",
    "phone_see_screen",
    "phone_ui_dump",
    "phone_battery",
    "phone_game_look",
    "phone_game_state",
    "phone_game_query",
})


def _norm(channel: str) -> str:
    return (channel or "").strip().lower()


def configured(settings: Any) -> set[str]:
    raw = str(getattr(settings, "friend_ids", "") or "")
    return {_norm(t) for t in raw.split(",") if ":" in t}


async def _stored(memory: Any) -> set[str]:
    if memory is None:
        return set()
    try:
        raw = await memory.get_fact(_FACT)
        data = json.loads(raw) if raw else []
        return {_norm(c) for c in data if isinstance(c, str) and c.strip()}
    except Exception as e:
        log.debug("friends.load_failed", error=str(e))
        return set()


async def friend_channels(settings: Any, memory: Any) -> set[str]:
    return configured(settings) | await _stored(memory)


async def is_friend(settings: Any, memory: Any, channel: str) -> bool:
    ch = _norm(channel)
    if not ch or settings.is_owner_channel(ch):
        return False
    return ch in await friend_channels(settings, memory)


async def set_friend(memory: Any, channel: str, friend: bool) -> None:
    current = await _stored(memory)
    ch = _norm(channel)
    if friend:
        current.add(ch)
    else:
        current.discard(ch)
    await memory.set_fact(_FACT, json.dumps(sorted(current)))
