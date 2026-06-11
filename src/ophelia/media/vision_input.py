"""Describe arbitrary image files via the vision provider stack."""

from __future__ import annotations

import base64
from pathlib import Path

import structlog

from ophelia.config import Settings
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import XAIBackend, build_provider_stack

log = structlog.get_logger()

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/png"


async def describe_image_file(
    settings: Settings,
    path: Path,
    *,
    question: str = "Describe this image in detail. What is the user showing you?",
    stack=None,
) -> str:
    path = path.expanduser().resolve()
    if not path.is_file():
        return f"Image file not found: {path}"
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return f"Unsupported image type: {path.suffix}"

    stack = stack or build_provider_stack(settings)
    if not stack.supports_vision():
        return (
            f"Photo saved to {path} but no vision provider "
            f"(set OPHELIA_PROVIDER_VISION=xai-oauth or openai)."
        )

    backend = stack.backend("vision")
    model = stack.model("vision")
    if isinstance(backend, XAIBackend):
        client = await backend.async_client_fresh()
    else:
        client = backend.async_client()

    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    mime = _mime_for(path)
    content: list[dict] = [
        {"type": "text", "text": question},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"},
        },
    ]

    try:
        gate = get_model_gate()
        async with gate.session("vision", model, stack.name("vision")):
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=1200,
            )
        text = (resp.choices[0].message.content or "").strip()
        log.info("vision.image_file_ok", path=str(path), chars=len(text))
        return text or "(vision returned empty)"
    except Exception as e:
        log.warning("vision.image_file_failed", error=str(e))
        return f"Vision failed for {path.name}: {e}"
