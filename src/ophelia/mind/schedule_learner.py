"""Learn the owner's schedule from observed Telegram activity (Tier B #6).

LifeContext today uses static WORK_DAYS / WORK_HOURS from .env, which means the
owner hand-maintains it and it drifts when shifts change. This module logs
inbound owner activity by (day-of-week, hour) into SQLite and infers:

  - quiet windows (hours where the owner is historically silent)
  - active windows (hours where the owner is historically chatty)
  - a learned schedule that complements (not replaces) the static .env one

The learned schedule is consulted by LifeContext to sharpen "is he home / awake
/ at work" beyond the static schedule. The static schedule stays as a fallback
for fresh installs and for shifts the learner hasn't seen enough data for.

Schema (new table `owner_activity`):
  - dow INTEGER (0=Mon..6=Sun)
  - hour INTEGER (0..23)
  - count INTEGER
  - last_seen REAL
"""

from __future__ import annotations

import time
from datetime import datetime

import aiosqlite
import structlog

from ophelia.memory.store import MemoryStore
from ophelia.config import Settings

log = structlog.get_logger()

# Number of distinct activity samples we want before trusting learned data over
# the static .env schedule. Below this, the static schedule wins.
_MIN_SAMPLES = 14
# A hour-of-week is considered "quiet" if its activity count is < this fraction
# of the median active hour. Tuned for sparse Telegram use.
_QUIET_FRACTION = 0.20
# Old activity fades: a sample counts half after this long, so a new job or
# sleep schedule shows within a few weeks instead of months.
_HALF_LIFE_S = 21 * 86400


def _faded(count: float, last_seen: float, now: float) -> float:
    return float(count) * 0.5 ** (max(0.0, now - float(last_seen or now)) / _HALF_LIFE_S)


class ScheduleLearner:
    def __init__(self, memory: MemoryStore, settings: Settings) -> None:
        self.memory = memory
        self.settings = settings
        self._cached: dict[int, dict[int, int]] | None = None
        self._cached_at: float = 0.0

    async def record_owner_activity(self, dt: datetime | None = None) -> None:
        """Call whenever the owner sends a message. Logs (dow, hour)."""
        if dt is None:
            dt = datetime.now(tz=self._tz())
        dow = dt.weekday()  # 0=Mon..6=Sun
        hour = dt.hour
        now = time.time()
        async with aiosqlite.connect(self.memory.db_path) as db:
            cursor = await db.execute(
                "SELECT count, last_seen FROM owner_activity WHERE dow = ? AND hour = ?",
                (dow, hour),
            )
            row = await cursor.fetchone()
            count = (_faded(row[0], row[1], now) if row else 0.0) + 1.0
            await db.execute(
                """
                INSERT INTO owner_activity (dow, hour, count, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(dow, hour) DO UPDATE SET
                    count = excluded.count,
                    last_seen = excluded.last_seen
                """,
                (dow, hour, count, now),
            )
            await db.commit()
        # Invalidate the cache so the next read picks up the new sample.
        self._cached = None

    def _tz(self):
        from ophelia.timeutil import resolve_timezone

        return resolve_timezone(self.settings.timezone)

    async def _load_counts(self) -> dict[int, dict[int, float]]:
        """Return {dow: {hour: faded count}}. Cached for 5 minutes."""
        if self._cached is not None and time.time() - self._cached_at < 300:
            return self._cached
        result: dict[int, dict[int, int]] = {d: {} for d in range(7)}
        try:
            async with aiosqlite.connect(self.memory.db_path) as db:
                cursor = await db.execute(
                    "SELECT dow, hour, count, last_seen FROM owner_activity"
                )
                rows = await cursor.fetchall()
        except aiosqlite.OperationalError:
            # Table doesn't exist yet (init hasn't run). Treat as empty.
            rows = []
        total = 0.0
        now = time.time()
        for dow, hour, count, last_seen in rows:
            value = _faded(count, last_seen, now)
            result[int(dow)][int(hour)] = value
            total += value
        self._cached = result
        self._cached_at = time.time()
        log.debug("schedule_learner.loaded", samples=total)
        return result

    async def total_samples(self) -> int:
        counts = await self._load_counts()
        return int(round(sum(sum(h.values()) for h in counts.values())))

    async def quiet_hours_for(self, dow: int) -> list[int]:
        """Hours of the given day-of-week where the owner is historically quiet.

        Returns [] until enough samples have been collected — below the
        threshold the static .env schedule should be used instead.
        """
        if await self.total_samples() < _MIN_SAMPLES:
            return []
        counts = await self._load_counts()
        day = counts.get(dow, {})
        if not day:
            return []
        # Compare against the median of hours that have *some* activity.
        active = sorted(c for c in day.values() if c > 0)
        if not active:
            return list(range(24))  # entirely silent day
        median = active[len(active) // 2]
        if median == 0:
            return []
        threshold = max(0.5, median * _QUIET_FRACTION)
        return [h for h in range(24) if day.get(h, 0) < threshold]

    async def is_likely_quiet_now(self, dt: datetime | None = None) -> bool:
        """Is the owner historically quiet at this day/hour?"""
        if dt is None:
            dt = datetime.now(tz=self._tz())
        quiet = await self.quiet_hours_for(dt.weekday())
        return dt.hour in quiet

    async def learned_summary(self) -> str:
        """Human-readable summary of the learned schedule for the prompt.

        Empty when there isn't enough data yet, so the prompt stays clean.
        """
        total = await self.total_samples()
        if total < _MIN_SAMPLES:
            return ""
        lines = ["Learned owner schedule (from observed activity, ~{} samples):".format(total)]
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        counts = await self._load_counts()
        for dow in range(7):
            day = counts.get(dow, {})
            quiet = await self.quiet_hours_for(dow)
            if not quiet:
                continue
            # Compact hour ranges e.g. "00-06, 22-23"
            ranges = _compact_hours(quiet)
            lines.append(f"  {day_names[dow]}: quiet {', '.join(ranges)}")
        if len(lines) == 1:
            return ""
        lines.append(
            "This complements your .env schedule — trust observed patterns "
            "over the static one when they disagree."
        )
        return "\n".join(lines)


def _compact_hours(hours: list[int]) -> list[str]:
    """Turn [0,1,2,3,22,23] into ['00-04', '22-24'] (hour ranges, end-exclusive
    displayed as the next hour for readability)."""
    if not hours:
        return []
    sorted_h = sorted(set(hours))
    ranges: list[str] = []
    start = sorted_h[0]
    prev = start
    for h in sorted_h[1:]:
        if h == prev + 1:
            prev = h
            continue
        ranges.append(f"{start:02d}-{prev + 1:02d}")
        start = h
        prev = h
    ranges.append(f"{start:02d}-{prev + 1:02d}")
    return ranges
