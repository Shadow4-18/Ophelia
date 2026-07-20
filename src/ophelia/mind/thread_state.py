"""Persistent thread awareness — feel the weight of a long conversation.

Beyond injecting the last N messages, keep a rolling per-channel thread
state: emotional tone, open loops, promised follow-ups, and how long the
thread has been alive. Updated heuristically after each owner turn and
injected next to psyche so continuity is more than a fact dump.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from ophelia.memory.store import MemoryStore

log = structlog.get_logger()

_FACT_PREFIX = "thread_state:"

_PROMISE = re.compile(
    r"\b(i(?:'|’)ll|i will|let me|remind me|don(?:'|’)t let me forget|"
    r"we should|next time|later(?:\s+i(?:'|’)ll)?|i(?:'|’)m going to|"
    r"i(?:'|’)ll get back|circle back|follow up)\b",
    re.I,
)
_QUESTION = re.compile(r"\?\s*$|\b(what|why|how|when|where|who)\b.+\?", re.I)
_WARM = re.compile(
    r"\b(love|miss you|thank you|thanks|grateful|proud|warm|soft|"
    r"care about|mean a lot|❤️|💜|💕)\b",
    re.I,
)
_TENSE = re.compile(
    r"\b(angry|frustrated|upset|hurt|annoyed|pissed|mad at|"
    r"leave me alone|stop it)\b",
    re.I,
)
_PLAYFUL = re.compile(
    r"\b(lol|lmao|haha|hehe|joke|tease|silly|fun|play|"
    r"😂|🤣|💀)\b",
    re.I,
)


@dataclass
class ThreadState:
    channel: str
    started_at: float = field(default_factory=time.time)
    last_at: float = field(default_factory=time.time)
    turn_count: int = 0
    tone: str = "neutral"  # warm | playful | tense | curious | neutral
    open_loops: list[str] = field(default_factory=list)
    callbacks: list[str] = field(default_factory=list)
    last_user_snippet: str = ""
    last_assistant_snippet: str = ""
    emotional_weight: float = 0.0  # 0..1 — accumulates with duration + intensity

    def hours_alive(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self.started_at <= 0:
            return 0.0
        return max(0.0, (now - self.started_at) / 3600.0)

    def to_context_block(self) -> str:
        hours = self.hours_alive()
        if self.turn_count < 1:
            return ""
        duration = (
            f"{hours:.1f}h" if hours >= 1 else f"{max(1, int(hours * 60))}m"
        )
        weight = (
            "heavy"
            if self.emotional_weight >= 0.7
            else "settled"
            if self.emotional_weight >= 0.4
            else "light"
        )
        lines = [
            "# Thread continuity (feel this — not just facts)",
            f"- This thread has been alive ~{duration} across {self.turn_count} turns.",
            f"- Emotional weight: {weight} ({self.emotional_weight:.2f}); tone: {self.tone}.",
        ]
        if self.open_loops:
            loops = "; ".join(self.open_loops[:4])
            lines.append(f"- Open loops (unfinished): {loops}")
        if self.callbacks:
            cbs = "; ".join(self.callbacks[:3])
            lines.append(f"- Promised follow-ups: {cbs}")
        if self.last_user_snippet:
            lines.append(f"- Last they said: {self.last_user_snippet[:120]}")
        lines.append(
            "Carry the weight of this conversation. Callbacks and open loops "
            "matter more than starting fresh."
        )
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str, *, channel: str = "") -> ThreadState:
        data = json.loads(raw)
        return cls(
            channel=str(data.get("channel") or channel),
            started_at=float(data.get("started_at") or time.time()),
            last_at=float(data.get("last_at") or time.time()),
            turn_count=int(data.get("turn_count") or 0),
            tone=str(data.get("tone") or "neutral"),
            open_loops=list(data.get("open_loops") or [])[:8],
            callbacks=list(data.get("callbacks") or [])[:6],
            last_user_snippet=str(data.get("last_user_snippet") or "")[:200],
            last_assistant_snippet=str(data.get("last_assistant_snippet") or "")[:200],
            emotional_weight=float(data.get("emotional_weight") or 0.0),
        )


class ThreadAwareness:
    """Load / update / inject rolling thread state per channel."""

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def _key(self, channel: str) -> str:
        return f"{_FACT_PREFIX}{channel}"

    async def load(self, channel: str) -> ThreadState | None:
        if not channel:
            return None
        raw = await self.memory.get_fact(self._key(channel))
        if not raw:
            return None
        try:
            return ThreadState.from_json(raw, channel=channel)
        except Exception as e:
            log.debug("thread_state.load_failed", error=str(e), channel=channel)
            return None

    async def save(self, state: ThreadState) -> None:
        await self.memory.set_fact(self._key(state.channel), state.to_json())

    async def context_block(self, channel: str) -> str:
        state = await self.load(channel)
        if not state:
            return ""
        # Stale threads (>18h quiet) soft-reset weight so "good morning"
        # doesn't inherit last night's intensity as if it never paused.
        quiet_h = (time.time() - state.last_at) / 3600.0
        if quiet_h > 18:
            return (
                f"# Thread continuity\n"
                f"- Prior thread with this person existed ({state.turn_count} turns, "
                f"tone was {state.tone}). Quiet for {quiet_h:.0f}h — greet the "
                f"reunion; don't pretend the last moment never paused."
                + (
                    f"\n- Still-open from before: {'; '.join(state.open_loops[:3])}"
                    if state.open_loops
                    else ""
                )
            )
        return state.to_context_block()

    async def observe_turn(
        self,
        channel: str,
        *,
        user_text: str,
        assistant_text: str,
    ) -> ThreadState:
        """Update thread state after an owner chat turn."""
        state = await self.load(channel) or ThreadState(channel=channel)
        now = time.time()
        # Gap > 6h starts a soft new session but keeps open loops.
        if state.last_at and (now - state.last_at) > 6 * 3600:
            state.started_at = now
            state.turn_count = 0
            state.emotional_weight *= 0.4
        state.last_at = now
        state.turn_count += 1
        state.last_user_snippet = (user_text or "").strip()[:160]
        state.last_assistant_snippet = (assistant_text or "").strip()[:160]
        state.tone = _infer_tone(user_text, assistant_text, state.tone)
        state.open_loops = _merge_loops(
            state.open_loops, user_text, assistant_text
        )
        state.callbacks = _merge_callbacks(state.callbacks, assistant_text)
        # Weight grows with duration + emotional signals.
        hours = state.hours_alive(now)
        intensity = 0.15 if state.tone in ("warm", "tense") else 0.05
        if _WARM.search(user_text or "") or _TENSE.search(user_text or ""):
            intensity += 0.1
        state.emotional_weight = min(
            1.0,
            state.emotional_weight * 0.92 + intensity + min(0.2, hours * 0.04),
        )
        await self.save(state)
        return state


def _infer_tone(user_text: str, assistant_text: str, prior: str) -> str:
    blob = f"{user_text or ''} {assistant_text or ''}"
    if _TENSE.search(blob):
        return "tense"
    if _WARM.search(blob):
        return "warm"
    if _PLAYFUL.search(blob):
        return "playful"
    if _QUESTION.search(user_text or ""):
        return "curious"
    return prior or "neutral"


def _snip(text: str, limit: int = 90) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t[:limit]


def _merge_loops(
    existing: list[str], user_text: str, assistant_text: str
) -> list[str]:
    loops = list(existing)
    # User questions become open loops until something resolves them.
    for text, role in ((user_text, "they asked"), (assistant_text, "you raised")):
        if text and _QUESTION.search(text):
            item = f"{role}: {_snip(text)}"
            if item not in loops:
                loops.append(item)
    # Cheap resolution: if assistant answered without a new question, drop
    # the oldest user question loop.
    if assistant_text and not _QUESTION.search(assistant_text):
        for i, item in enumerate(loops):
            if item.startswith("they asked:"):
                loops.pop(i)
                break
    return loops[-8:]


def _merge_callbacks(existing: list[str], assistant_text: str) -> list[str]:
    cbs = list(existing)
    text = (assistant_text or "").strip()
    if text and _PROMISE.search(text):
        item = _snip(text, 100)
        if item and item not in cbs:
            cbs.append(item)
    return cbs[-6:]


def thread_state_from_dict(data: dict[str, Any]) -> ThreadState:
    """Test helper."""
    return ThreadState.from_json(json.dumps(data))
