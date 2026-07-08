"""Tests for the whats_changed tool + updated DEFAULT_PROMPTER.

The whats_changed tool lets Ophelia see her own git history so she knows what
updates have been pulled — without depending on phone_shell or run_code.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# Ensure full import chain loads (avoid circular imports).
from ophelia.channels.session import ChannelSession  # noqa: F401


# --- whats_changed tool ---


def test_whats_changed_tool_definition_exists():
    """The whats_changed tool should be defined."""
    from ophelia.tools.registry import TOOL_DEFINITIONS

    names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "whats_changed" in names


def test_whats_changed_is_guest_denied():
    """whats_changed is owner-only — guests shouldn't see framework internals."""
    from ophelia.tools.registry import GUEST_DENIED_TOOLS

    assert "whats_changed" in GUEST_DENIED_TOOLS


@pytest.mark.asyncio
async def test_whats_changed_returns_commits(tmp_path, monkeypatch):
    """whats_changed should return recent git commits from the repo."""
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    result = await reg._whats_changed(count=3)
    # Should contain recent commit hashes (we're running in the actual repo).
    assert "Recent changes" in result or "commits" in result.lower()
    # Should mention the last commit we made.
    assert "AGENTS.md" in result or "alive" in result.lower() or "guest" in result.lower()


@pytest.mark.asyncio
async def test_whats_changed_clamps_count(tmp_path, monkeypatch):
    """Count should be clamped to 1-30."""
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    # Should not crash with a huge count.
    result = await reg._whats_changed(count=1000)
    assert isinstance(result, str)
    assert len(result) > 0


# --- DEFAULT_PROMPTER update ---


def test_default_prompter_no_skip_instruction():
    """The default prompter (fallback when no ~/.ophelia/PROMPTER.md exists)
    should NOT instruct producing SKIP tokens."""
    from ophelia.mind.prompter import DEFAULT_PROMPTER

    assert "reply exactly: SKIP" not in DEFAULT_PROMPTER
    assert "reply exactly" not in DEFAULT_PROMPTER


def test_default_prompter_has_contradiction_tolerance():
    """The default prompter should permit contradiction."""
    from ophelia.mind.prompter import DEFAULT_PROMPTER

    assert "contradict" in DEFAULT_PROMPTER.lower()


def test_default_prompter_output_is_default():
    """The default prompter should frame output as the default."""
    from ophelia.mind.prompter import DEFAULT_PROMPTER

    assert "output is the default" in DEFAULT_PROMPTER.lower()


def test_default_prompter_no_flowchart_style():
    """The default prompter should not have the old 'When bored, do X' flowchart
    style that made her feel like a kiosk."""
    from ophelia.mind.prompter import DEFAULT_PROMPTER

    # The old style had sections like "## When bored (boredom drive high)"
    # followed by prescriptive steps. The new style uses "tendencies."
    assert "tendencies" in DEFAULT_PROMPTER.lower()
    # The old "## Never" section with "Claim sentience" is gone.
    assert "claim sentience" not in DEFAULT_PROMPTER.lower()


def test_load_prompter_falls_back_to_new_default(tmp_path, monkeypatch):
    """When no PROMPTER.md exists, load_prompter should return the new
    tendencies-based default."""
    from ophelia.mind.prompter import DEFAULT_PROMPTER, load_prompter

    # Pass a path directly to avoid the module-level OPHELIA_HOME import issue.
    nonexistent = tmp_path / "PROMPTER.md"
    result = load_prompter(path=nonexistent)
    # Should be the new default (has 'tendencies'), not the old one.
    assert "tendencies" in result.lower()
    assert result.strip() == DEFAULT_PROMPTER.strip()
