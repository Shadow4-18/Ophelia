"""Backward-compatible alias — use providers.router.build_backend instead."""

from __future__ import annotations

from ophelia.config import Settings
from ophelia.providers.router import XAIBackend, build_backend


class XAIClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._backend = build_backend(settings)

    def bearer(self) -> str | None:
        if isinstance(self._backend, XAIBackend):
            return self._backend.bearer()
        return None

    def client(self):
        return self._backend.async_client()

    def reset(self) -> None:
        if hasattr(self._backend, "reset"):
            self._backend.reset()
