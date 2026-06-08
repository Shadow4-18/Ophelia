"""Chat gateway interface — Telegram, Discord, future sources."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatGateway(Protocol):
    """Inbound/outbound messaging surface for Ophelia."""

    platform: str

    def is_configured(self) -> bool: ...

    async def run(self) -> None:
        """Block until stopped (poll/connect loop)."""

    async def stop(self) -> None: ...

    async def send_proactive(self, text: str) -> None:
        """Consciousness / initiative outreach to allowed users."""
