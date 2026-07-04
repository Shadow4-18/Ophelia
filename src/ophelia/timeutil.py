"""Timezone resolution that works on Termux and other minimal Python installs.

Python 3.9+ zoneinfo needs the IANA database. On Windows and many Android
(Termux) installs it is NOT bundled — `ZoneInfo('UTC')` raises
ZoneInfoNotFoundError. LifeContext, schedule learning, and initiative all
depend on a working tz; without a fallback every chat turn dies.

Resolution order:
  1. ZoneInfo(configured name, e.g. America/New_York)
  2. ZoneInfo('UTC')
  3. datetime.timezone.utc (stdlib — always available)

Also add `tzdata` to project dependencies so `pip install -e .` fixes Termux.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger()

_warned_no_tzdata = False


def resolve_timezone(name: str | None):
    """Return a tzinfo for `name` (IANA key). Never raises."""
    global _warned_no_tzdata
    raw = (name or "UTC").strip() or "UTC"
    for candidate in (raw, "UTC"):
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
    if not _warned_no_tzdata:
        _warned_no_tzdata = True
        log.warning(
            "timezone.no_tzdata",
            configured=raw,
            hint="pip install tzdata  (or set OPHELIA_TIMEZONE=America/New_York in ~/.ophelia/.env)",
        )
    return timezone.utc


def now_in_timezone(name: str | None) -> datetime:
    """Current time in the configured timezone."""
    return datetime.now(tz=resolve_timezone(name))
