"""Reactions and replies: the small things people do in a chat.

A per-chat record of recent messages (his and hers) lets her know what a
reaction or a reply refers to, and lets her react to or reply to a specific
message by quoting a few of its words. A channel gateway fills the record
and hands the turn a ``ChatActions`` that does the platform-specific part.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

# Emoji a bot may react with on Telegram (anything else is rejected there).
TELEGRAM_REACTIONS = frozenset(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 "
    "🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ "
    "💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()
)


def normalize_emoji(emoji: str) -> str:
    """Drop variation selectors so ❤️ matches Telegram's ❤."""
    return (emoji or "").replace("️", "").strip()


@dataclass
class Entry:
    message_id: int
    who: str  # "him" (the person she's talking to) | "her"
    text: str
    ts: float


def describe_message(text: str | None, *, caption: str | None = None, kind: str = "") -> str:
    """Short readable content of a message for prompts."""
    body = " ".join((text or caption or "").split())
    label = f"[{kind}]" if kind else ""
    if body and label:
        return f"{label} {body}"[:300]
    return (body or label or "[message]")[:300]


class MessageIndex:
    """Recent messages per chat, newest last."""

    def __init__(self, per_chat: int = 80) -> None:
        self._chats: dict[int | str, deque[Entry]] = {}
        self._per_chat = per_chat

    def add(self, chat_id: int | str, message_id: int, who: str, text: str) -> None:
        chat = self._chats.setdefault(chat_id, deque(maxlen=self._per_chat))
        chat.append(Entry(int(message_id), who, text, time.time()))

    def get(self, chat_id: int | str, message_id: int) -> Entry | None:
        for e in reversed(self._chats.get(chat_id, ())):
            if e.message_id == int(message_id):
                return e
        return None

    def last(self, chat_id: int | str, who: str = "him") -> Entry | None:
        for e in reversed(self._chats.get(chat_id, ())):
            if e.who == who:
                return e
        return None

    def find(self, chat_id: int | str, query: str) -> Entry | None:
        """Newest message containing the quoted words (exact phrase first,
        then most words in common)."""
        q = " ".join((query or "").lower().split()).strip("\"' ")
        if not q:
            return self.last(chat_id, "him")
        entries = list(reversed(self._chats.get(chat_id, ())))
        for e in entries:
            if q in e.text.lower():
                return e
        words = set(re.findall(r"\w+", q))
        if not words:
            return None
        best, best_score = None, 0.0
        for e in entries:
            have = set(re.findall(r"\w+", e.text.lower()))
            score = len(words & have) / len(words)
            if score > best_score:
                best, best_score = e, score
        return best if best_score >= 0.6 else None


@dataclass
class ChatActions:
    """What a turn can do in the current chat besides sending text."""

    react: Callable[[str, str], Awaitable[str]]  # (emoji, which message) -> result
    reply: Callable[[str, str], Awaitable[str]]  # (text, which message) -> result
    typing: Callable[[], Awaitable[None]] | None = None  # show "typing..." (~5s)


def reaction_note(emoji: str, target: Entry | None) -> str:
    """How his reaction reads in her history."""
    if target is None:
        return f"[He reacted {emoji} to one of your messages]"
    whose = "your" if target.who == "her" else "his own"
    return f"[He reacted {emoji} to {whose} message: \"{target.text[:200]}\"]"


def reply_prefix(quoted: str, *, to_her: bool, partial: bool = False) -> str:
    """Context line put before a message that replies to an earlier one."""
    whose = "your" if to_her else "his own earlier"
    part = "this part of " if partial else ""
    return f"[Replying to {part}{whose} message: \"{quoted[:300]}\"]"
