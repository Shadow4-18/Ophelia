"""WebSocket event bus for the workstation UI."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

log = structlog.get_logger()


class EventBus:
    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: Any) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: Any) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        async with self._lock:
            dead: list[Any] = []
            for ws in self._clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
