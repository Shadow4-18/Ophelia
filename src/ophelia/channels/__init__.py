"""Messaging channel adapters.

Keep this package init lightweight — importing a submodule such as
``ophelia.channels.media_reply`` must not pull Telegram/Discord (and thus
``AgentLoop``) or we hit a circular import via ``tools.registry``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ChannelHub", "TelegramGateway"]


def __getattr__(name: str) -> Any:
    if name == "ChannelHub":
        from ophelia.channels.hub import ChannelHub

        return ChannelHub
    if name == "TelegramGateway":
        from ophelia.channels.telegram_bot import TelegramGateway

        return TelegramGateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
