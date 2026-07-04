"""Tests for timezone resolution on Termux/minimal installs."""

from __future__ import annotations

from datetime import timezone
from unittest.mock import patch

from ophelia.timeutil import resolve_timezone


def test_resolve_timezone_uses_configured_name():
    tz = resolve_timezone("America/New_York")
    assert str(tz) in ("America/New_York", "EST", "EDT") or "New_York" in str(tz)


def test_resolve_timezone_falls_back_to_stdlib_utc_when_no_tzdata():
    from zoneinfo import ZoneInfoNotFoundError

    with patch("ophelia.timeutil.ZoneInfo", side_effect=ZoneInfoNotFoundError("UTC")):
        tz = resolve_timezone("UTC")
    assert tz is timezone.utc
