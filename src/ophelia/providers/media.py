"""Image and video generation — routed per OPHELIA_PROVIDER_IMAGE / _VIDEO."""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

import httpx
import structlog

from ophelia.config import Settings
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import ProviderStack, XAIBackend

log = structlog.get_logger()


# Censored backends — never send explicit prompts to these (they refuse and may
# flag the account). NSFW requests are auto-routed away from them.
_CENSORED_IMAGE_PROVIDERS = frozenset({"xai-oauth", "xai", "openai"})

_ASPECT_DIMS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1216, 683),
    "9:16": (683, 1216),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "3:2": (1216, 811),
    "2:3": (811, 1216),
}

# Prefer these when the primary image backend fails (timeouts, filters, 5xx).
_IMAGE_FALLBACK_ORDER = (
    "pollinations",
    "civitai",
    "fal",
    "replicate",
    "modelslab",
    "ollama",
    "a1111",
    "comfyui",
)


def normalize_aspect_ratio(aspect_ratio: str | None) -> str:
    """Map free-form / glitched aspect strings onto a known ratio.

    Fixes common agent mistakes (\"16/9\", \"portrait\", \"square\", \"1x1\")
    and snaps custom W:H values to the nearest supported preset so xAI/Grok
    don't reject or distort the request.
    """
    raw = (aspect_ratio or "1:1").strip().lower().replace(" ", "")
    aliases = {
        "square": "1:1",
        "1x1": "1:1",
        "1/1": "1:1",
        "landscape": "16:9",
        "widescreen": "16:9",
        "16x9": "16:9",
        "16/9": "16:9",
        "portrait": "9:16",
        "9x16": "9:16",
        "9/16": "9:16",
        "vertical": "9:16",
        "4x3": "4:3",
        "4/3": "4:3",
        "3x4": "3:4",
        "3/4": "3:4",
        "3x2": "3:2",
        "3/2": "3:2",
        "2x3": "2:3",
        "2/3": "2:3",
    }
    if raw in aliases:
        return aliases[raw]
    if raw in _ASPECT_DIMS:
        return raw
    # Accept W:H / W/H / WxH and snap to nearest preset by ratio distance.
    for sep in (":", "/", "x"):
        if sep in raw:
            try:
                aw_s, ah_s = raw.split(sep, 1)
                aw, ah = float(aw_s), float(ah_s)
                if aw > 0 and ah > 0:
                    target = aw / ah
                    best = "1:1"
                    best_d = 1e9
                    for key in _ASPECT_DIMS:
                        kw, kh = key.split(":")
                        r = float(kw) / float(kh)
                        d = abs(r - target)
                        if d < best_d:
                            best_d = d
                            best = key
                    return best
            except Exception:
                break
    return "1:1"


def _dims(aspect_ratio: str, *, clamp_max: int | None = None) -> tuple[int, int]:
    """Pixel dimensions for an aspect ratio, ~1MP, multiples of 8."""
    ar = normalize_aspect_ratio(aspect_ratio)
    w, h = _ASPECT_DIMS.get(ar, (1024, 1024))
    w = max(256, (w // 8) * 8)
    h = max(256, (h // 8) * 8)
    if clamp_max:
        w = min(w, clamp_max)
        h = min(h, clamp_max)
    return w, h


def _image_result_failed(result: str) -> bool:
    """True when a backend returned an error string instead of a saved image."""
    if not result:
        return True
    if result.startswith("Image saved to"):
        return False
    if result.startswith("Refused:"):
        return False  # policy refusal — do not fallback-retry as if it failed
    return True


def _is_transient_image_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    needles = (
        "timeout",
        "timed out",
        "429",
        "500",
        "502",
        "503",
        "504",
        "rate limit",
        "overloaded",
        "connection",
        "temporarily",
        "content_filter",
        "content filter",
        "safety",
        "moderation",
        "blocked",
        "refused",
        "policy",
    )
    return any(n in text for n in needles)


async def _save_image_bytes(
    content: bytes, artifacts_dir: Path, provider: str, prompt: str
) -> Path:
    """Write image bytes with an extension matching the real format.

    Civitai (and others) often return JPEG blobs even when the URL path
    looks like .png. Saving JPEG bytes as .png breaks Discord/viewers.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    slug = abs(hash(prompt)) % 10**8
    ext = _image_ext_from_bytes(content)
    out = artifacts_dir / f"{provider}_{stamp}_{slug}{ext}"
    out.write_bytes(content)
    return out


def _image_ext_from_bytes(content: bytes) -> str:
    if not content:
        return ".bin"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        return ".webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".png"


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


def _first_image_url(node: object) -> str:
    """Find the first http(s) URL in a JSON-like tree that looks like an image."""
    stack: list[object] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and cur.startswith("http") and (
            "image" in cur.lower() or "." in cur
        ):
            # Prefer blob/image-ish URLs but accept any http URL as a fallback.
            return cur
    return ""


async def _dispatch_image_provider(
    provider: str,
    *,
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    aspect_ratio: str,
    resolved_model: str,
    artifacts_dir: Path,
    nsfw: bool,
    negative_prompt: str | None,
    loras: dict[str, float] | str | None,
    image: str | None,
    strength: float,
    auto_pick: bool,
) -> str:
    """Call a single image backend. Raises on hard misconfiguration."""
    if provider in ("xai-oauth", "xai"):
        return await _xai_image(
            settings, stack, prompt, aspect_ratio, resolved_model, artifacts_dir
        )
    if provider == "openai":
        return await _openai_image(
            settings, stack, prompt, resolved_model, artifacts_dir
        )
    if provider == "ollama":
        return await _ollama_image(
            settings, prompt, resolved_model, artifacts_dir
        )
    if provider == "pollinations":
        return await _pollinations_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir, nsfw=nsfw
        )
    if provider == "a1111":
        return await _a1111_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir
        )
    if provider == "comfyui":
        return await _comfyui_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir
        )
    if provider == "fal":
        return await _fal_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir
        )
    if provider == "replicate":
        return await _replicate_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir
        )
    if provider == "civitai":
        return await _civitai_image(
            settings,
            prompt,
            aspect_ratio,
            resolved_model,
            artifacts_dir,
            nsfw=nsfw,
            negative_prompt=negative_prompt,
            loras=loras,
            image=image,
            strength=strength,
            auto_pick=auto_pick,
        )
    if provider == "modelslab":
        return await _modelslab_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir, nsfw=nsfw
        )
    raise RuntimeError(
        f"Image generation not configured for provider '{provider}'. "
        "Set OPHELIA_PROVIDER_IMAGE to one of: xai-oauth, xai, openai, ollama, "
        "pollinations, a1111, comfyui, fal, replicate, civitai, modelslab."
    )


def _resolve_image_model(
    stack: ProviderStack,
    provider: str,
    agent_model: str,
    *,
    nsfw: bool,
    auto_pick: bool,
) -> tuple[str, bool]:
    """Return (resolved_model, auto_pick) for a provider attempt."""
    if provider == "civitai":
        resolved = agent_model
        if not agent_model:
            return "", True
        if agent_model.lower() in ("auto", "pick", "dynamic"):
            return "", True
        return resolved, auto_pick
    resolved = agent_model or stack.image_model_for(provider, nsfw=nsfw)
    if (resolved or "").lower().startswith("urn:air:"):
        log.warning("image.stripped_air_from_non_civitai", provider=provider)
        resolved = stack.image_model_for(provider, nsfw=nsfw)
    return resolved, auto_pick


def _fallback_image_providers(
    settings: Settings,
    primary: str,
    *,
    nsfw: bool,
) -> list[str]:
    """Configured alternate backends to try when the primary fails."""
    out: list[str] = []
    for name in _IMAGE_FALLBACK_ORDER:
        if name == primary:
            continue
        if nsfw and name in _CENSORED_IMAGE_PROVIDERS:
            continue
        try:
            configured = settings.image_backend_configured(name)
        except Exception:
            continue
        # Require exact True so MagicMock / odd stubs don't enable every backend.
        if configured is True:
            out.append(name)
    # Pollinations is usually zero-config — always offer it as last resort
    # for SFW (and NSFW when allowed) unless it was already primary.
    if "pollinations" not in out and primary != "pollinations":
        nsfw_ok = getattr(settings, "image_nsfw_allowed", False) is True
        if not nsfw or nsfw_ok:
            out.append("pollinations")
    return out


async def generate_image(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    *,
    aspect_ratio: str = "1:1",
    artifacts_dir: Path,
    nsfw: bool = False,
    model: str | None = None,
    negative_prompt: str | None = None,
    loras: dict[str, float] | str | None = None,
    image: str | None = None,
    strength: float = 0.7,
    auto_pick: bool = False,
) -> str:
    # Content tier gating.
    if nsfw and not settings.image_nsfw_allowed:
        return (
            "Refused: explicit image requested but OPHELIA_IMAGE_NSFW_ALLOWED is off. "
            "Enable it in ~/.ophelia/.env to route explicit prompts to an uncensored "
            "backend (pollinations/a1111/comfyui/fal/replicate/civitai/modelslab/ollama)."
        )

    aspect_ratio = normalize_aspect_ratio(aspect_ratio)
    provider = stack.image_provider_for(nsfw=nsfw)
    # Defensive: never let an explicit prompt reach a censored backend, even if
    # the user mis-configured OPHELIA_IMAGE_NSFW_PROVIDER to point at one.
    if nsfw and provider in _CENSORED_IMAGE_PROVIDERS:
        return (
            f"Refused: explicit image would route to {provider}, which refuses NSFW and "
            "may flag the account. Set OPHELIA_IMAGE_NSFW_PROVIDER to an uncensored "
            "backend (pollinations/a1111/comfyui/fal/replicate/civitai/modelslab/ollama)."
        )

    agent_model = (model or "").strip()
    # Civitai AIR URNs must never hit xAI/OpenAI (404). If she passes an AIR,
    # route to Civitai when a key is configured; otherwise strip and use default.
    is_civitai_air = agent_model.lower().startswith("urn:air:")
    if is_civitai_air and provider != "civitai":
        if settings.civitai_api_key:
            log.info(
                "image.reroute_air_to_civitai",
                from_provider=provider,
                model=agent_model[:80],
            )
            provider = "civitai"
        else:
            log.warning(
                "image.ignored_civitai_urn",
                provider=provider,
                model=agent_model[:80],
            )
            agent_model = ""

    # Civitai: Ophelia picks a curated checkpoint per image. Do NOT lock to the
    # menu/env model (CIVITAI_IMAGE_MODEL / OPHELIA_IMAGE_NSFW_MODEL) — those
    # are fallbacks only. Other backends still use router defaults.
    resolved_model, auto_pick = _resolve_image_model(
        stack, provider, agent_model, nsfw=nsfw, auto_pick=auto_pick
    )

    gate = get_model_gate()
    nsfw_tag = " [nsfw]" if nsfw else ""
    attempts = [provider] + _fallback_image_providers(settings, provider, nsfw=nsfw)
    errors: list[str] = []
    result = ""
    used_provider = provider
    used_model = resolved_model

    for attempt_idx, attempt_provider in enumerate(attempts):
        # Only the first attempt keeps an explicit agent model pin (except AIR
        # already rerouted). Fallbacks use each backend's own default model.
        if attempt_provider == provider:
            attempt_model, attempt_auto = resolved_model, auto_pick
        else:
            pin = agent_model if attempt_provider == "civitai" and is_civitai_air else ""
            attempt_model, attempt_auto = _resolve_image_model(
                stack, attempt_provider, pin, nsfw=nsfw, auto_pick=True
            )

        # Primary: one retry on transient failure. Fallbacks: single shot.
        retries = 2 if attempt_idx == 0 else 1
        for retry in range(retries):
            try:
                async with gate.session(
                    "image", attempt_model or "civitai-auto", attempt_provider
                ):
                    result = await _dispatch_image_provider(
                        attempt_provider,
                        settings=settings,
                        stack=stack,
                        prompt=prompt,
                        aspect_ratio=aspect_ratio,
                        resolved_model=attempt_model,
                        artifacts_dir=artifacts_dir,
                        nsfw=nsfw,
                        negative_prompt=negative_prompt,
                        loras=loras,
                        image=image,
                        strength=strength,
                        auto_pick=attempt_auto,
                    )
            except Exception as e:
                err = f"{attempt_provider}: {e}"
                errors.append(err)
                log.warning(
                    "image.attempt_exception",
                    provider=attempt_provider,
                    retry=retry,
                    error=str(e)[:200],
                )
                if retry + 1 < retries and _is_transient_image_error(e):
                    await asyncio.sleep(0.6 * (retry + 1))
                    continue
                break

            if not _image_result_failed(result):
                used_provider = attempt_provider
                used_model = attempt_model
                if attempt_idx > 0:
                    log.info(
                        "image.fallback_succeeded",
                        primary=provider,
                        used=attempt_provider,
                    )
                # Annotate which backend actually ran.
                label = used_model or "auto"
                fallback_note = (
                    f"; fell back from {provider}" if attempt_idx > 0 else ""
                )
                return (
                    f"{result} (backend: {used_provider}/{label}{nsfw_tag}"
                    f"{fallback_note})"
                )

            errors.append(f"{attempt_provider}: {result[:180]}")
            log.warning(
                "image.attempt_failed",
                provider=attempt_provider,
                retry=retry,
                error=result[:180],
            )
            if retry + 1 < retries and _is_transient_image_error(result):
                await asyncio.sleep(0.6 * (retry + 1))
                continue
            break

        # Don't cascade fallbacks for hard policy refusals.
        if result.startswith("Refused:"):
            return result

    # All attempts failed — surface the primary error plus what we tried.
    summary = errors[0] if errors else (result or "unknown error")
    tried = ", ".join(attempts)
    return (
        f"Image generation failed after trying: {tried}. "
        f"Last error: {summary}"
    )


async def generate_video(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    *,
    duration_seconds: int = 6,
    artifacts_dir: Path | None = None,
    image: str | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
) -> str:
    """Generate a video via xAI Grok Imagine.

    If `image` is provided, runs image-to-video: the image becomes the first
    frame. Accepts an http(s) URL or a local file path (PNG/JPG/etc.) — local
    files are base64-encoded into a data URI. If omitted, runs text-to-video.
    """
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
                image=image,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
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
    artifacts_dir: Path,
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
    b64 = getattr(resp.data[0], "b64_json", None) if resp.data else None
    if not url and not b64:
        return "Image generation returned no URL or bytes."
    saved = await _save_image_artifact(
        artifacts_dir=artifacts_dir,
        prompt=prompt,
        url=url,
        b64=b64,
        model=model,
    )
    if not saved:
        return "Image generation failed to save."
    log.info("image.saved", provider="xai", model=model, path=str(saved))
    return f"Image saved to {saved}"


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
    url = getattr(item, "url", None)
    b64 = getattr(item, "b64_json", None)
    if not url and not b64:
        return "Image generation returned no URL or bytes."
    saved = await _save_image_artifact(
        artifacts_dir=artifacts_dir,
        prompt=prompt,
        url=url,
        b64=b64,
        model=model,
    )
    if not saved:
        return "Image generation failed to save."
    log.info("image.saved", provider="openai", model=model, path=str(saved))
    return f"Image saved to {saved}"


async def _save_image_artifact(
    *,
    artifacts_dir: Path,
    prompt: str,
    url: str | None,
    b64: str | None,
    model: str,
) -> Path | None:
    """Persist an image artifact to disk. Always saves, regardless of source.

    xAI and OpenAI image URLs are temporary and expire — downloading them now
    means the image survives and can be re-sent to Telegram later.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    slug = abs(hash(prompt)) % 10**8
    out = artifacts_dir / f"image_{stamp}_{slug}.png"

    if b64:
        try:
            out.write_bytes(base64.standard_b64decode(b64))
            return out
        except Exception as e:
            log.warning("image.save_b64_failed", error=str(e))

    if url:
        try:
            async with httpx.AsyncClient(timeout=120.0) as http:
                dl = await http.get(url)
                if dl.status_code >= 400:
                    # Some signed URLs require auth; retry with empty headers
                    # (no-op here, but leaves a hook for future bearer retries).
                    dl = await http.get(url)
                dl.raise_for_status()
                out.write_bytes(dl.content)
                return out
        except Exception as e:
            log.warning("image.download_failed", url=url, error=str(e))
    return None


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
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    slug = abs(hash(prompt)) % 10**8
    out = artifacts_dir / f"ollama_{stamp}_{slug}.png"
    out.write_bytes(base64.standard_b64decode(b64))
    log.info("image.saved", provider="ollama", model=model, path=str(out))
    return f"Image saved to {out}"


async def _pollinations_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
    *,
    nsfw: bool = False,
) -> str:
    w, h = _dims(aspect_ratio)
    base = settings.pollinations_base_url.rstrip("/")
    params = {
        "model": model,
        "width": w,
        "height": h,
        "nologo": "true",
        "seed": str(random.randint(1, 1 << 30)),
    }
    # safe=false allows explicit output; only send it when NSFW is permitted.
    if nsfw or settings.image_nsfw_allowed:
        params["safe"] = "false"
    url = f"{base}/prompt/{quote(prompt, safe='')}"
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as http:
        r = await http.get(url, params=params)
        if r.status_code >= 400:
            return f"Pollinations failed HTTP {r.status_code}: {r.text[:200]}"
        ctype = r.headers.get("content-type", "")
        if "image" not in ctype:
            return f"Pollinations returned non-image ({ctype}): {r.text[:200]}"
        saved = await _save_image_bytes(r.content, artifacts_dir, "pollinations", prompt)
    log.info("image.saved", provider="pollinations", model=model, path=str(saved))
    return f"Image saved to {saved}"


async def _a1111_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    w, h = _dims(aspect_ratio)
    base = settings.a1111_base_url.rstrip("/").removesuffix("/sdapi/v1")
    headers = {"Content-Type": "application/json"}
    if settings.a1111_api_key:
        headers["Authorization"] = f"Bearer {settings.a1111_api_key}"
    body: dict[str, object] = {
        "prompt": prompt,
        "negative_prompt": "",
        "steps": settings.a1111_steps,
        "sampler_name": settings.a1111_sampler,
        "cfg_scale": settings.a1111_cfg_scale,
        "width": w,
        "height": h,
        "seed": random.randint(1, 1 << 30),
        "batch_size": 1,
    }
    if settings.a1111_image_model:
        body["override_settings"] = {"sd_model_checkpoint": settings.a1111_image_model}
    async with httpx.AsyncClient(timeout=180.0) as http:
        r = await http.post(f"{base}/sdapi/v1/txt2img", headers=headers, json=body)
        if r.status_code >= 400:
            return f"A1111 failed HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
    images = data.get("images") or []
    if not images:
        return f"A1111 returned no images: {str(data)[:300]}"
    b64 = images[0]
    if "," in b64 and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    saved = await _save_image_bytes(
        base64.standard_b64decode(b64), artifacts_dir, "a1111", prompt
    )
    log.info("image.saved", provider="a1111", model=model, path=str(saved))
    return f"Image saved to {saved}"


def _comfyui_default_graph(
    prompt: str, w: int, h: int, seed: int, ckpt: str | None
) -> dict[str, object]:
    ckpt_name = ckpt or "sd_xl_base_1.0.safetensors"
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt_name},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": w, "height": h, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "", "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": 30,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "ophelia"},
        },
    }


async def _comfyui_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    w, h = _dims(aspect_ratio)
    base = settings.comfyui_base_url.rstrip("/")
    seed = random.randint(1, 1 << 30)

    graph: dict[str, object]
    wf_path = settings.comfyui_workflow_path
    if wf_path and Path(wf_path).exists():
        try:
            raw = Path(wf_path).read_text(encoding="utf-8")
            graph = json.loads(raw.format(prompt=prompt, width=w, height=h, seed=seed, model=model))
        except Exception as e:
            return f"ComfyUI workflow load failed ({e}); fix {wf_path} or remove it."
    else:
        graph = _comfyui_default_graph(prompt, w, h, seed, settings.comfyui_image_model)

    client_id = f"ophelia-{seed}"
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.post(
            f"{base}/prompt", json={"prompt": graph, "client_id": client_id}
        )
        if r.status_code >= 400:
            return f"ComfyUI /prompt failed HTTP {r.status_code}: {r.text[:300]}"
        prompt_id = (r.json() or {}).get("prompt_id")
        if not prompt_id:
            return f"ComfyUI returned no prompt_id: {r.text[:300]}"

    deadline = time.monotonic() + 180.0
    async with httpx.AsyncClient(timeout=30.0) as http:
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            h2 = await http.get(f"{base}/history/{prompt_id}")
            if h2.status_code >= 400:
                continue
            hist = h2.json() or {}
            entry = hist.get(prompt_id)
            if not entry:
                continue
            outputs = entry.get("outputs") or {}
            for node_out in outputs.values():
                for img in (node_out.get("images") or []):
                    fname = img.get("filename")
                    if not fname:
                        continue
                    params = {
                        "filename": fname,
                        "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"),
                    }
                    view = await http.get(f"{base}/view", params=params)
                    if view.status_code < 400 and view.content:
                        saved = await _save_image_bytes(
                            view.content, artifacts_dir, "comfyui", prompt
                        )
                        log.info("image.saved", provider="comfyui", model=model, path=str(saved))
                        return f"Image saved to {saved}"
    return "ComfyUI image timed out (180s) — check the server is running and GPU isn't busy."


async def _fal_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    w, h = _dims(aspect_ratio)
    headers = {
        "Authorization": f"Key {settings.fal_api_key}",
        "Content-Type": "application/json",
    }
    body = {"prompt": prompt, "image_size": {"width": w, "height": h}}
    model_path = model.lstrip("/")
    async with httpx.AsyncClient(timeout=120.0) as http:
        submit = await http.post(
            f"https://queue.fal.run/{model_path}", headers=headers, json=body
        )
        if submit.status_code >= 400:
            return f"fal submit failed HTTP {submit.status_code}: {submit.text[:300]}"
        sd = submit.json()
        status_url = sd.get("status_url") or sd.get("status")
        response_url = sd.get("response_url") or sd.get("response")
        request_id = sd.get("request_id")
        if not (status_url and response_url) and request_id:
            status_url = f"https://queue.fal.run/{model_path}/requests/{request_id}/status"
            response_url = f"https://queue.fal.run/{model_path}/requests/{request_id}"
        if not (status_url and response_url):
            return f"fal returned no result URLs: {sd}"

        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            st = await http.get(status_url, headers=headers)
            if st.status_code >= 400:
                continue
            sj = st.json()
            status = str(sj.get("status", "")).upper()
            if status == "COMPLETED":
                break
            if status in ("FAILED", "ERROR"):
                return f"fal generation failed: {sj}"
        else:
            return "fal generation timed out (180s)."

        res = await http.get(response_url, headers=headers)
        if res.status_code >= 400:
            return f"fal result fetch failed HTTP {res.status_code}: {res.text[:300]}"
        rj = res.json()
    url = _first_image_url(rj)
    if not url:
        return f"fal returned no image URL: {str(rj)[:300]}"
    dl = await http.get(url)
    if dl.status_code >= 400:
        dl = await http.get(url, headers=headers)
    dl.raise_for_status()
    saved = await _save_image_bytes(dl.content, artifacts_dir, "fal", prompt)
    log.info("image.saved", provider="fal", model=model, path=str(saved))
    return f"Image saved to {saved}"


async def _replicate_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    w, h = _dims(aspect_ratio)
    headers = {
        "Authorization": f"Bearer {settings.replicate_api_key}",
        "Content-Type": "application/json",
        "Prefer": "wait=60",
    }
    body: dict[str, object] = {"input": {"prompt": prompt, "width": w, "height": h}}
    model = model.strip()
    async with httpx.AsyncClient(timeout=120.0) as http:
        if ":" in model and "/" in model.split(":", 1)[0]:
            # Versioned form: owner/model:version -> generic /predictions endpoint.
            owner_model, version = model.split(":", 1)
            body["version"] = version
            r = await http.post(
                "https://api.replicate.com/v1/predictions", headers=headers, json=body
            )
        else:
            owner, name = model.split("/", 1)
            r = await http.post(
                f"https://api.replicate.com/v1/models/{owner}/{name}/predictions",
                headers=headers,
                json=body,
            )
        if r.status_code >= 400:
            return f"Replicate submit failed HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()

        # Prefer: wait may already have output. Otherwise poll urls.get.
        def _output_url(d: dict) -> str:
            out = d.get("output")
            if isinstance(out, str) and out.startswith("http"):
                return out
            if isinstance(out, list):
                for o in out:
                    if isinstance(o, str) and o.startswith("http"):
                        return o
            return ""

        url = _output_url(data)
        get_url = (data.get("urls") or {}).get("get")
        deadline = time.monotonic() + 180.0
        while not url and get_url and time.monotonic() < deadline:
            await asyncio.sleep(3.0)
            p = await http.get(get_url, headers=headers)
            if p.status_code >= 400:
                continue
            pj = p.json()
            if str(pj.get("status")) == "failed":
                return f"Replicate prediction failed: {pj.get('error') or pj}"
            url = _output_url(pj)
        if not url:
            return f"Replicate returned no image URL: {str(data)[:300]}"
        dl = await http.get(url)
        if dl.status_code >= 400:
            dl = await http.get(url, headers={"Authorization": f"Bearer {settings.replicate_api_key}"})
        dl.raise_for_status()
        saved = await _save_image_bytes(dl.content, artifacts_dir, "replicate", prompt)
    log.info("image.saved", provider="replicate", model=model, path=str(saved))
    return f"Image saved to {saved}"


async def _civitai_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
    *,
    nsfw: bool = False,
    negative_prompt: str | None = None,
    loras: dict[str, float] | str | None = None,
    image: str | None = None,
    strength: float = 0.7,
    auto_pick: bool = True,
) -> str:
    """Civitai orchestration — txt2img (createImage) or img2img (createVariant).

    By default auto_pick selects a curated general checkpoint (no LoRAs, no
    trigger injection). Pass model=<AIR> to pin a checkpoint; pass loras= to
    add compatible LoRAs. CIVITAI_IMAGE_MODEL is a last-resort fallback only.
    """
    from ophelia.providers import civitai as civ

    w, h = _dims(aspect_ratio)
    base = settings.civitai_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.civitai_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Ophelia/1.0 (+https://github.com/Shadow4-18/Ophelia)",
        "Accept": "application/json",
    }

    # Agent-provided pin only. Menu/env models are NOT applied up front.
    model_air = civ.sanitize_air((model or "").strip())
    if model_air.lower() in ("auto", "pick", "dynamic"):
        model_air = ""
        explicit_pin = False
    elif model_air.lower() == "flux":
        explicit_pin = True
    else:
        explicit_pin = bool(model_air)

    agent_loras = bool(loras)
    lora_map = civ.parse_loras(loras)
    # Sanitize LoRA AIR keys early.
    lora_map = {civ.sanitize_air(k): v for k, v in lora_map.items() if civ.sanitize_air(k)}
    lora_triggers: list[str] = []
    pick_note = ""
    checkpoint_base = ""
    style = civ.detect_style(prompt)

    # Common mistake: pass a LoRA AIR as model=. Move it to loras= and pick a
    # matching checkpoint — orchestration needs checkpoint + LoRA together.
    if model_air and civ.air_kind(model_air) == "lora":
        lora_map.setdefault(model_air, 0.8)
        agent_loras = True
        pick_note = "model_was_lora→loras; "
        # Prefer checkpoint family matching the LoRA once we know baseModel.
        try:
            vid_s = model_air.rsplit("@", 1)[-1].split("+")[0]
            if vid_s.isdigit():
                ver = await civ.get_version(settings, int(vid_s))
                if ver and ver.base_model:
                    fam = civ.base_family(ver.base_model, model_air)
                    if fam == "pony":
                        style = "pony"
                    elif fam == "illustrious":
                        style = "illustrious"
                    lora_triggers.extend(ver.trained_words[:3])
        except Exception as e:
            log.debug("civitai.lora_as_model_meta_failed", error=str(e))
        model_air = ""
        explicit_pin = False
    elif model_air and not model_air.startswith("urn:air:"):
        # Short aliases: model=pony / model=illustrious
        alias = civ.resolve_checkpoint_alias(model_air)
        if alias:
            model_air = alias
            explicit_pin = True
            pick_note = f"alias→{alias}; "

    ecosystem = civ.ecosystem_from_air_or_base(model_air)

    # Dynamic selection is the default. Only skip when she pinned a model URN/flux.
    should_pick = bool(auto_pick) and not explicit_pin
    if should_pick:
        try:
            # Prefer style already inferred (e.g. from a LoRA demoted out of model=).
            if style in civ._CURATED_CHECKPOINTS:
                air, name = civ._CURATED_CHECKPOINTS[style]
                model_air = civ.sanitize_air(air)
                ecosystem = "sdxl"
                checkpoint_base = (
                    "Pony"
                    if style == "pony"
                    else "Illustrious"
                    if style in ("illustrious", "anime")
                    else "SDXL 1.0"
                )
                pick_note = (
                    pick_note
                    + f"checkpoint={name} ({model_air}, style={style}); "
                    "loras=none-or-agent"
                )
            else:
                ck, _picked_loras, rationale = await civ.pick_best_resources(
                    settings, prompt, nsfw=nsfw, want_lora=False
                )
                if ck:
                    model_air = civ.sanitize_air(ck.air)
                    ecosystem = ck.ecosystem
                    checkpoint_base = ck.base_model or ""
                    pick_note = pick_note + rationale
        except Exception as e:
            log.warning("civitai.auto_pick_failed", error=str(e))
            pick_note = pick_note + f"auto_pick_failed: {e}"

    # Fallback chain if pick found nothing: env default → known SDXL URN.
    # Never bare engine:flux (400 workflowTemplate required).
    if not model_air:
        env_model = civ.sanitize_air((settings.civitai_image_model or "").strip())
        if env_model and env_model.lower() not in ("auto", "dynamic", "pick", "flux"):
            model_air = env_model
            ecosystem = civ.ecosystem_from_air_or_base(model_air)
            pick_note = (pick_note + "; " if pick_note else "") + f"fallback_env={env_model}"
        else:
            if style == "pony":
                model_air = civ._FALLBACK_PONY_AIR
            elif style in ("illustrious", "anime"):
                model_air = civ._FALLBACK_ILLUSTRIOUS_AIR
            else:
                model_air = civ._FALLBACK_SDXL_AIR
            ecosystem = "sdxl"
            pick_note = (pick_note + "; " if pick_note else "") + f"fallback_urn={model_air}"

    model_air = civ.sanitize_air(model_air)

    # Enrich ecosystem/base from site API — do NOT pull trainedWords into prompt.
    if model_air.startswith("urn:air:") and "@" in model_air:
        try:
            vid_s = model_air.rsplit("@", 1)[-1].split("+")[0]
            if vid_s.isdigit():
                ver = await civ.get_version(settings, int(vid_s))
                if ver:
                    ecosystem = ver.ecosystem or ecosystem
                    checkpoint_base = ver.base_model or checkpoint_base
        except Exception as e:
            log.debug("civitai.version_enrich_failed", error=str(e))

    # Enrich LoRA baseModel from site API, then drop incompatible ones
    # (e.g. Pony LoRA on generic SDXL — AIR alone is always :sdxl:).
    lora_meta: dict[str, str] = {}
    if lora_map:
        for air in list(lora_map)[:5]:
            if "@" not in air:
                continue
            try:
                vid_s = air.rsplit("@", 1)[-1].split("+")[0]
                if vid_s.isdigit():
                    ver = await civ.get_version(settings, int(vid_s))
                    if ver:
                        lora_meta[air] = ver.base_model or ""
                        if agent_loras:
                            lora_triggers.extend(ver.trained_words[:3])
            except Exception as e:
                log.debug("civitai.lora_meta_fetch_failed", error=str(e))
        lora_map = civ.filter_loras_for_checkpoint(
            lora_map,
            checkpoint_air=model_air,
            checkpoint_base=checkpoint_base,
            lora_meta=lora_meta,
        )

    # Prompt: keep user intent. Soft quality tags for danbooru families only.
    # Short LoRA triggers appended only when the agent explicitly passed loras=.
    final_prompt = civ.maybe_quality_prefix(prompt, style)
    if agent_loras and lora_map:
        final_prompt = civ.ensure_triggers_in_prompt(
            final_prompt, lora_triggers, mode="append"
        )

    neg = (negative_prompt or "").strip()
    if not neg and ecosystem in ("sd1", "sdxl"):
        neg = civ.default_negative_for(ecosystem)

    image_url = None
    if image:
        try:
            image_url = await civ.resolve_image_url(settings, image)
        except Exception as e:
            return f"Civitai img2img source failed: {e}"

    step_input = civ.build_step_input(
        prompt=final_prompt,
        width=w,
        height=h,
        model_air=model_air,
        ecosystem=ecosystem,
        negative_prompt=neg,
        loras=lora_map or None,
        image_url=image_url,
        strength=strength,
    )
    # NSFW requests must always allow mature content on orchestration.
    allow_mature = bool(nsfw or settings.image_nsfw_allowed)
    mature = "true" if allow_mature else "false"
    body: dict = {
        "steps": [{"$type": "imageGen", "input": step_input}],
        "allowMatureContent": allow_mature,
    }
    log.info(
        "civitai.submit",
        operation=step_input.get("operation"),
        ecosystem=step_input.get("ecosystem"),
        engine=step_input.get("engine"),
        model=step_input.get("model") or step_input.get("engine"),
        loras=len(lora_map),
        img2img=bool(image_url),
        pick=pick_note or None,
        auto_pick=should_pick,
        allow_mature=allow_mature,
        prompt_len=len(final_prompt),
    )

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
        r = await http.post(
            f"{base}/v2/consumer/workflows",
            headers=headers,
            params={"wait": "90", "allowMatureContent": mature},
            json=body,
        )
        if r.status_code >= 400:
            return f"Civitai submit failed HTTP {r.status_code}: {r.text[:300]}"
        wf = r.json()
        wf_id = wf.get("id") or wf.get("workflowId")
        status = str(wf.get("status") or wf.get("state") or "").lower()

        deadline = time.monotonic() + 180.0
        while status not in ("done", "completed", "succeeded", "success") and time.monotonic() < deadline:
            if status in ("failed", "error", "expired"):
                return f"Civitai workflow {status}: {wf}"
            await asyncio.sleep(5.0)
            p = await http.get(f"{base}/v2/consumer/workflows/{wf_id}", headers=headers)
            if p.status_code >= 400:
                continue
            wf = p.json()
            status = str(wf.get("status") or wf.get("state") or "").lower()
        if status not in ("done", "completed", "succeeded", "success"):
            return f"Civitai workflow not done in 180s (id={wf_id}, status={status})."

        url = _deep_find_first_str(
            wf, ("url", "signed_url", "download_url", "result_url", "blob_url")
        )
        if not url:
            return f"Civitai completed but no image URL found: {str(wf)[:400]}"
        # Blob hosts often 301 to orchestration-new; follow_redirects is on.
        # Never raise — return a soft error so the chat turn doesn't crash.
        dl = await http.get(url, headers={"Authorization": f"Bearer {settings.civitai_api_key}"})
        if dl.status_code >= 400:
            dl = await http.get(url)
        if dl.status_code >= 400:
            return (
                f"Civitai image download failed HTTP {dl.status_code} "
                f"(url={url[:120]}). Workflow succeeded but blob fetch failed."
            )
        if not dl.content or len(dl.content) < 100:
            return f"Civitai image download returned empty/tiny body ({len(dl.content)} bytes)."
        saved = await _save_image_bytes(dl.content, artifacts_dir, "civitai", prompt)

    used = step_input.get("model") or step_input.get("engine") or model_air or "flux"
    meta = f"Image saved to {saved}"
    extras = [f"civitai {step_input.get('operation', 'createImage')}"]
    extras.append(f"model={used}")
    if lora_map:
        extras.append(f"loras={len(lora_map)}")
    if pick_note:
        extras.append(f"picked: {pick_note}")
    if lora_triggers:
        extras.append("triggers_injected=" + ", ".join(lora_triggers[:6]))
    log.info("image.saved", provider="civitai", model=str(used), path=str(saved))
    return meta + " [" + "; ".join(extras) + "]"


async def _modelslab_image(
    settings: Settings,
    prompt: str,
    aspect_ratio: str,
    model: str,
    artifacts_dir: Path,
    *,
    nsfw: bool = False,
) -> str:
    # ModelsLab width/height must be 256-1024.
    w, h = _dims(aspect_ratio, clamp_max=1024)
    base = settings.modelslab_base_url.rstrip("/")
    allow_explicit = nsfw or settings.image_nsfw_allowed
    body = {
        "key": settings.modelslab_api_key,
        "model_id": model,
        "prompt": prompt,
        "width": w,
        "height": h,
        "samples": 1,
        "num_inference_steps": 30,
        "guidance_scale": 7.5,
        "safety_checker": not allow_explicit,  # false allows explicit output
        "base64": False,
    }
    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(f"{base}/images/text2img", json=body)
        if r.status_code >= 400:
            return f"ModelsLab failed HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()

        urls: list[str] = []
        if isinstance(data.get("output"), list):
            urls = [u for u in data["output"] if isinstance(u, str)]
        request_id = data.get("request_id") or data.get("id")

        deadline = time.monotonic() + 180.0
        while not urls and request_id and time.monotonic() < deadline:
            status = str(data.get("status") or "").lower()
            if status in ("failed", "error"):
                return f"ModelsLab generation failed: {data}"
            await asyncio.sleep(5.0)
            f = await http.post(
                f"{base}/images/fetch/{request_id}",
                json={"key": settings.modelslab_api_key},
            )
            if f.status_code >= 400:
                continue
            data = f.json()
            if isinstance(data.get("output"), list):
                urls = [u for u in data["output"] if isinstance(u, str)]
        if not urls:
            # Some payloads return base64 directly.
            b64 = data.get("base64") or (data.get("images") or [{}])[0].get("base64") if isinstance(data.get("images"), list) else None
            if b64:
                saved = await _save_image_bytes(
                    base64.standard_b64decode(b64), artifacts_dir, "modelslab", prompt
                )
                log.info("image.saved", provider="modelslab", model=model, path=str(saved))
                return f"Image saved to {saved}"
            return f"ModelsLab returned no image URLs: {str(data)[:300]}"
        dl = await http.get(urls[0])
        dl.raise_for_status()
        saved = await _save_image_bytes(dl.content, artifacts_dir, "modelslab", prompt)
    log.info("image.saved", provider="modelslab", model=model, path=str(saved))
    return f"Image saved to {saved}"


def _mime_for_image_path(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


async def _resolve_xai_video_image(image: str) -> dict[str, str] | None:
    """Turn an image reference into the xAI `image` field value.

    Accepts:
      - http(s) URL → {"url": "..."}
      - file_id: prefix (xAI Files API) → {"file_id": "..."}
      - data: URI → {"url": "data:..."}
      - bare base64 (long, no scheme/path) → wrapped as a JPEG data URI
      - local file path → read, base64-encode, wrap as a data URI

    Returns None if the input can't be resolved (file missing / unreadable).
    """
    s = (image or "").strip()
    if not s:
        return None

    if s.startswith(("http://", "https://")):
        return {"url": s}
    if s.startswith("data:"):
        return {"url": s}
    if s.startswith("file_id:"):
        return {"file_id": s[len("file_id:") :].strip()}
    if s.startswith("/9j/") or s.startswith("iVBOR"):  # bare base64 JPEG/PNG
        return {"url": f"data:image/jpeg;base64,{s}"}

    # Treat as a local file path.
    try:
        p = Path(s).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if not p.is_file():
        log.warning("video.image_not_found", path=s)
        return None
    try:
        raw = p.read_bytes()
    except OSError as e:
        log.warning("video.image_read_failed", path=str(p), error=str(e))
        return None
    mime = _mime_for_image_path(p)
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return {"url": f"data:{mime};base64,{b64}"}


async def _xai_video(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    duration_seconds: int,
    model: str,
    *,
    artifacts_dir: Path | None = None,
    image: str | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
) -> str:
    backend = stack.backend("video")
    assert isinstance(backend, XAIBackend)
    # Validate resolution early — xAI only accepts "480p" or "720p". An
    # invalid value returns an opaque 400 that's hard to debug downstream.
    valid_resolutions = {"480p", "720p"}
    if resolution and resolution not in valid_resolutions:
        log.warning("video.invalid_resolution", resolution=resolution)
        resolution = "480p"  # fall back rather than 400
    try:
        token = await backend.bearer_fresh()
    except Exception:
        token = backend.bearer()
    if not token:
        return "No xAI credentials for video."

    image_input = await _resolve_xai_video_image(image) if image else None

    base = settings.xai_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    body: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        "duration": duration_seconds,
    }
    # Image-to-video: image becomes the first frame. Omit `image` entirely
    # for text-to-video. xAI rejects requests that set both `image` and
    # `reference_images`, so we never send them together.
    if image_input is not None:
        body["image"] = image_input
    if aspect_ratio:
        body["aspect_ratio"] = aspect_ratio
    if resolution:
        body["resolution"] = resolution

    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(
            f"{base}/videos/generations",
            headers=headers,
            json=body,
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
                mode = "image-to-video" if image_input is not None else "text-to-video"
                log.info(
                    "video.saved",
                    path=str(out),
                    request_id=request_id,
                    mode=mode,
                )
                return (
                    f"Video generated ({model}, {duration_seconds}s, {mode}). "
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
