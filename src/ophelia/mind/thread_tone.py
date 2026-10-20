"""Per-conversation tone — feel the weight of a long thread, not just facts.

Tracks, per owner channel, how long the current conversation has been going,
its tone (warm / playful / tense / curious / neutral) and an emotional weight
that builds with duration and intensity. Injected next to her follow-ups
(mind/open_loops.py, which owns promises and unfinished things) so she
carries the mood of a conversation instead of starting fresh every message.

Heuristic regexes only — no model call, cheap on a phone. Adapted from the
thread-awareness part of the July "feel more alive" draft; its open-loop
tracking is covered by open_loops.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

_FACT_PREFIX = "thread_tone:"
NEW_SESSION_GAP_S = 6 * 3600
STALE_S = 18 * 3600

_QUESTION = re.compile(r"\?\s*$|\b(what|why|how|when|where|who)\b.+\?", re.I)
_WARM = re.compile(
    r"\b(love|miss you|thank you|thanks|grateful|proud|care about|mean a lot)\b|❤️|💜|💕",
    re.I,
)
_TENSE = re.compile(
    r"\b(angry|frustrated|upset|hurt|annoyed|pissed|mad at|leave me alone|stop it)\b",
    re.I,
)
_PLAYFUL = re.compile(r"\b(lol|lmao|haha|hehe|joke|tease|silly)\b|😂|🤣|💀", re.I)


@dataclass
class ThreadTone:
    channel: str
    started_at: float = field(default_factory=time.time)
    last_at: float = field(default_factory=time.time)
    turn_count: int = 0
    tone: str = "neutral"
    emotional_weight: float = 0.0  # 0..1

    def context_block(self, now: float | None = None) -> str:
        now = now or time.time()
        if self.turn_count < 1:
            return ""
        quiet = now - self.last_at
        if quiet > STALE_S:
            return (
                "# This conversation\n"
                f"- You two last talked {quiet / 3600:.0f}h ago (tone was {self.tone}). "
                "Greet the reunion; don't act like the last moment never paused."
            )
        hours = max(0.0, (now - self.started_at) / 3600.0)
        duration = f"{hours:.1f}h" if hours >= 1 else f"{max(1, int(hours * 60))}m"
        weight = (
            "heavy" if self.emotional_weight >= 0.7
            else "settled" if self.emotional_weight >= 0.4
            else "light"
        )
        return (
            "# This conversation (feel it, don't recite it)\n"
            f"- Going ~{duration} across {self.turn_count} turns; tone: {self.tone}; "
            f"emotional weight: {weight}."
        )


def infer_tone(user_text: str, assistant_text: str, prior: str) -> str:
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


class ThreadTones:
    def __init__(self, memory) -> None:
        self.memory = memory

    async def load(self, channel: str) -> ThreadTone | None:
        raw = await self.memory.get_fact(_FACT_PREFIX + channel)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return ThreadTone(**{k: data[k] for k in ThreadTone.__dataclass_fields__ if k in data})
        except (json.JSONDecodeError, TypeError):
            return None

    async def context_block(self, channel: str) -> str:
        state = await self.load(channel)
        return state.context_block() if state else ""

    async def observe_turn(self, channel: str, user_text: str, assistant_text: str) -> ThreadTone:
        now = time.time()
        state = await self.load(channel) or ThreadTone(channel=channel)
        if now - state.last_at > NEW_SESSION_GAP_S:
            state.started_at = now
            state.turn_count = 0
            state.emotional_weight *= 0.4
        state.last_at = now
        state.turn_count += 1
        state.tone = infer_tone(user_text, assistant_text, state.tone)
        hours = (now - state.started_at) / 3600.0
        intensity = 0.15 if state.tone in ("warm", "tense") else 0.05
        if _WARM.search(user_text or "") or _TENSE.search(user_text or ""):
            intensity += 0.1
        state.emotional_weight = min(
            1.0, state.emotional_weight * 0.92 + intensity + min(0.2, hours * 0.04)
        )
        await self.memory.set_fact(_FACT_PREFIX + channel, json.dumps(asdict(state)))
        return state
