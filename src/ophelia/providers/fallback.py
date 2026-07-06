"""Reusable fallback wrapper for LLM completion calls.

Any caller that builds its own chat.completions.create() call (the curator,
consciousness loop, etc.) can wrap it in `call_with_fallback` to get the same
transient-error retry behavior that AgentLoop._complete uses, without
duplicating the fallback logic.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog

from ophelia.config import Settings
from ophelia.providers.errors import api_error_detail
from ophelia.providers.model_gate import ModelGate, get_model_gate
from ophelia.providers.router import (
    ProviderStack,
    ProviderRole,
    _provider_configured_for_role,
    _provider_default_model_for_role,
    build_backend_for_name,
)

log = structlog.get_logger()


def extra_body_for(settings: Settings, provider: str) -> dict[str, Any] | None:
    """Provider-specific extra_body for chat.completions.create.

    DeepSeek V4 enables thinking mode by default. Thinking mode returns a
    long reasoning_content and tends to reason past tool calls — the model
    talks itself out of calling web_search and just answers from weights,
    which is why "web search stopped working" when routed through DeepSeek.
    Disable it unless the user explicitly opted in via
    OPHELIA_DEEPSEEK_THINKING=true.

    Callers should pass the result as `extra_body=` to
    client.chat.completions.create(...). Returns None when no override is
    needed so the call site can pass None cleanly.
    """
    if provider == "deepseek" and not settings.deepseek_thinking:
        return {"thinking": {"type": "disabled"}}
    return None


def _is_transient_error(exc: BaseException) -> bool:
    detail = api_error_detail(exc).lower()
    if any(k in detail for k in ("429", "rate limit", "rate_limit")):
        return True
    if any(k in detail for k in ("500", "502", "503", "504", "server error", "overloaded")):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if any(k in type(exc).__name__.lower() for k in ("connect", "timeout", "network")):
        return True
    cause = getattr(exc, "__cause__", None) or exc
    if any(k in type(cause).__name__.lower() for k in ("connect", "timeout", "network")):
        return True
    return False


async def _client_for_provider(settings: Settings, provider: str, role: ProviderRole):
    """Build a fresh OpenAI client bound to a specific provider."""
    from ophelia.providers.router import XAIBackend

    backend = build_backend_for_name(settings, provider, role=role)
    if isinstance(backend, XAIBackend):
        return await backend.async_client_fresh()
    return backend.async_client()


async def call_with_fallback(
    settings: Settings,
    stack: ProviderStack,
    *,
    role: ProviderRole,
    primary_provider: str,
    primary_model: str,
    primary_client,
    make_call: Callable[[Any, str, str], Awaitable[Any]],
    gate: ModelGate | None = None,
    log_tag: str = "llm.fallback",
) -> Any:
    """Try the primary provider, then each fallback on transient failure.

    `make_call(client, model, provider)` is called with the resolved client,
    model, and provider name and must return the awaited completion result.
    This lets each caller shape its own request (tools, max_tokens, messages,
    provider-specific extra_body) while sharing fallback behavior.

    Raises only if every provider fails transiently, or immediately if the
    primary failure is non-transient (e.g. 400).
    """
    gate = gate or get_model_gate()

    # Primary
    try:
        async with gate.session(role, primary_model, primary_provider):
            return await make_call(primary_client, primary_model, primary_provider)
    except Exception as e:
        if not _is_transient_error(e):
            raise
        log.warning(
            f"{log_tag}.primary_failed",
            role=role,
            provider=primary_provider,
            model=primary_model,
            error=api_error_detail(e),
        )

    # Fallbacks
    primary = primary_provider
    for prov in settings.fallback_provider_list():
        if prov == primary or prov == "auto":
            continue
        if not _provider_configured_for_role(settings, prov, role):
            continue
        fb_model = (
            settings.fallback_model
            or _provider_default_model_for_role(settings, prov, role)
            or primary_model
        )
        try:
            fb_client = await _client_for_provider(settings, prov, role)
        except Exception as e:
            log.warning(f"{log_tag}.client_failed", provider=prov, error=str(e))
            continue
        try:
            async with gate.session(role, fb_model, prov):
                result = await make_call(fb_client, fb_model, prov)
            log.info(
                f"{log_tag}.succeeded",
                role=role,
                provider=prov,
                model=fb_model,
            )
            return result
        except Exception as e:
            if not _is_transient_error(e):
                raise
            log.warning(
                f"{log_tag}.failed",
                role=role,
                provider=prov,
                model=fb_model,
                error=api_error_detail(e),
            )
            continue

    raise RuntimeError(
        f"All providers failed for {role} (primary {primary_provider}). "
        "Last error was transient — retry later."
    )
