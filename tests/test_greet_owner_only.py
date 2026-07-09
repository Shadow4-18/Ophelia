"""Tests for the greet-on-start behavior.

The greet is an owner-only 'hi, I'm up' — it must NOT be broadcast to
guests on the allowlist. Guests don't need a ping every time she
restarts. These tests verify the greet sends only to owner channels
and falls back to broadcast only when no owner channel is reachable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_greet_sends_only_to_owner_channels():
    """_greet_on_start should send to each owner channel, not broadcast
    to the full allowlist (which includes guests)."""
    from ophelia.core import orchestrator as orch_mod

    settings = MagicMock()
    settings.primary_user_channel.return_value = "telegram:111"
    settings.owner_channels.return_value = {"telegram:111", "discord:222"}

    orchestrator = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    orchestrator.settings = settings
    orchestrator.agent = MagicMock()
    orchestrator.agent.run_turn = AsyncMock(return_value="morning.")
    orchestrator.hub = MagicMock()
    orchestrator.hub.send_to_user = AsyncMock(return_value=True)
    orchestrator.hub.broadcast_proactive = AsyncMock()

    orig_sleep = orch_mod.asyncio.sleep
    orch_mod.asyncio.sleep = AsyncMock()
    try:
        await orchestrator._greet_on_start()
    finally:
        orch_mod.asyncio.sleep = orig_sleep

    # send_to_user should have been called for both owner channels.
    assert orchestrator.hub.send_to_user.await_count == 2
    called_platforms = {
        call.args[0] for call in orchestrator.hub.send_to_user.await_args_list
    }
    assert called_platforms == {"telegram", "discord"}
    # broadcast_proactive should NOT have been called (owners were reachable).
    orchestrator.hub.broadcast_proactive.assert_not_awaited()


@pytest.mark.asyncio
async def test_greet_falls_back_to_broadcast_when_no_owner_reachable():
    """If no owner channel is reachable (send_to_user returns False for all),
    fall back to broadcast so the greet isn't silently lost."""
    from ophelia.core import orchestrator as orch_mod

    settings = MagicMock()
    settings.primary_user_channel.return_value = "telegram:111"
    settings.owner_channels.return_value = {"telegram:111"}

    orchestrator = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    orchestrator.settings = settings
    orchestrator.agent = MagicMock()
    orchestrator.agent.run_turn = AsyncMock(return_value="morning.")
    orchestrator.hub = MagicMock()
    orchestrator.hub.send_to_user = AsyncMock(return_value=False)  # all fail
    orchestrator.hub.broadcast_proactive = AsyncMock()

    orig_sleep = orch_mod.asyncio.sleep
    orch_mod.asyncio.sleep = AsyncMock()
    try:
        await orchestrator._greet_on_start()
    finally:
        orch_mod.asyncio.sleep = orig_sleep

    # send_to_user was tried...
    orchestrator.hub.send_to_user.assert_awaited()
    # ...but since it failed, broadcast_proactive is the fallback.
    orchestrator.hub.broadcast_proactive.assert_awaited_once_with("morning.")


@pytest.mark.asyncio
async def test_greet_does_not_send_to_guests():
    """The greet must never reach a guest id. With owner_channels =
    {telegram:111} and a guest on telegram:222, only 111 should receive it."""
    from ophelia.core import orchestrator as orch_mod

    settings = MagicMock()
    settings.primary_user_channel.return_value = "telegram:111"
    settings.owner_channels.return_value = {"telegram:111"}  # owner only

    orchestrator = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    orchestrator.settings = settings
    orchestrator.agent = MagicMock()
    orchestrator.agent.run_turn = AsyncMock(return_value="hi")
    orchestrator.hub = MagicMock()
    orchestrator.hub.send_to_user = AsyncMock(return_value=True)
    orchestrator.hub.broadcast_proactive = AsyncMock()

    orig_sleep = orch_mod.asyncio.sleep
    orch_mod.asyncio.sleep = AsyncMock()
    try:
        await orchestrator._greet_on_start()
    finally:
        orch_mod.asyncio.sleep = orig_sleep

    # Only one send_to_user call, to the owner's platform — never to a guest.
    assert orchestrator.hub.send_to_user.await_count == 1
    call = orchestrator.hub.send_to_user.await_args
    platform, uid, text = call.args
    assert platform == "telegram"
    assert uid == 111
    assert text == "hi"
