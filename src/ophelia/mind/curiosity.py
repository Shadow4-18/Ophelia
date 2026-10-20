"""Curiosity trails — rabbit holes she chooses, not a calendar of goals.

When idle, prefer continuing an active trail over rotating generic nudges.
Consciousness (and tools) can open / deepen / abandon trails so exploration
feels like an urge rather than a scheduled chore.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

import structlog

from ophelia.memory.store import MemoryStore

log = structlog.get_logger()

_FACT_KEY = "curiosity_trail:active"
_MAX_DEPTH = 8


@dataclass
class CuriosityTrail:
    topic: str
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    depth: int = 1
    next_step: str = ""
    notes: list[str] = field(default_factory=list)
    status: str = "active"  # active | satisfied | abandoned

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> CuriosityTrail:
        data = json.loads(raw)
        return cls(
            topic=str(data.get("topic") or "").strip(),
            started_at=float(data.get("started_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            depth=max(1, int(data.get("depth") or 1)),
            next_step=str(data.get("next_step") or "").strip(),
            notes=list(data.get("notes") or [])[-12:],
            status=str(data.get("status") or "active"),
        )

    def age_hours(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        return max(0.0, (now - self.started_at) / 3600.0)

    def to_context_block(self) -> str:
        if self.status != "active" or not self.topic:
            return ""
        age = self.age_hours()
        age_s = f"{age:.1f}h" if age >= 1 else f"{int(age * 60)}m"
        lines = [
            "# Active curiosity trail (your rabbit hole — not a goal cadence)",
            f"- Topic: {self.topic}",
            f"- Depth: {self.depth}/{_MAX_DEPTH} · open for {age_s}",
        ]
        if self.next_step:
            lines.append(f"- Next pull: {self.next_step}")
        if self.notes:
            lines.append(f"- Recent notes: {'; '.join(self.notes[-3:])}")
        lines.append(
            "If idle energy wants somewhere to go, prefer continuing this trail "
            "(explore/act/reflect on it) over inventing a new whim — unless it "
            "genuinely went cold. Close it when satisfied or bored of it."
        )
        return "\n".join(lines)

    def idle_nudge(self, idle_minutes: int) -> str:
        if self.status != "active" or not self.topic:
            return ""
        pull = self.next_step or f"one more step into {self.topic}"
        return (
            f"\n\nRABBIT HOLE (optional, preferred over generic idle): you've been "
            f"idle {idle_minutes}m. Your open trail is \"{self.topic}\" "
            f"(depth {self.depth}). If curiosity tugs, continue: {pull}. "
            f"Otherwise stay silent — trails wait."
        )


class CuriosityStore:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def load(self) -> CuriosityTrail | None:
        raw = await self.memory.get_fact(_FACT_KEY)
        if not raw:
            return None
        try:
            trail = CuriosityTrail.from_json(raw)
        except Exception as e:
            log.debug("curiosity.load_failed", error=str(e))
            return None
        if trail.status != "active" or not trail.topic:
            return None
        # Auto-abandon stale trails (>36h untouched).
        if (time.time() - trail.updated_at) > 36 * 3600:
            trail.status = "abandoned"
            await self._save(trail)
            return None
        return trail

    async def _save(self, trail: CuriosityTrail) -> None:
        await self.memory.set_fact(_FACT_KEY, trail.to_json())

    async def open(
        self,
        topic: str,
        *,
        next_step: str = "",
        note: str = "",
    ) -> CuriosityTrail:
        topic = (topic or "").strip()
        if not topic:
            raise ValueError("topic required")
        existing = await self.load()
        if existing and existing.topic.lower() == topic.lower():
            return await self.deepen(next_step=next_step, note=note)
        trail = CuriosityTrail(
            topic=topic[:160],
            next_step=(next_step or "").strip()[:200],
            notes=[note.strip()[:160]] if note.strip() else [],
        )
        await self._save(trail)
        log.info("curiosity.opened", topic=trail.topic)
        return trail

    async def deepen(
        self,
        *,
        next_step: str = "",
        note: str = "",
    ) -> CuriosityTrail:
        trail = await self.load()
        if not trail:
            raise ValueError("no active curiosity trail")
        trail.depth = min(_MAX_DEPTH, trail.depth + 1)
        trail.updated_at = time.time()
        if next_step.strip():
            trail.next_step = next_step.strip()[:200]
        if note.strip():
            trail.notes = (trail.notes + [note.strip()[:160]])[-12:]
        if trail.depth >= _MAX_DEPTH:
            trail.status = "satisfied"
            await self._save(trail)
            log.info("curiosity.satisfied", topic=trail.topic, depth=trail.depth)
            return trail
        await self._save(trail)
        return trail

    async def close(self, *, reason: str = "satisfied") -> CuriosityTrail | None:
        trail = await self.load()
        if not trail:
            return None
        trail.status = "abandoned" if reason == "abandoned" else "satisfied"
        trail.updated_at = time.time()
        if reason and reason not in ("satisfied", "abandoned"):
            trail.notes = (trail.notes + [f"closed: {reason[:120]}"])[-12:]
        await self._save(trail)
        log.info("curiosity.closed", topic=trail.topic, status=trail.status)
        return trail

    async def context_block(self) -> str:
        trail = await self.load()
        return trail.to_context_block() if trail else ""

    async def maybe_note_explore(self, thought: str, action: str) -> None:
        """Lightweight heuristic: explore/act thoughts can deepen an open trail
        or seed a new one when curiosity is clearly pointed."""
        thought = (thought or "").strip()
        if not thought or len(thought) < 12:
            return
        if action not in ("explore", "act", "reflect"):
            return
        trail = await self.load()
        if trail:
            # Same rabbit hole — deepen with a short note.
            await self.deepen(note=thought[:120])
            return
        # Seed a new trail from a pointed explore thought.
        if action == "explore":
            topic = thought.split(".")[0].strip()[:120]
            if topic:
                try:
                    await self.open(topic, next_step="follow the interesting bit", note=thought[:120])
                except Exception as e:
                    log.debug("curiosity.auto_open_failed", error=str(e))
