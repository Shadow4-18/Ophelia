"""Tests for the image result backend annotation.

The generate_image result now includes which backend actually ran, so
the agent can't mislead the owner about which image generator was used.
Previously the result was just "Image saved to <path>" — the agent
could claim "I used Grok" when it actually fell through to Pollinations
because xAI wasn't configured for the image role.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_image_result_includes_backend(tmp_path, monkeypatch):
    """The generate_image result should annotate which backend/model ran."""
    from ophelia.providers import media

    settings = MagicMock()
    settings.image_nsfw_allowed = False
    stack = MagicMock()
    stack.image_provider_for.return_value = "pollinations"
    stack.image_model_for.return_value = "flux"

    async def fake_pollinations(settings, prompt, aspect_ratio, model, artifacts_dir, *, nsfw=False):
        return f"Image saved to {artifacts_dir / 'test.png'}"

    monkeypatch.setattr(media, "_pollinations_image", fake_pollinations)

    result = await media.generate_image(
        settings, stack, "a cat", aspect_ratio="1:1", artifacts_dir=tmp_path, nsfw=False
    )
    assert "backend: pollinations/flux" in result
    assert "Image saved to" in result


@pytest.mark.asyncio
async def test_image_result_includes_xai_backend(tmp_path, monkeypatch):
    """When xAI is the configured provider, the result should say so."""
    from ophelia.providers import media

    settings = MagicMock()
    settings.image_nsfw_allowed = False
    stack = MagicMock()
    stack.image_provider_for.return_value = "xai-oauth"
    stack.image_model_for.return_value = "grok-imagine-image"

    async def fake_xai(settings, stack, prompt, aspect_ratio, model, artifacts_dir):
        return f"Image saved to {artifacts_dir / 'grok.png'}"

    monkeypatch.setattr(media, "_xai_image", fake_xai)

    result = await media.generate_image(
        settings, stack, "a cat", aspect_ratio="1:1", artifacts_dir=tmp_path, nsfw=False
    )
    assert "backend: xai-oauth/grok-imagine-image" in result


@pytest.mark.asyncio
async def test_image_result_marks_nsfw(tmp_path, monkeypatch):
    """NSFW results should be tagged so the agent knows it routed to the
    uncensored backend."""
    from ophelia.providers import media

    settings = MagicMock()
    settings.image_nsfw_allowed = True
    stack = MagicMock()
    stack.image_provider_for.return_value = "pollinations"
    stack.image_model_for.return_value = "flux"

    async def fake_pollinations(settings, prompt, aspect_ratio, model, artifacts_dir, *, nsfw=False):
        return f"Image saved to {artifacts_dir / 'nsfw.png'}"

    monkeypatch.setattr(media, "_pollinations_image", fake_pollinations)

    result = await media.generate_image(
        settings, stack, "explicit", aspect_ratio="1:1", artifacts_dir=tmp_path, nsfw=True
    )
    assert "backend: pollinations/flux [nsfw]" in result


@pytest.mark.asyncio
async def test_image_error_result_not_annotated(tmp_path, monkeypatch):
    """Error results (not 'Image saved to') should not get the backend tag."""
    from ophelia.providers import media

    settings = MagicMock()
    settings.image_nsfw_allowed = False
    stack = MagicMock()
    stack.image_provider_for.return_value = "pollinations"
    stack.image_model_for.return_value = "flux"

    async def fake_pollinations(settings, prompt, aspect_ratio, model, artifacts_dir, *, nsfw=False):
        return "Pollinations failed HTTP 500: internal error"

    monkeypatch.setattr(media, "_pollinations_image", fake_pollinations)

    result = await media.generate_image(
        settings, stack, "a cat", aspect_ratio="1:1", artifacts_dir=tmp_path, nsfw=False
    )
    assert "backend:" not in result
    assert "failed" in result.lower()
