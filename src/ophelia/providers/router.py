from __future__ import annotations

from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from ophelia.config import Settings
from ophelia.providers.auth import resolve_xai_bearer
from ophelia.providers.oauth_refresh import ensure_fresh_token, load_oauth_state


class LLMBackend(Protocol):
    def async_client(self) -> AsyncOpenAI: ...
    def default_model(self) -> str: ...
    def label(self) -> str: ...


class XAIBackend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = None

    def _oauth_path(self) -> Path:
        if self.settings.hermes_auth_path.is_file():
            return self.settings.hermes_auth_path
        return self.settings.xai_oauth_token_path

    def bearer(self) -> str | None:
        return resolve_xai_bearer(
            api_key=self.settings.xai_api_key if self.settings.provider != "xai-oauth" else None,
            oauth_path=self.settings.xai_oauth_token_path,
            grok_cli_path=self.settings.grok_cli_auth_path,
            hermes_auth_path=self.settings.hermes_auth_path,
            prefer_oauth=self.settings.provider == "xai-oauth",
        )

    async def bearer_fresh(self) -> str:
        if self.settings.provider == "xai-oauth":
            path = self._oauth_path()
            if load_oauth_state(path):
                return await ensure_fresh_token(path)
        token = self.bearer()
        if not token:
            raise RuntimeError(self._auth_help())
        return token

    def async_client(self) -> AsyncOpenAI:
        token = self.bearer()
        if not token:
            raise RuntimeError(self._auth_help())
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=token,
                base_url=self.settings.xai_base_url,
            )
        return self._client

    async def async_client_fresh(self) -> AsyncOpenAI:
        token = await self.bearer_fresh()
        return AsyncOpenAI(api_key=token, base_url=self.settings.xai_base_url)

    def default_model(self) -> str:
        return self.settings.xai_model

    def label(self) -> str:
        return f"xAI ({self.settings.provider})"

    def _auth_help(self) -> str:
        return (
            "No xAI credentials. On old phone: tar ~/.hermes and scp to S21. "
            "Then: ophelia migrate hermes && ophelia auth import-hermes"
        )

    def reset(self) -> None:
        self._client = None


class OllamaBackend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = None

    def async_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key="ollama",
                base_url=self.settings.ollama_base_url,
            )
        return self._client

    def default_model(self) -> str:
        return self.settings.ollama_model

    def label(self) -> str:
        return f"Ollama @ {self.settings.ollama_base_url}"


def build_backend(settings: Settings) -> LLMBackend:
    if settings.provider == "ollama":
        return OllamaBackend(settings)
    return XAIBackend(settings)
