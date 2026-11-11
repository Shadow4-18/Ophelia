"""Tests for DeepSeek V4 thinking-mode handling.

DeepSeek V4 enables "thinking mode" by default. Thinking mode returns a long
reasoning_content and tends to reason past tool calls — the model decides
during reasoning that it "can't search the web" and never emits a web_search
tool call, which is why users saw web_search silently stop working when
routed through DeepSeek. These tests pin the contract that:

  * `extra_body_for` disables thinking by default for the deepseek provider,
  * opting in via OPHELIA_DEEPSEEK_THINKING=true leaves it untouched,
  * non-deepseek providers are unaffected (return None so the call site
    passes None cleanly to the OpenAI client).
"""

from __future__ import annotations

import pytest

from ophelia.providers.fallback import extra_body_for


def _settings(monkeypatch, *, thinking: bool | None = None) -> "Settings":  # type: ignore[name-defined]
    from ophelia.config import Settings

    if thinking is not None:
        monkeypatch.setenv("OPHELIA_DEEPSEEK_THINKING", "true" if thinking else "false")
    return Settings()


def test_extra_body_disables_thinking_for_deepseek_by_default(settings):
    """The whole point: deepseek + default config -> thinking disabled."""
    body = extra_body_for(settings, "deepseek")
    assert body == {"thinking": {"type": "disabled"}}


def test_extra_body_respects_opt_in(monkeypatch):
    """OPHELIA_DEEPSEEK_THINKING=true must NOT inject the disable override."""
    s = _settings(monkeypatch, thinking=True)
    assert s.deepseek_thinking is True
    assert extra_body_for(s, "deepseek") is None


def test_extra_body_explicit_off_matches_default(monkeypatch):
    """OPHELIA_DEEPSEEK_THINKING=false is the same as the default."""
    s = _settings(monkeypatch, thinking=False)
    assert s.deepseek_thinking is False
    assert extra_body_for(s, "deepseek") == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("provider", ["xai", "openai", "ollama", "compat", "auto"])
def test_extra_body_none_for_non_deepseek_providers(settings, provider):
    """No other provider gets a thinking override — call sites pass None."""
    assert extra_body_for(settings, provider) is None


def test_deepseek_thinking_defaults_to_false(settings):
    """Pin the default — flipping it to True would silently break web_search."""
    assert settings.deepseek_thinking is False


def test_make_call_signature_accepts_provider():
    """call_with_fallback now passes (client, model, provider) to make_call.

    Regression guard: every make_call callback must accept a third `provider`
    positional arg so extra_body_for(provider) is computed correctly across
    the primary + each fallback, not just the primary.
    """
    import inspect

    from ophelia.providers.fallback import call_with_fallback

    sig = inspect.signature(call_with_fallback)
    make_call_param = sig.parameters["make_call"]
    # The annotation is Callable[[Any, str, str], Awaitable[Any]] — three args.
    ann = str(make_call_param.annotation)
    assert "Any, str, str" in ann, ann


@pytest.mark.asyncio
async def test_shared_helper_turns_thinking_off(monkeypatch):
    """reflect (now on complete_text) and the health ping passed the
    provider_name *method* to extra_body_for, so the 'deepseek' check never
    matched and thinking mode stayed on."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from ophelia.providers import fallback

    sent = {}

    async def fake_cwf(settings, stack, *, make_call, primary_client, primary_model, **kw):
        client = MagicMock()

        async def create(**k):
            sent.update(k)
            msg = SimpleNamespace(content='{"reflection": "x", "lessons": []}')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        client.chat.completions.create = create
        return await make_call(client, primary_model, "deepseek")

    monkeypatch.setattr(fallback, "call_with_fallback", fake_cwf)
    stack = MagicMock()
    stack.name.return_value = "deepseek"
    stack.model.return_value = "deepseek-chat"
    out = await fallback.complete_text(SimpleNamespace(deepseek_thinking=False), stack,
                                       role="chat", messages=[], log_tag="t")
    assert out.startswith("{") and sent["extra_body"] == {"thinking": {"type": "disabled"}}
