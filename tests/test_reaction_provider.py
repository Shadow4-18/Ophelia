"""Tests for reaction provider role defaults (S21 Neuro-lite)."""

from __future__ import annotations

from ophelia.providers.router import ProviderStack, resolve_ollama_model


def test_ollama_reaction_defaults_to_1b_not_chat_model(settings):
    settings.ollama_model = "llama3.2:3b"
    settings.ollama_consciousness_model = None
    settings.ollama_reaction_model = None
    assert resolve_ollama_model(settings, "reaction") == "llama3.2:1b"
    assert resolve_ollama_model(settings, "chat") == "llama3.2:3b"


def test_ollama_reaction_prefers_explicit_then_consciousness(settings):
    settings.ollama_model = "llama3.2:3b"
    settings.ollama_consciousness_model = "llama3.2:1b"
    settings.ollama_reaction_model = None
    assert resolve_ollama_model(settings, "reaction") == "llama3.2:1b"

    settings.ollama_reaction_model = "gemma3:1b"
    assert resolve_ollama_model(settings, "reaction") == "gemma3:1b"


def test_provider_stack_lists_reaction_role(settings):
    settings.provider = "ollama"
    settings.provider_reaction = "ollama"
    settings.ollama_reaction_model = "llama3.2:1b"
    stack = ProviderStack(settings)
    assert stack.name("reaction") == "ollama"
    assert stack.model("reaction") == "llama3.2:1b"
    desc = stack.describe()
    assert "reaction" in desc
