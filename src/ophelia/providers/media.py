"""Image and video generation — routed per OPHELIA_PROVIDER_IMAGE / _VIDEO."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ophelia.config import Settings
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import ProviderStack, XAIBackend


async def generate_image(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    *,
    aspect_ratio: str = "1:1",
    artifacts_dir: Path,
) -> str:
    role = "image"
    provider = stack.name(role)
    model = stack.model(role)
    gate = get_model_gate()

    async with gate.session(role, model, provider):
        if provider in ("xai-oauth", "xai"):
            return await _xai_image(settings, stack, prompt, aspect_ratio, model)
        if provider == "openai":
            return await _openai_image(settings, stack, prompt, model, artifacts_dir)
        if provider == "ollama":
            return await _ollama_image(settings, prompt, model, artifacts_dir)
        raise RuntimeError(
            f"Image generation not configured for provider '{provider}'. "
            "Set OPHELIA_PROVIDER_IMAGE=xai-oauth, openai, or ollama + OLLAMA_IMAGE_MODEL."
        )


async def generate_video(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    *,
    duration_seconds: int = 6,
) -> str:
    role = "video"
    provider = stack.name(role)
    model = stack.model(role)
    gate = get_model_gate()

    async with gate.session(role, model, provider):
        if provider in ("xai-oauth", "xai"):
            return await _xai_video(settings, stack, prompt, duration_seconds, model)
        raise RuntimeError(
            f"Video generation requires xAI (provider={provider}). "
            "Set OPHELIA_PROVIDER_VIDEO=xai-oauth or xai."
        )


async def _xai_image(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    aspect_ratio: str,
    model: str,
) -> str:
    backend = stack.backend("image")
    assert isinstance(backend, XAIBackend)
    client = backend.async_client()
    resp = await client.images.generate(
        model=model,
        prompt=prompt,
        extra_body={"aspect_ratio": aspect_ratio},
    )
    url = resp.data[0].url if resp.data else None
    if not url:
        return "Image generation returned no URL."
    return f"Image generated ({model}): {url}"


async def _openai_image(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    backend = stack.backend("image")
    client = backend.async_client()
    resp = await client.images.generate(model=model, prompt=prompt, n=1)
    item = resp.data[0] if resp.data else None
    if not item:
        return "OpenAI image generation returned no data."
    if item.url:
        return f"Image generated ({model}): {item.url}"
    if item.b64_json:
        out = artifacts_dir / f"image_{abs(hash(prompt)) % 10**8}.png"
        out.write_bytes(base64.standard_b64decode(item.b64_json))
        return f"Image saved to {out}"
    return "Image generation returned no URL or bytes."


async def _ollama_image(
    settings: Settings,
    prompt: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    base = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(timeout=180.0) as http:
        r = await http.post(
            f"{base}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        data = r.json()

    # Ollama image models may return base64 in `image` or nested response
    b64 = data.get("image") or ""
    if not b64 and isinstance(data.get("response"), str) and data["response"].startswith("/9j"):
        b64 = data["response"]
    if not b64:
        return (
            f"Ollama model '{model}' did not return image bytes. "
            "Try: ollama pull flux (or another image-capable model)."
        )
    out = artifacts_dir / f"ollama_{abs(hash(prompt)) % 10**8}.png"
    out.write_bytes(base64.standard_b64decode(b64))
    return f"Image saved to {out} (model={model})"


async def _xai_video(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    duration_seconds: int,
    model: str,
) -> str:
    backend = stack.backend("video")
    assert isinstance(backend, XAIBackend)
    token = backend.bearer()
    if not token:
        return "No xAI credentials for video."

    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(
            f"{settings.xai_base_url.rstrip('/')}/videos/generations",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": model,
                "prompt": prompt,
                "duration": duration_seconds,
            },
        )
        r.raise_for_status()
        data = r.json()

    request_id = data.get("request_id") or data.get("id")
    return (
        f"Video job started ({model}, request_id={request_id}). "
        "Poll xAI video API or ask Ophelia to check status in a follow-up."
    )
