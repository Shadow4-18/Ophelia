"""Tests for timezone resolution on Termux/minimal installs."""

from __future__ import annotations

from datetime import timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ophelia.timeutil import (
    configured_timezone_label,
    is_system_timezone,
    normalize_timezone_name,
    resolve_timezone,
    system_timezone_name,
    validate_timezone_name,
)


def test_resolve_timezone_uses_configured_name():
    tz = resolve_timezone("America/New_York")
    assert str(tz) in ("America/New_York", "EST", "EDT") or "New_York" in str(tz)


def test_resolve_timezone_falls_back_to_stdlib_utc_when_no_tzdata():
    from zoneinfo import ZoneInfoNotFoundError

    with patch("ophelia.timeutil.ZoneInfo", side_effect=ZoneInfoNotFoundError("UTC")):
        tz = resolve_timezone("UTC")
    assert tz is timezone.utc


def test_normalize_est_abbrev_to_iana():
    assert normalize_timezone_name("EST") == "America/New_York"
    assert normalize_timezone_name("eastern") == "America/New_York"
    assert normalize_timezone_name("PST") == "America/Los_Angeles"


def test_normalize_system_aliases():
    assert normalize_timezone_name("system") == "system"
    assert normalize_timezone_name("local") == "system"
    assert normalize_timezone_name("auto") == "system"
    assert normalize_timezone_name("") == "system"
    assert is_system_timezone("system")
    assert is_system_timezone(None)


def test_normalize_utc_offset():
    assert normalize_timezone_name("UTC-5") == "Etc/GMT+5"
    assert normalize_timezone_name("GMT+2") == "Etc/GMT-2"
    assert normalize_timezone_name("UTC+0") == "UTC"


def test_resolve_system_timezone():
    tz = resolve_timezone("system")
    assert tz is not None
    # Should match whatever system_timezone_name resolves to (or UTC fallback).
    host = system_timezone_name()
    assert isinstance(tz, (ZoneInfo, type(timezone.utc)))
    label = configured_timezone_label("system")
    assert label.startswith("system (")
    assert host in label


def test_validate_timezone_accepts_est_and_system():
    ok, normalized, detail = validate_timezone_name("EST")
    assert ok
    assert normalized == "America/New_York"
    assert "America/New_York" in detail

    ok, normalized, detail = validate_timezone_name("system")
    assert ok
    assert normalized == "system"
    assert "system" in detail.lower()


def test_validate_timezone_rejects_garbage():
    ok, normalized, detail = validate_timezone_name("Not/ARealZone")
    assert not ok
    assert normalized == ""
    assert "Unknown" in detail
