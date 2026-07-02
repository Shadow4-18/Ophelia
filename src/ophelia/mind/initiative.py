"""Rate limits and quiet hours — tune aliveness without spam."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ophelia.config import OPHELIA_HOME


@dataclass
class InitiativeGovernor:
    max_spontaneous_per_hour: int = 4
    quiet_hours: str = ""  # e.g. "23-08" = 11pm to 8am
    timezone: str = "UTC"
    log_path: Path = field(default_factory=lambda: OPHELIA_HOME / "data" / "initiative_log.jsonl")
    _recent: list[float] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings) -> InitiativeGovernor:
        return cls(
            max_spontaneous_per_hour=settings.max_spontaneous_per_hour,
            quiet_hours=settings.quiet_hours or "",
            timezone=settings.timezone or "UTC",
            log_path=settings.data_dir / "initiative_log.jsonl",
        )

    def _local_hour(self) -> int:
        try:
            tz = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        return datetime.now(tz=tz).hour

    def _in_quiet_hours(self) -> bool:
        raw = self.quiet_hours.strip()
        if not raw or "-" not in raw:
            return False
        try:
            start_s, end_s = raw.split("-", 1)
            start_h = int(start_s.strip())
            end_h = int(end_s.strip())
        except ValueError:
            return False
        hour = self._local_hour()
        if start_h <= end_h:
            return start_h <= hour < end_h
        return hour >= start_h or hour < end_h

    def _prune_recent(self) -> None:
        cutoff = time.time() - 3600
        self._recent = [t for t in self._recent if t >= cutoff]

    def allow_outreach(self) -> tuple[bool, str]:
        if self._in_quiet_hours():
            return False, "quiet_hours"
        self._prune_recent()
        if len(self._recent) >= self.max_spontaneous_per_hour:
            return False, "rate_limit"
        return True, "ok"

    def record_outreach(self, action: str, pressure: float, outward: str) -> None:
        now = time.time()
        self._recent.append(now)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": now,
            "action": action,
            "pressure": round(pressure, 3),
            "preview": outward[:120],
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
