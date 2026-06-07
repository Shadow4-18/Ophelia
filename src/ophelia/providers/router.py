from __future__ import annotations

import asyncio
import asyncio
from pathlib import Path
from typing import Literal, Protocol

import httpx
from openai import AsyncOpenAI

from ophelia.config import Settings
from ophelia.providers.auth import resolve_xai_bearer
from ophelia.providers.oauth_refresh import ensure_fresh_token, load_oauth_state

ProviderRole = Literal["chat", "consciousness", "vision", "curator"]
ProviderName = Literal[
    "auto",
    "xai-oauth",
    "xai",
    "ollama",
    "openai",
    "compat",
]

ROLE_ENV: dict[ProviderRole, str] = {
    "chat": "OPHELIA_PROVIDER_CHAT",
    "consciousness": "OPHELIA_PROVIDER_CONSCIOUSNESS",
    "vision": "OPHELIA_PROVIDER_VISION",
    "curator": "OPHELIA_PROVIDER_CURATOR",
}

VISION_CAPABLE = frozenset({"xai-oauth", "xai", "openai", "compat", "ollama"})


class LLMBackend(Protocol):
    def async_client(self) -> AsyncOpenAI: ...
    def default_model(self) -> str: ...
    def label(self) -> str: ...
    def provider_name(self) -> str: ...


class XAIBackend:
    """xAI Grok — SuperGrok OAuth or API key."""

    def __init__(self, settings: Settings, *, prefer_oauth: bool) -> None:
        self.settings = settings
        self.prefer_oauth = prefer_oauth
        self._client: AsyncOpenAI | None = None

    def provider_name(self) -> str:
        return "xai-oauth" if self.prefer_oauth else "xai"

    def _oauth_path(self) -> Path:
        if self.settings.hermes_auth_path.is_file():
            return self.settings.hermes_auth_path
        return self.settings.xai_oauth_token_path

    def bearer(self) -> str | None:
        return resolve_xai_bearer(
            api_key=self.settings.xai_api_key if not self.prefer_oauth else None,
            oauth_path=self.settings.xai_oauth_token_path,
            grok_cli_path=self.settings.grok_cli_auth_path,
            hermes_auth_path=self.settings.hermes_auth_path,
            prefer_oauth=self.prefer_oauth,
        )

    async def bearer_fresh(self) -> str:
        if self.prefer_oauth:
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
        mode = "OAuth" if self.prefer_oauth else "API key"
        return f"xAI Grok ({mode})"

    def _auth_help(self) -> str:
        return (
            "No xAI credentials. Options:\n"
            "  ophelia auth import-hermes   (SuperGrok OAuth from Hermes)\n"
            "  ophelia auth import-grok     (Grok CLI login)\n"
            "  set XAI_API_KEY + OPHELIA_PROVIDER=xai"
        )

    def reset(self) -> None:
        self._client = None


class OpenAICompatibleBackend:
    """OpenAI API or any OpenAI-compatible server (OpenRouter, LM Studio, etc.)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        label: str,
        provider_name: str,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._label = label
        self._provider_name = provider_name
        self._client: AsyncOpenAI | None = None

    def provider_name(self) -> str:
        return self._provider_name

    def async_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def default_model(self) -> str:
        return self._model

    def label(self) -> str:
        return self._label


class OllamaBackend:
    def __init__(self, settings: Settings, *, vision: bool = False) -> None:
        self.settings = settings
        self.vision = vision
        self._client: AsyncOpenAI | None = None

    def provider_name(self) -> str:
        return "ollama"

    def async_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key="ollama",
                base_url=self.settings.ollama_base_url.rstrip("/"),
            )
        return self._client

    def default_model(self) -> str:
        if self.vision and self.settings.ollama_vision_model:
            return self.settings.ollama_vision_model
        return self.settings.ollama_model

    def label(self) -> str:
        suffix = " vision" if self.vision else ""
        return f"Ollama{suffix} @ {self.settings.ollama_base_url}"


def _ollama_reachable(settings: Settings, timeout: float = 2.0) -> bool:
    base = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
    try:
        r = httpx.get(f"{base}/api/tags", timeout=timeout)
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _xai_oauth_available(settings: Settings) -> bool:
    return bool(
        resolve_xai_bearer(
            api_key=None,
            oauth_path=settings.xai_oauth_token_path,
            grok_cli_path=settings.grok_cli_auth_path,
            hermes_auth_path=settings.hermes_auth_path,
            prefer_oauth=True,
        )
    )


def _auto_pick_provider(settings: Settings, role: ProviderRole) -> str:
    if role == "consciousness" and settings.auto_local_consciousness:
        if _ollama_reachable(settings):
            return "ollama"

    if role == "vision":
        if _xai_oauth_available(settings):
            return "xai-oauth"
        if settings.xai_api_key:
            return "xai"
        if settings.openai_api_key:
            return "openai"
        if settings.compat_api_key and settings.compat_base_url:
            return "compat"
        if _ollama_reachable(settings) and settings.ollama_vision_model:
            return "ollama"

    if _xai_oauth_available(settings):
        return "xai-oauth"
    if settings.xai_api_key:
        return "xai"
    if _ollama_reachable(settings):
        return "ollama"
    if settings.openai_api_key:
        return "openai"
    if settings.compat_api_key and settings.compat_base_url and settings.compat_model:
        return "compat"
    return "xai-oauth"


def resolve_provider_name(settings: Settings, role: ProviderRole = "chat") -> str:
    role_attr = {
        "chat": settings.provider_chat,
        "consciousness": settings.provider_consciousness,
        "vision": settings.provider_vision,
        "curator": settings.provider_curator,
    }[role]
    if role_attr and role_attr.strip().lower() != "auto":
        return role_attr.strip().lower()

    primary = (settings.provider or "auto").strip().lower()
    if primary != "auto":
        return primary
    return _auto_pick_provider(settings, role)


def build_backend_for_name(settings: Settings, name: str, *, role: ProviderRole = "chat") -> LLMBackend:
    n = name.strip().lower()
    if n in ("xai-oauth", "xai"):
        return XAIBackend(settings, prefer_oauth=(n == "xai-oauth"))
    if n == "ollama":
        return OllamaBackend(settings, vision=(role == "vision"))
    if n == "openai":
        key = settings.openai_api_key
        if not key:
            raise RuntimeError("OPENAI_API_KEY missing for OPHELIA_PROVIDER=openai")
        return OpenAICompatibleBackend(
            api_key=key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            label=f"OpenAI @ {settings.openai_base_url}",
            provider_name="openai",
        )
    if n == "compat":
        key = settings.compat_api_key
        base = settings.compat_base_url
        model = settings.compat_model
        if not key or not base or not model:
            raise RuntimeError(
                "Set OPHELIA_COMPAT_API_KEY, OPHELIA_COMPAT_BASE_URL, OPHELIA_COMPAT_MODEL"
            )
        return OpenAICompatibleBackend(
            api_key=key,
            base_url=base,
            model=model,
            label=f"OpenAI-compatible @ {base}",
            provider_name="compat",
        )
    raise RuntimeError(
        f"Unknown provider '{name}'. Use: xai-oauth, xai, ollama, openai, compat, auto"
    )


class ProviderStack:
    """Route chat, consciousness, vision, and curator to different backends."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._backends: dict[tuple[str, ProviderRole], LLMBackend] = {}

    def name(self, role: ProviderRole = "chat") -> str:
        return resolve_provider_name(self.settings, role)

    def backend(self, role: ProviderRole = "chat") -> LLMBackend:
        name = self.name(role)
        key = (name, role)
        if key not in self._backends:
            self._backends[key] = build_backend_for_name(self.settings, name, role=role)
        return self._backends[key]

    def model(self, role: ProviderRole = "chat") -> str:
        name = self.name(role)
        if name in ("xai-oauth", "xai") and role == "vision":
            return self.settings.vision_model or self.settings.xai_model
        if name == "openai" and role == "vision":
            return self.settings.openai_vision_model or self.settings.openai_model
        if name == "compat" and role == "vision" and self.settings.compat_vision_model:
            return self.settings.compat_vision_model
        return self.backend(role).default_model()

    def supports_vision(self, role: ProviderRole = "vision") -> bool:
        name = self.name(role)
        if name not in VISION_CAPABLE:
            return False
        if name == "ollama":
            return bool(self.settings.ollama_vision_model)
        return True

    def uses_xai_oauth(self) -> bool:
        for role in ("chat", "consciousness", "vision", "curator"):
            if self.name(role) == "xai-oauth":
                return True
        return False

    def xai_backend(self) -> XAIBackend | None:
        for role in ("chat", "vision", "consciousness", "curator"):
            b = self.backend(role)
            if isinstance(b, XAIBackend):
                return b
        return None

    async def check(self, role: ProviderRole = "chat") -> tuple[bool, str]:
        name = self.name(role)
        try:
            backend = self.backend(role)
            if isinstance(backend, XAIBackend):
                if name == "xai-oauth":
                    if not backend.bearer():
                        return False, "missing OAuth — ophelia auth import-grok or import-hermes"
                    return True, "OK (OAuth token present)"
                if not backend.bearer():
                    return False, "missing xAI API key"
                return True, "OK (API key present)"
            if isinstance(backend, OllamaBackend):
                if not _ollama_reachable(self.settings):
                    return False, f"Ollama not reachable at {self.settings.ollama_base_url}"
                return True, f"OK ({backend.default_model()})"
            client = backend.async_client()
            await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model(role),
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                ),
                timeout=8.0,
            )
            return True, "OK"
        except TimeoutError:
            return False, "timeout (8s)"
        except Exception as e:
            return False, str(e)[:120]

    def describe(self) -> str:
        lines = ["Provider routing:"]
        for role in ("chat", "consciousness", "vision", "curator"):
            name = self.name(role)
            model = self.model(role)
            lines.append(f"  {role:14} {name:12} -> {model}")
        return "\n".join(lines)


def build_provider_stack(settings: Settings) -> ProviderStack:
    return ProviderStack(settings)


def build_backend(settings: Settings) -> LLMBackend:
    """Primary chat backend (backward compatible)."""
    return build_provider_stack(settings).backend("chat")
