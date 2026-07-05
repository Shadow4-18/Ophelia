"""Tests for Discord log channel naming helpers."""

from ophelia.channels.discord_log_channels import _channel_name, _slug


def test_slug_sanitizes_names():
    assert _slug("Alice Smith!") == "alice-smith"
    assert _slug("___") == "unknown"


def test_channel_name_includes_prefix_and_id():
    name = _channel_name("tg", "Bob", "12345")
    assert name.startswith("tg-bob-12345")
    assert len(name) <= 100
