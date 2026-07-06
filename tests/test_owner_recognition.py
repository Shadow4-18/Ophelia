"""Tests for owner recognition across multiple platforms.

Regression: after enabling Discord, a user who was previously recognized as
owner on Telegram got demoted to guest. Root cause: owner_channels() fell
back to primary_user_channel() when OPHELIA_OWNER_ID was unset, and that
returns ONE platform's channel only — so the Telegram owner silently lost
owner status once Discord was enabled (Discord became the primary).

The fix: when OPHELIA_OWNER_ID is unset, treat the first allowed user on
EACH enabled platform as the owner. Guests are appended to the allowlist
after the owner, so they're never first and never get promoted.
"""

from __future__ import annotations

import os

import pytest


def _settings_with(monkeypatch, tmp_path, **env) -> "Settings":  # type: ignore[name-defined]
    from ophelia.config import Settings

    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path / "ophelia_home"))
    # Clear any inherited owner-related env so tests are hermetic.
    for k in (
        "OPHELIA_OWNER_ID",
        "OPHELIA_PRIMARY_CHANNEL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_ALLOWED_USERS",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USER_IDS",
        "OPHELIA_TELEGRAM_ENABLED",
        "OPHELIA_DISCORD_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_telegram_owner_still_recognized_after_discord_added(monkeypatch, tmp_path):
    """THE REGRESSION: enabling Discord must NOT demote the Telegram owner."""
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111",
        DISCORD_BOT_TOKEN="dc_token",
        DISCORD_ALLOWED_USER_IDS="222",
    )
    assert s.is_owner_channel("telegram:111") is True
    assert s.is_owner_channel("discord:222") is True


def test_telegram_only_owner_recognized(monkeypatch, tmp_path):
    """Single-platform setup (no Discord) — original behavior preserved."""
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111",
    )
    assert s.is_owner_channel("telegram:111") is True
    assert s.discord_enabled is False
    assert "discord:111" not in s.owner_channels()


def test_approved_guest_not_promoted_to_owner(monkeypatch, tmp_path):
    """Guests are appended to the allowlist; they must NOT become owner.

    The owner is the FIRST entry (111); an approved guest (999) is appended
    after. is_owner_channel must return False for the guest even though
    they're in allowed_telegram_users().
    """
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111,999",
        DISCORD_BOT_TOKEN="dc_token",
        DISCORD_ALLOWED_USER_IDS="222,888",
    )
    assert s.is_owner_channel("telegram:111") is True
    assert s.is_owner_channel("telegram:999") is False
    assert 999 in (s.allowed_telegram_users() or set())  # guest is allowed
    assert s.is_owner_channel("discord:222") is True
    assert s.is_owner_channel("discord:888") is False


def test_explicit_owner_id_overrides_fallback(monkeypatch, tmp_path):
    """When OPHELIA_OWNER_ID is set, it's used verbatim — fallback doesn't run."""
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111,999",
        OPHELIA_OWNER_ID="telegram:111,discord:222",
    )
    assert s.owner_channels() == {"telegram:111", "discord:222"}
    assert s.is_owner_channel("telegram:111") is True
    assert s.is_owner_channel("telegram:999") is False


def test_explicit_owner_id_single_platform_demotes_other(monkeypatch, tmp_path):
    """Documents the user's likely original bug: setting OPHELIA_OWNER_ID to
    only one platform demotes the other. This is now a config error, not a
    code bug — but whoami diagnostics will surface it."""
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111",
        DISCORD_BOT_TOKEN="dc_token",
        DISCORD_ALLOWED_USER_IDS="222",
        OPHELIA_OWNER_ID="discord:222",
    )
    assert s.is_owner_channel("discord:222") is True
    assert s.is_owner_channel("telegram:111") is False  # demoted — config error


def test_ordered_telegram_allowlist_preserves_owner_first(monkeypatch, tmp_path):
    """The ordered accessor must keep the owner first (not set-iterate order)."""
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111,999,555,777",
    )
    ordered = s._allowed_telegram_users_ordered()
    assert ordered[0] == 111  # owner first, as configured
    assert set(ordered) == {111, 999, 555, 777}
    # set-based accessor is unordered but must contain the same ids
    assert s.allowed_telegram_users() == {111, 999, 555, 777}


def test_owner_channels_empty_when_nothing_configured(monkeypatch, tmp_path):
    """No tokens, no allowlists, no OPHELIA_OWNER_ID -> no owner recognized."""
    s = _settings_with(monkeypatch, tmp_path)
    assert s.owner_channels() == set()
    assert s.is_owner_channel("telegram:111") is False


def test_case_insensitive_owner_channel(monkeypatch, tmp_path):
    """Channel strings are lowercased on both sides so Telegram:111 matches."""
    s = _settings_with(
        monkeypatch,
        tmp_path,
        TELEGRAM_BOT_TOKEN="tg_token",
        TELEGRAM_ALLOWED_USER_IDS="111",
        OPHELIA_OWNER_ID="Telegram:111",
    )
    assert s.is_owner_channel("telegram:111") is True
    assert s.is_owner_channel("TELEGRAM:111") is True
