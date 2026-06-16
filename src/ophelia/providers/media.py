"""Image and video generation — routed per OPHELIA_PROVIDER_IMAGE / _VIDEO."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Iterable
from pathlib import Path

import httpx
import structlog

from ophelia.config import Settings
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import ProviderStack, XAIBackend

log = structlog.get_logger()


def _deep_find_first_str(node: object, keys: Iterable[str]) -> str:
    wanted = {k.lower() for k in keys}
    stack: list[object] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(k, str) and k.lower() in wanted and isinstance(v, str):
                    value = v.strip()
                    if value:
                        return value
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


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
    artifacts_dir: Path | None = None,
) -> str:
    role = "video"
    provider = stack.name(role)
    model = stack.model(role)
    gate = get_model_gate()

    async with gate.session(role, model, provider):
        if provider in ("xai-oauth", "xai"):
            return await _xai_video(
                settings,
                stack,
                prompt,
                duration_seconds,
                model,
                artifacts_dir=artifacts_dir,
            )
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
    *,
    artifacts_dir: Path | None = None,
) -> str:
    backend = stack.backend("video")
    assert isinstance(backend, XAIBackend)
    try:
        token = await backend.bearer_fresh()
    except Exception:
        token = backend.bearer()
    if not token:
        return "No xAI credentials for video."

    base = settings.xai_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(
            f"{base}/videos/generations",
            headers=headers,
            json={
                "model": model,
                "prompt": prompt,
                "duration": duration_seconds,
            },
        )
        if r.status_code >= 400:
            return f"Video start failed HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()

    request_id = _deep_find_first_str(data, ("request_id", "id", "job_id", "generation_id"))
    if not request_id:
        return f"Video API returned no request_id: {data}"

    deadline = time.monotonic() + 600.0
    async with httpx.AsyncClient(timeout=120.0) as http:
        while time.monotonic() < deadline:
            poll = await http.get(f"{base}/videos/{request_id}", headers=headers)
            if poll.status_code == 404:
                # Some deployments expose /videos/generations/{id} for status.
                poll = await http.get(f"{base}/videos/generations/{request_id}", headers=headers)
            if poll.status_code >= 400:
                return f"Video poll failed HTTP {poll.status_code}: {poll.text[:300]}"
            result = poll.json()
            status = str(
                result.get("status")
                or result.get("state")
                or result.get("phase")
                or ""
            ).lower()
            if status in {"done", "completed", "succeeded", "success"}:
                url = _deep_find_first_str(
                    result,
                    (
                        "url",
                        "video_url",
                        "download_url",
                        "signed_url",
                        "result_url",
                    ),
                )
                if not url:
                    return f"Video done but no URL in response: {result}"
                out_dir = artifacts_dir or (settings.data_dir / "artifacts")
                out_dir.mkdir(parents=True, exist_ok=True)
                out = out_dir / f"video_{request_id[:16]}.mp4"
                dl = await http.get(url)
                if dl.status_code >= 400:
                    # Some URLs still require bearer auth.
                    dl = await http.get(url, headers=headers)
                dl.raise_for_status()
                out.write_bytes(dl.content)
                log.info("video.saved", path=str(out), request_id=request_id)
                return (
                    f"Video generated ({model}, {duration_seconds}s). "
                    f"Video saved to {out}"
                )
            if status in {"failed", "expired"}:
                err = result.get("error") or result
                return f"Video generation {status}: {err}"
            await asyncio.sleep(5.0)

    return (
        f"Video still pending after 10m (request_id={request_id}). "
        "Try again or ask to check status later."
    )
