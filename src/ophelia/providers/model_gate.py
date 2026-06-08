"""Serialize all LLM / media inference — one model loaded at a time."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog

log = structlog.get_logger()


@dataclass
class ModelGate:
    """Process-wide lock so Ollama (and hybrid setups) never run two models at once."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _active: str | None = None
    _waiters: int = 0

    def is_busy(self) -> bool:
        return self._lock.locked()

    def active_label(self) -> str | None:
        return self._active

    def status(self) -> dict[str, str | bool | int]:
        return {
            "busy": self.is_busy(),
            "active": self._active or "",
            "waiters": self._waiters,
        }

    @asynccontextmanager
    async def session(
        self,
        role: str,
        model: str,
        provider: str,
    ) -> AsyncIterator[str]:
        label = f"{role}:{provider}/{model}"
        self._waiters += 1
        try:
            await self._lock.acquire()
            self._waiters -= 1
            self._active = label
            log.debug("model_gate.acquire", label=label)
            try:
                yield label
            finally:
                self._active = None
                self._lock.release()
                log.debug("model_gate.release", label=label)
        except Exception:
            self._waiters = max(0, self._waiters - 1)
            raise


_gate: ModelGate | None = None


def get_model_gate() -> ModelGate:
    global _gate
    if _gate is None:
        _gate = ModelGate()
    return _gate
