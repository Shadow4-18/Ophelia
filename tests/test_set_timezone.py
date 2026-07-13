"""set_timezone tool persists OPHELIA_TIMEZONE and updates the live clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from ophelia.config import Settings
from ophelia.mind.initiative import InitiativeGovernor
from ophelia.mind.life_context import LifeContext
from ophelia.setup.env_io import read_env_key
from ophelia.timeutil import apply_timezone_setting, configured_timezone_label

# Import session first to break the tools↔channels circular import.
from ophelia.channels.session import ChannelSession  # noqa: F401
from ophelia.tools.registry import GUEST_DENIED_TOOLS, ToolRegistry


def test_set_timezone_is_owner_only():
    assert "set_timezone" in GUEST_DENIED_TOOLS


def test_apply_timezone_setting_persists_and_updates_governor(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    # Settings reads OPHELIA_HOME at import/construction via env_file path;
    # force timezone via env then construct.
    monkeypatch.setenv("OPHELIA_TIMEZONE", "UTC")
    settings = Settings()
    assert settings.timezone == "UTC"

    governor = InitiativeGovernor.from_settings(settings)
    assert governor.timezone == "UTC"

    msg = apply_timezone_setting(
        settings, "EST", persist=True, governor=governor
    )
    assert settings.timezone == "America/New_York"
    assert governor.timezone == "America/New_York"
    assert "America/New_York" in msg
    assert read_env_key("OPHELIA_TIMEZONE") == "America/New_York"


def test_apply_timezone_system(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    monkeypatch.setenv("OPHELIA_TIMEZONE", "UTC")
    settings = Settings()
    msg = apply_timezone_setting(settings, "system", persist=True)
    assert settings.timezone == "system"
    assert read_env_key("OPHELIA_TIMEZONE") == "system"
    assert "system" in msg.lower()


def test_apply_timezone_rejects_invalid(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_TIMEZONE", "UTC")
    settings = Settings()
    with pytest.raises(ValueError, match="Unknown"):
        apply_timezone_setting(settings, "Nope/Nowhere", persist=False)


@pytest.mark.asyncio
async def test_set_timezone_tool_dispatch(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    monkeypatch.setenv("OPHELIA_TIMEZONE", "UTC")
    monkeypatch.setenv("OPHELIA_ANDROID_ENABLED", "false")
    settings = Settings()
    artifacts = isolated_env / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    reg = ToolRegistry(settings, artifacts)
    governor = InitiativeGovernor.from_settings(settings)
    reg._governor_ref = governor
    reg.set_owner(True)

    result = await reg.dispatch(
        "set_timezone",
        '{"timezone": "America/Chicago", "reason": "owner asked"}',
    )
    assert "America/Chicago" in result
    assert settings.timezone == "America/Chicago"
    assert governor.timezone == "America/Chicago"
    assert read_env_key("OPHELIA_TIMEZONE") == "America/Chicago"


@pytest.mark.asyncio
async def test_set_timezone_blocked_for_guest(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_TIMEZONE", "UTC")
    monkeypatch.setenv("OPHELIA_ANDROID_ENABLED", "false")
    settings = Settings()
    artifacts = Path(isolated_env) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    reg = ToolRegistry(settings, artifacts)
    reg.set_owner(False)
    result = await reg.dispatch("set_timezone", '{"timezone": "EST"}')
    assert "owner-only" in result.lower() or "isn't available" in result.lower()
    assert settings.timezone == "UTC"


def test_life_context_shows_configured_label(settings, signals, monkeypatch):
    monkeypatch.setenv("OPHELIA_TIMEZONE", "America/New_York")
    # Rebuild settings so the env override sticks (fixture may have forced UTC).
    from ophelia.config import Settings

    settings = Settings()
    life = LifeContext(settings, signals)
    life._telegram_silent_hours = lambda: 1.0  # type: ignore[assignment]
    block = life.to_context_block()
    assert "America/New_York" in block
    assert "set_timezone" in block
    assert configured_timezone_label(settings.timezone) == "America/New_York"
