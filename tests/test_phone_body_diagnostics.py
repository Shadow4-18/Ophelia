"""Tests for phone-body diagnostics.

When the agent claims 'I have no termux api access' / 'shizuku isn't working',
the cause is almost always one of:
  - android_enabled is false (tools not registered at all), or
  - the bridge script (rish / phone_control.sh) is missing on Termux, so
    `mode` resolves to 'termux_only' and every phone_* tool returns an error.

Previously every phone_* tool returned the same 'Phone body disabled' message
in both cases, so the agent (and user) couldn't tell a config toggle from a
missing bridge script. These tests pin the distinction: 'disabled' vs
'bridge not wired', plus the diagnostic status_line that names what's missing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — triggers full import chain
from ophelia.android.shizuku import AndroidBody


def _body_with_no_bridge(tmp_path: Path) -> AndroidBody:
    """An AndroidBody on Termux with neither rish nor phone_control.sh present."""
    body = AndroidBody(
        phone_control=tmp_path / "phone_control.sh",  # doesn't exist
        rish_path=None,
        adb_device=None,
    )
    body._adb_path = None  # no adb either
    return body


def test_status_line_termux_only_names_what_is_missing(tmp_path):
    body = _body_with_no_bridge(tmp_path)
    with patch("ophelia.android.shizuku.is_termux", return_value=True):
        line = body.status_line()
    assert "termux_only" in line
    assert "rish" in line  # names the missing bridge
    assert "phone_control.sh" in line
    assert "termux-shizuku-setup.sh" in line  # names the fix script


def test_status_line_adb_mode_unaffected(tmp_path):
    """ADB mode must keep its original concise status (regression guard)."""
    body = AndroidBody(phone_control=tmp_path / "phone_control.sh", adb_device="192.168.1.5:5555")
    body._adb_path = "/usr/bin/adb"
    with patch("ophelia.android.shizuku.is_termux", return_value=False):
        line = body.status_line()
    assert "adb" in line
    assert "192.168.1.5:5555" in line
    assert "termux_only" not in line


def test_mode_termux_only_when_no_bridge_on_termux(tmp_path):
    body = _body_with_no_bridge(tmp_path)
    with patch("ophelia.android.shizuku.is_termux", return_value=True):
        assert body.mode == "termux_only"


def test_mode_phone_control_when_script_present_on_termux(tmp_path):
    """If phone_control.sh exists, mode should be 'phone_control' not 'termux_only'."""
    script = tmp_path / "phone_control.sh"
    script.write_text("#!/bin/sh\n")
    body = AndroidBody(phone_control=script, rish_path=None, adb_device=None)
    body._adb_path = None
    with patch("ophelia.android.shizuku.is_termux", return_value=True):
        assert body.mode == "phone_control"


# ── ToolRegistry._phone_unavailable_reason ─────────────────────────────────


def _registry(android):
    """Build a ToolRegistry without running __init__ (avoids circular import
    triggered by importing the full channels stack)."""
    from ophelia.tools.registry import ToolRegistry

    settings = MagicMock()
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.android = android
    return reg


def test_phone_unavailable_reason_disabled():
    """When android is None, the reason must say 'disabled' (config choice)."""
    reg = _registry(android=None)
    reason = reg._phone_unavailable_reason()
    assert reason is not None
    assert "disabled" in reason
    assert "OPHELIA_ANDROID_ENABLED" in reason


def test_phone_unavailable_reason_bridge_not_wired(tmp_path):
    """When android exists but mode is termux_only, reason must say 'not wired'."""
    body = _body_with_no_bridge(tmp_path)
    reg = _registry(android=body)
    with patch("ophelia.android.shizuku.is_termux", return_value=True):
        reason = reg._phone_unavailable_reason()
    assert reason is not None
    assert "not wired" in reason
    assert "termux-shizuku-setup.sh" in reason
    # CRITICAL: must NOT say 'disabled' — that would make the agent think the
    # whole feature is off rather than a fixable bridge gap.
    assert "disabled" not in reason


def test_phone_unavailable_reason_none_when_bridge_ok(tmp_path):
    """When phone_control.sh is present, the body is usable -> reason is None."""
    script = tmp_path / "phone_control.sh"
    script.write_text("#!/bin/sh\n")
    body = AndroidBody(phone_control=script, rish_path=None, adb_device=None)
    body._adb_path = None
    reg = _registry(android=body)
    with patch("ophelia.android.shizuku.is_termux", return_value=True):
        assert reg._phone_unavailable_reason() is None


@pytest.mark.asyncio
async def test_phone_tap_returns_bridge_message_not_disabled(tmp_path):
    """The tool result must distinguish 'bridge not wired' from 'disabled'.

    This is the message the agent sees in its tool result — if it says
    'disabled' the agent over-generalizes to 'I have no phone access at all'
    and tells the user the feature is gone. It must say 'not wired' instead.
    """
    body = _body_with_no_bridge(tmp_path)
    reg = _registry(android=body)
    with patch("ophelia.android.shizuku.is_termux", return_value=True):
        result = await reg._phone_tap(100, 200)
    assert "not wired" in result
    assert "disabled" not in result
