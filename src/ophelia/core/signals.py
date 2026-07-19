"""Shared state bus (Neuro-sama Signals pattern)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Signals:
    user_talking: bool = False
    agent_thinking: bool = False
    last_user_message_at: float = 0.0
    last_agent_message_at: float = 0.0
    # Last time she took an autonomous action (outreach/act/explore). Used by
    # soft satiation — raises the bar briefly after she acts, then decays.
    last_action_at: float = 0.0
    last_telegram_user_id: int | None = None
    autonomy_paused: bool = False
    listen_enabled: bool = False
    inner_mirror: bool = False
    terminate: bool = False
    # Event-driven consciousness wakes (chat ended, presence, drive spike).
    wake_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    wake_reason: str = ""
    wake_urgent: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def set_user_talking(self, value: bool) -> None:
        async with self._lock:
            self.user_talking = value

    async def set_agent_thinking(self, value: bool) -> None:
        async with self._lock:
            self.agent_thinking = value

    async def mark_action(self) -> None:
        async with self._lock:
            self.last_action_at = time.time()

    def request_wake(self, reason: str, *, urgent: bool = False) -> None:
        """Ask the consciousness loop to stir soon."""
        reason = (reason or "wake").strip() or "wake"
        if not self.wake_reason or urgent:
            self.wake_reason = reason
        if urgent:
            self.wake_urgent = True
        self.wake_event.set()

    def consume_wake(self) -> tuple[str, bool]:
        """Return (reason, urgent) and clear the wake latch."""
        reason = self.wake_reason or "wake"
        urgent = self.wake_urgent
        self.wake_reason = ""
        self.wake_urgent = False
        try:
            self.wake_event.clear()
        except Exception:
            pass
        return reason, urgent
