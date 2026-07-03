"""Track what lands vs falls flat — adaptive humor hints for the prompt.

Tier B #8 expands this beyond just "score the reaction to spontaneous outreach":

  - Detects jokes/quips in her normal chat replies (not just outreach) and
    scores them when the owner reacts.
  - Recognizes sticker / emoji / reaction signals as positive humor feedback
    (Telegram stickers, emoji-only replies, reactions).
  - Recognizes "bit" callbacks — when a phrase or setup from a prior landing
    joke reappears, it's tracked as a callback.
  - Auto-feeds save_lesson when a bit lands repeatedly (>=3 times with positive
    avg score), so durable comedic instincts become part of her memory.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import structlog

from ophelia.memory.store import MemoryStore

if TYPE_CHECKING:
    from ophelia.core.agent_loop import AgentLoop

log = structlog.get_logger()

_POSITIVE = re.compile(
    r"\b(lol|lmao|rofl|haha|hehe|😂|🤣|💀|dead|funny|good one|nice one|"
    r"that got me|i'm dying|you're killing me)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(not funny|cringe|stop|whatever|ok\.|okay\.|meh|ugh)\b",
    re.I,
)
# Heuristics for "this outbound line is probably a joke/quip" — used to decide
# whether to track a chat reply as a humor event at all, vs. just an answer.
_JOKE_HINTS = re.compile(
    r"(?:^|[\s])(lol|lmao|haha|heh|💀|🤣|😂|\.\.\.|…|—|btw,|okay so|imagine|"
    r"plot twist|twist:|spoiler|fun fact|pro tip|hot take|unpopular opinion)",
    re.I,
)
# Sticker / reaction signal in inbound. Telegram stickers arrive as a special
# message kind; an emoji-only text reply is also a strong humor signal.
def _is_emoji_only(text: str) -> bool:
    """True if `text` is short and contains only emoji / punctuation / whitespace."""
    if not text or len(text) > 12:
        return False
    for ch in text:
        cp = ord(ch)
        # ASCII letter or digit → not emoji-only.
        if (0x30 <= cp <= 0x39) or (0x41 <= cp <= 0x5A) or (0x61 <= cp <= 0x7A):
            return False
    # Must contain at least one character in the emoji unicode blocks.
    has_emoji = any(
        0x1F300 <= ord(ch) <= 0x1FAFF
        or 0x2600 <= ord(ch) <= 0x27BF
        or 0x1F900 <= ord(ch) <= 0x1F9FF
        for ch in text
    )
    return has_emoji


class HumorTracker:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory
        # Pending outbound line we're waiting for a reaction to. Outreach path
        # uses this (existing behavior); chat-joke path uses _pending_chat.
        self._pending_outbound: str | None = None
        self._pending_ts: float = 0.0
        # Pending chat-joke: a humor-flavored line in a normal chat reply.
        self._pending_chat: str | None = None
        self._pending_chat_ts: float = 0.0
        # Bits we've already turned into a lesson, so we don't double-save.
        self._lesson_done: set[str] = set()
        self._agent: AgentLoop | None = None

    def bind_agent(self, agent: "AgentLoop") -> None:
        """Wire to the agent so auto-save_lesson can fire when bits land."""
        self._agent = agent

    async def note_outbound(self, text: str) -> None:
        """Call when Ophelia sends something that might be a joke or quip
        (outreach path — spontaneous outward messages from consciousness)."""
        t = text.strip()
        if len(t) < 8 or len(t) > 500:
            return
        self._pending_outbound = t
        self._pending_ts = time.time()
        await self.memory.record_humor_outbound(t)

    async def note_chat_reply(self, text: str) -> None:
        """Call when Ophelia sends a normal chat reply. If it looks joke-shaped
        (Tier B #8), track it as a chat-joke so we can score the owner's next
        reaction. Non-joke replies are ignored — we don't want to score every
        factual answer."""
        t = text.strip()
        if len(t) < 6 or len(t) > 400:
            return
        if not _JOKE_HINTS.search(t):
            return
        self._pending_chat = t
        self._pending_chat_ts = time.time()
        await self.memory.record_humor_event(
            outbound=t, score=0.0, kind="chat-joke"
        )

    async def score_inbound_reply(self, user_text: str) -> None:
        """Score reaction to the last outbound humor attempt (outreach path)."""
        if not self._pending_outbound:
            # Fall through to chat-joke scoring (Tier B #8).
            await self._score_chat_reaction(user_text)
            return
        text = user_text.strip()
        score = self._score_text(text)
        await self.memory.record_humor_reaction(
            self._pending_outbound,
            user_reply=text[:400],
            score=score,
            latency_s=time.time() - self._pending_ts,
        )
        log.info("humor.scored", kind="outreach", score=round(score, 2), preview=text[:60])
        self._pending_outbound = None
        # Also try chat path in case both were pending.
        await self._score_chat_reaction(user_text)
        await self._maybe_save_lesson()

    async def _score_chat_reaction(self, user_text: str) -> None:
        if not self._pending_chat:
            return
        text = user_text.strip()
        score = self._score_text(text)
        await self.memory.record_humor_event(
            outbound=self._pending_chat,
            user_reply=text[:400],
            score=score,
            latency_s=time.time() - self._pending_chat_ts,
            kind="chat-joke",
        )
        log.info("humor.scored", kind="chat-joke", score=round(score, 2), preview=text[:60])
        self._pending_chat = None
        await self._maybe_save_lesson()

    async def note_sticker_reaction(self, emoji_or_sticker: str) -> None:
        """Tier B #8: a sticker or emoji reaction is a strong positive humor
        signal. Attribute it to whichever outbound is pending."""
        sig = (emoji_or_sticker or "").strip()
        if not sig:
            return
        if self._pending_outbound:
            await self.memory.record_humor_event(
                outbound=self._pending_outbound,
                user_reply=sig[:80],
                score=0.8,
                latency_s=time.time() - self._pending_ts,
                kind="sticker",
            )
            log.info("humor.scored", kind="sticker", preview=sig[:40])
            self._pending_outbound = None
        elif self._pending_chat:
            await self.memory.record_humor_event(
                outbound=self._pending_chat,
                user_reply=sig[:80],
                score=0.8,
                latency_s=time.time() - self._pending_chat_ts,
                kind="sticker",
            )
            log.info("humor.scored", kind="sticker", preview=sig[:40])
            self._pending_chat = None
        await self._maybe_save_lesson()

    @staticmethod
    def _score_text(text: str) -> float:
        if _POSITIVE.search(text):
            return 1.0
        if _NEGATIVE.search(text):
            return -0.8
        if _is_emoji_only(text):
            return 0.8
        if len(text) > 80:
            return 0.4
        if len(text) < 4:
            return -0.3
        return 0.1

    async def _maybe_save_lesson(self) -> None:
        """Tier B #8: when a bit lands repeatedly (>=3 events, avg >= 0.5),
        auto-feed save_lesson so durable comedic instincts enter memory."""
        try:
            top = await self.memory.humor_top_bits(limit=8)
        except Exception as e:
            log.debug("humor.top_bits_failed", error=str(e))
            return
        for bit in top:
            outbound = bit["outbound"]
            if bit["count"] < 3 or bit["avg_score"] < 0.5:
                continue
            key = outbound[:60]
            if key in self._lesson_done:
                continue
            self._lesson_done.add(key)
            lesson = (
                f"A bit that consistently lands: \"{outbound[:120]}\" "
                f"(landed {bit['count']}x, avg reaction {bit['avg_score']:.2f}). "
                "Keep this instinct — timing, tone, the setup — but don't repeat "
                "it verbatim. Variations of it are part of her comedic voice."
            )
            try:
                if self._agent is not None:
                    save = getattr(self._agent.tools, "save_lesson", None)
                    if callable(save):
                        await save(lesson, context="humor_tracker", tags=["humor", "auto"])
                        continue
                # No agent bound — write directly to memory as a lesson.
                await self.memory.add_lesson(lesson, context="humor_tracker", tags=["humor", "auto"])
            except Exception as e:
                log.debug("humor.save_lesson_failed", error=str(e))

    async def hints_for_prompt(self, limit: int = 4) -> str:
        hits = await self.memory.humor_hints(limit=limit)
        if not hits:
            return ""
        lines = ["# Humor calibration (what landed vs flopped)"]
        for h in hits:
            tag = "landed" if h["score"] >= 0.5 else "flopped" if h["score"] <= -0.2 else "meh"
            lines.append(f"- [{tag}] you: {h['outbound'][:100]}")
            if h.get("user_reply"):
                lines.append(f"  them: {h['user_reply'][:80]}")
        lines.append("Lean into what landed; avoid repeating what flopped.")
        return "\n".join(lines)
