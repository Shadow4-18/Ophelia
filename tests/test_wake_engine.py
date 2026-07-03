"""Tests for WakeEngine availability + local STT fallback (Tier C #15).

Wake-word availability is environment-dependent (openWakeWord/Porcupine must
be importable + a mic capture binary must exist). These tests pin the
availability contract so a config change doesn't silently disable "Hey
Ophelia" and fall back to the slower STT-polling path without anyone
noticing.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_auto_engine_is_unavailable_by_default(settings):
    """With wake_engine='auto' (unset), the engine reports unavailable and
    the system is expected to fall back to STT polling."""
    from ophelia.media.wake_engine import WakeEngine

    assert (settings.wake_engine or "auto").lower() in ("", "auto")
    eng = WakeEngine(settings)
    assert eng.available() is False


async def test_uninstalled_engine_reports_unavailable(settings, monkeypatch):
    """Even with wake_engine='openwakeword', no mic binary → unavailable."""
    from ophelia.media import wake_engine as we_mod
    from ophelia.media.wake_engine import WakeEngine

    settings.__dict__["wake_engine"] = "openwakeword"
    eng = WakeEngine(settings)
    # Force no mic binary regardless of the host.
    monkeypatch.setattr(eng, "_record_bin", None)
    assert eng.available() is False


async def test_local_stt_not_configured_by_default(settings):
    """Local STT (whisper.cpp) should be off by default — falls back to xAI."""
    from ophelia.media.stt_local import local_stt_configured

    assert local_stt_configured(settings) is False


async def test_local_stt_configured_when_server_set(settings, monkeypatch):
    from ophelia.media.stt_local import local_stt_configured

    settings.__dict__["stt_provider"] = "local"
    settings.__dict__["whisper_server_url"] = "http://localhost:8080"
    assert local_stt_configured(settings) is True
