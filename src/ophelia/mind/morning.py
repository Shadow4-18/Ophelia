"""Dream -> wake continuity (Tier B #10).

Dreams are extracted by DreamLoop but, before this module, they didn't
reliably surface as "I had a weird dream about…" the next morning. The sleep
cycle loop felt open-ended.

This module closes that loop:

  - `record_dream(dream_text)` is called by DreamLoop when a dream is produced.
  - `pending_morning_reference()` returns a short narrative the next time the
    owner transitions from asleep -> awake (detected by LifeContext).
  - After it's been surfaced once, it's cleared — no repeats.

Stored in the SQLite memory DB as facts so it survives restarts.
"""

from __future__ import annotations

import time

import structlog

from ophelia.memory.store import MemoryStore

log = structlog.get_logger()

_DREAM_KEY = "dream:last_narrative"
_SURFACED_KEY = "dream:last_surfaced_at"
# A dream is worth referencing within this many hours of being dreamt.
_FRESH_HOURS = 10.0


class DreamContinuity:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    async def record_dream(self, dream_text: str) -> None:
        t = (dream_text or "").strip()
        if not t:
            return
        await self.memory.set_fact(_DREAM_KEY, t[:800])
        # Reset surfaced flag so this dream can be referenced next wake.
        await self.memory.set_fact(_SURFACED_KEY, "0")
        log.info("dream.recorded_for_morning", preview=t[:80])

    async def pending_morning_reference(self) -> str | None:
        """Return a dream narrative to surface on wake, or None if not fresh /
        already surfaced / never dreamt."""
        dream, ts = await self.memory.get_fact_with_ts(_DREAM_KEY)
        if not dream:
            return None
        surfaced = await self.memory.get_fact(_SURFACED_KEY)
        if surfaced and surfaced not in ("0", "", "None"):
            return None  # already surfaced
        if ts is not None:
            age_h = (time.time() - ts) / 3600.0
            if age_h > _FRESH_HOURS:
                return None
        return dream

    async def mark_surfaced(self) -> None:
        await self.memory.set_fact(_SURFACED_KEY, str(time.time()))
