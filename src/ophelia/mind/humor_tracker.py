"""Track what lands vs falls flat — adaptive humor hints for the prompt."""

from __future__ import annotations

import re
import time

import structlog

from ophelia.memory.store import MemoryStore

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


class HumorTracker:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory
        self._pending_outbound: str | None = None
        self._pending_ts: float = 0.0

    async def note_outbound(self, text: str) -> None:
        """Call when Ophelia sends something that might be a joke or quip."""
        t = text.strip()
        if len(t) < 8 or len(t) > 500:
            return
        self._pending_outbound = t
        self._pending_ts = time.time()
        await self.memory.record_humor_outbound(t)

    async def score_inbound_reply(self, user_text: str) -> None:
        """Score reaction to the last outbound humor attempt."""
        if not self._pending_outbound:
            return
        text = user_text.strip()
        score = 0.0
        if _POSITIVE.search(text):
            score = 1.0
        elif _NEGATIVE.search(text):
            score = -0.8
        elif len(text) > 80:
            score = 0.4
        elif len(text) < 4:
            score = -0.3
        else:
            score = 0.1
        await self.memory.record_humor_reaction(
            self._pending_outbound,
            user_reply=text[:400],
            score=score,
            latency_s=time.time() - self._pending_ts,
        )
        log.info("humor.scored", score=round(score, 2), preview=text[:60])
        self._pending_outbound = None

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
