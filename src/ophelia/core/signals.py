"""Shared state bus (Neuro-sama Signals pattern)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class Signals:
    user_talking: bool = False
    agent_thinking: bool = False
    last_user_message_at: float = 0.0
    last_agent_message_at: float = 0.0
    autonomy_paused: bool = False
    listen_enabled: bool = False
    inner_mirror: bool = False
    terminate: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def set_user_talking(self, value: bool) -> None:
        async with self._lock:
            self.user_talking = value

    async def set_agent_thinking(self, value: bool) -> None:
        async with self._lock:
            self.agent_thinking = value
