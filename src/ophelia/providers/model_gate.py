"""Serialize LLM / media inference with per-role granularity.

Local providers (ollama, ollama*) share a single global lock because they load
one model at a time on the same GPU. Cloud providers (xai*, openai, compat)
get per-role locks, so chat, consciousness, vision, image, and video can
overlap — enabling Neuro-sama-style concurrent sub-minds.

The legacy single-lock behavior is preserved via `is_busy()`/`status()` which
report any active local or cloud session.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import structlog

log = structlog.get_logger()

LOCAL_PROVIDERS = {"ollama", "ollama-oauth"}


@dataclass
class ModelGate:
    """Per-role locks for cloud providers; one shared lock for local providers."""

    _local_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _role_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _active: dict[str, str] = field(default_factory=dict)  # role -> label
    _local_active: str | None = None
    _local_waiters: int = 0

    def _is_local(self, provider: str) -> bool:
        p = (provider or "").lower()
        return p in LOCAL_PROVIDERS or p.startswith("ollama")

    def _role_lock(self, role: str) -> asyncio.Lock:
        if role not in self._role_locks:
            self._role_locks[role] = asyncio.Lock()
        return self._role_locks[role]

    def is_busy(self) -> bool:
        """Any active inference anywhere (used by consciousness/listen to yield)."""
        if self._local_lock.locked():
            return True
        return any(lock.locked() for lock in self._role_locks.values())

    def active_label(self) -> str | None:
        if self._local_active:
            return self._local_active
        labels = list(self._active.values())
        return labels[0] if labels else None

    def status(self) -> dict[str, object]:
        active: dict[str, str] = {}
        if self._local_active:
            active["local"] = self._local_active
        active.update(self._active)
        return {
            "busy": self.is_busy(),
            "active": active,
            "local_waiters": self._local_waiters,
        }

    @asynccontextmanager
    async def session(
        self,
        role: str,
        model: str,
        provider: str,
    ) -> AsyncIterator[str]:
        label = f"{role}:{provider}/{model}"
        acquired_lock: asyncio.Lock | None = None
        is_local = self._is_local(provider)
        if is_local:
            self._local_waiters += 1
            try:
                await self._local_lock.acquire()
                acquired_lock = self._local_lock
            finally:
                self._local_waiters = max(0, self._local_waiters - 1)
            self._local_active = label
            log.debug("model_gate.acquire_local", label=label)
        else:
            lock = self._role_lock(role)
            await lock.acquire()
            acquired_lock = lock
            self._active[role] = label
            log.debug("model_gate.acquire_role", role=role, label=label)
        try:
            yield label
        finally:
            if is_local:
                self._local_active = None
            else:
                self._active.pop(role, None)
            if acquired_lock is not None:
                acquired_lock.release()
            log.debug("model_gate.release", label=label)


_gate: ModelGate | None = None


def get_model_gate() -> ModelGate:
    global _gate
    if _gate is None:
        _gate = ModelGate()
    return _gate
