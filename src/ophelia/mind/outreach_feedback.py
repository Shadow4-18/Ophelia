"""Learn how welcome her unprompted messages are, and adjust how often she
reaches out.

Every spontaneous message is already logged as an ``outreach`` humor event.
Here we look at the most recent ones and ask a simple question: did the
owner write back within a couple of hours? If most outreach goes
unanswered she raises her initiative threshold a little (quieter); if it's
answered quickly she lowers it a little (more forward). The adjustment is
small and bounded, so personality settings stay in charge. No LLM call.
"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

WINDOW = 12            # most recent outreach events considered
MIN_EVENTS = 4         # don't adjust on too little evidence
REPLY_WINDOW_S = 2 * 3600
MAX_QUIETER = 0.10     # threshold can rise at most this much
MAX_FORWARD = 0.05     # …and fall at most this much
CACHE_S = 600


class OutreachFeedback:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._cached: tuple[float, float, float] | None = None  # (at, delta, rate)

    async def reply_rate(self, now: float | None = None) -> tuple[float, int]:
        """(fraction of recent outreach answered within the window, count)."""
        now = now or time.time()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute(
                    """
                    SELECT h.created_at,
                           EXISTS (
                             SELECT 1 FROM messages m
                             WHERE m.role = 'user'
                               AND m.channel != 'consciousness'
                               AND COALESCE(m.metadata, '') NOT LIKE '%consciousness_tick%'
                               AND m.created_at > h.created_at
                               AND m.created_at <= h.created_at + ?
                           ) AS replied
                    FROM humor_events h
                    WHERE h.kind = 'outreach' AND h.created_at <= ?
                    ORDER BY h.created_at DESC
                    LIMIT ?
                    """,
                    (REPLY_WINDOW_S, now - REPLY_WINDOW_S, WINDOW),
                )
                rows = await cursor.fetchall()
            except aiosqlite.OperationalError:
                return 0.5, 0
        if not rows:
            return 0.5, 0
        return sum(1 for r in rows if r[1]) / len(rows), len(rows)

    async def threshold_delta(self) -> float:
        """Positive = reach out less; negative = reach out more."""
        now = time.time()
        if self._cached and now - self._cached[0] < CACHE_S:
            return self._cached[1]
        rate, n = await self.reply_rate(now)
        if n < MIN_EVENTS:
            delta = 0.0
        elif rate < 0.5:
            delta = MAX_QUIETER * (0.5 - rate) / 0.5
        else:
            delta = -MAX_FORWARD * (rate - 0.5) / 0.5
        self._cached = (now, delta, rate)
        return delta

    def last_rate(self) -> float | None:
        return self._cached[2] if self._cached else None
