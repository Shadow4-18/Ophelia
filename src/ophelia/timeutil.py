"""Timezone resolution that works on Termux and other minimal Python installs.

Python 3.9+ zoneinfo needs the IANA database. On Windows and many Android
(Termux) installs it is NOT bundled — `ZoneInfo('UTC')` raises
ZoneInfoNotFoundError. LifeContext, schedule learning, and initiative all
depend on a working tz; without a fallback every chat turn dies.

Resolution order for a configured name:
  1. ``system`` / ``local`` / ``auto`` → host local zone (see below)
  2. Common abbreviations (EST, PST, …) → canonical IANA keys
  3. ZoneInfo(configured name, e.g. America/New_York)
  4. ZoneInfo('UTC')
  5. datetime.timezone.utc (stdlib — always available)

Also add `tzdata` to project dependencies so `pip install -e .` fixes Termux.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger()

_warned_no_tzdata = False

# Common civil abbreviations → IANA zones (DST-aware where applicable).
# EST/EDT both map to America/New_York so clocks follow local law, not a
# fixed offset that would be wrong half the year.
_ABBREV_TO_IANA: dict[str, str] = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "EASTERN": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "CENTRAL": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
    "MOUNTAIN": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",
    "GMT": "Etc/GMT",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "JST": "Asia/Tokyo",
    "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
}

_SYSTEM_ALIASES = frozenset({"system", "local", "auto", "host"})


def is_system_timezone(name: str | None) -> bool:
    """True when the configured value means 'follow the host clock'."""
    raw = (name or "").strip().lower()
    return raw in _SYSTEM_ALIASES or raw == ""


def system_timezone_name() -> str:
    """Best-effort IANA key for the host's local timezone.

    Order:
      1. ``$TZ`` if it looks like an IANA key
      2. ``/etc/localtime`` symlink target under zoneinfo
      3. ``datetime.now().astimezone().tzinfo.key`` when present
      4. ``UTC``
    """
    env_tz = (os.environ.get("TZ") or "").strip()
    if env_tz and env_tz.upper() not in _ABBREV_TO_IANA and "/" in env_tz:
        try:
            ZoneInfo(env_tz)
            return env_tz
        except ZoneInfoNotFoundError:
            pass

    localtime = Path("/etc/localtime")
    try:
        if localtime.exists():
            resolved = localtime.resolve()
            parts = resolved.parts
            if "zoneinfo" in parts:
                idx = parts.index("zoneinfo")
                key = "/".join(parts[idx + 1 :])
                if key:
                    try:
                        ZoneInfo(key)
                        return key
                    except ZoneInfoNotFoundError:
                        pass
    except OSError:
        pass

    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    return "UTC"


def normalize_timezone_name(name: str | None) -> str:
    """Canonical config value: ``system``, ``UTC``, or an IANA key.

    Accepts abbreviations (``EST``), IANA names, and system aliases.
    Does not validate that the IANA database is installed — callers that
    need a live ``tzinfo`` should use :func:`resolve_timezone`.
    """
    raw = (name or "").strip()
    if not raw or raw.lower() in _SYSTEM_ALIASES:
        return "system"

    # Fixed offsets like UTC-5 / GMT+2 → Etc/GMT±N (note inverted sign).
    m = re.fullmatch(r"(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::00)?", raw, re.I)
    if m:
        sign, hours_s = m.group(1), m.group(2)
        hours = int(hours_s)
        if hours == 0:
            return "UTC"
        if hours > 14:
            return raw  # leave invalid for resolve_timezone to fall back
        # Etc/GMT+5 means UTC-5 (POSIX sign inversion).
        etc_sign = "-" if sign == "+" else "+"
        return f"Etc/GMT{etc_sign}{hours}"

    upper = raw.upper().replace(" ", "_")
    if upper in _ABBREV_TO_IANA:
        return _ABBREV_TO_IANA[upper]

    # Soft match: "US/Eastern", "us-eastern", "america/new_york"
    soft = raw.replace(" ", "_").replace("-", "/")
    soft_upper = soft.upper()
    if soft_upper in _ABBREV_TO_IANA:
        return _ABBREV_TO_IANA[soft_upper]
    if soft_upper in {"US/EASTERN", "US/EAST"}:
        return "America/New_York"
    if soft_upper in {"US/CENTRAL"}:
        return "America/Chicago"
    if soft_upper in {"US/MOUNTAIN"}:
        return "America/Denver"
    if soft_upper in {"US/PACIFIC", "US/WEST"}:
        return "America/Los_Angeles"
    if soft_upper == "UTC":
        return "UTC"

    return soft


def configured_timezone_label(name: str | None) -> str:
    """Human label for prompts: ``America/New_York`` or ``system (America/Chicago)``."""
    normalized = normalize_timezone_name(name)
    if normalized == "system":
        return f"system ({system_timezone_name()})"
    return normalized


def resolve_timezone(name: str | None) -> tzinfo:
    """Return a tzinfo for `name` (IANA key, abbrev, or system). Never raises."""
    global _warned_no_tzdata
    normalized = normalize_timezone_name(name)
    if normalized == "system":
        candidates = (system_timezone_name(), "UTC")
    else:
        candidates = (normalized, "UTC")

    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue

    # Last resort: fixed UTC. Also try a fixed offset if Etc/GMT failed.
    if not _warned_no_tzdata:
        _warned_no_tzdata = True
        log.warning(
            "timezone.no_tzdata",
            configured=name,
            normalized=normalized,
            hint=(
                "pip install tzdata  (or set OPHELIA_TIMEZONE=America/New_York "
                "or OPHELIA_TIMEZONE=system in ~/.ophelia/.env)"
            ),
        )
    return timezone.utc


def now_in_timezone(name: str | None) -> datetime:
    """Current time in the configured timezone."""
    return datetime.now(tz=resolve_timezone(name))


def validate_timezone_name(name: str | None) -> tuple[bool, str, str]:
    """Validate a user/agent-supplied timezone.

    Returns ``(ok, normalized_or_error, detail)``.
    On success, ``normalized`` is the value to store in ``OPHELIA_TIMEZONE``
    (``system`` or an IANA key) and ``detail`` describes the resolved zone.
    """
    raw = (name or "").strip()
    if not raw:
        return False, "", "Timezone name is empty."

    normalized = normalize_timezone_name(raw)
    if normalized == "system":
        host = system_timezone_name()
        # Ensure we can actually resolve it.
        tz = resolve_timezone("system")
        label = getattr(tz, "key", None) or host
        return True, "system", f"system local time ({label})"

    try:
        tz = ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        return (
            False,
            "",
            (
                f"Unknown timezone '{raw}'. Use an IANA name like "
                "America/New_York, a common abbrev like EST, or 'system'."
            ),
        )

    # Prove "now" works and surface current offset/abbrev for confirmation.
    now = datetime.now(tz=tz)
    offset = now.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    abbrev = now.tzname() or normalized
    return True, normalized, f"{normalized} (now {abbrev}, UTC{offset})"


def apply_timezone_setting(
    settings,
    name: str,
    *,
    persist: bool = True,
    governor=None,
) -> str:
    """Set timezone on the live Settings object, optionally persist to .env.

    Returns a short confirmation string for the tool/user. Raises ValueError
    if the name cannot be validated.
    """
    ok, normalized, detail = validate_timezone_name(name)
    if not ok:
        raise ValueError(detail)

    settings.timezone = normalized
    os.environ["OPHELIA_TIMEZONE"] = normalized

    if governor is not None and hasattr(governor, "timezone"):
        governor.timezone = normalized

    if persist:
        from ophelia.setup.env_io import write_env_updates

        write_env_updates({"OPHELIA_TIMEZONE": normalized})

    now = now_in_timezone(normalized)
    stamp = now.strftime("%A, %B %d, %Y — %I:%M %p %Z")
    return (
        f"Timezone set to {detail}. "
        f"Current local time: {stamp}. "
        f"Stored as OPHELIA_TIMEZONE={normalized}"
        + (" in ~/.ophelia/.env." if persist else ".")
    )
