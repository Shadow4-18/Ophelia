"""Image and video generation — routed per OPHELIA_PROVIDER_IMAGE / _VIDEO."""

from __future__ import annotations

import asyncio
import base64
import random
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

import httpx
import structlog

from ophelia.config import Settings
from ophelia.providers import image_routing
from ophelia.providers.image_job import FAILED, ImageJob, ImageResult
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import ProviderStack, XAIBackend

log = structlog.get_logger()


# Routing constants live in image_routing; kept here under their old names.
_CENSORED_IMAGE_PROVIDERS = image_routing.CENSORED

_ASPECT_DIMS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1216, 683),
    "9:16": (683, 1216),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "3:2": (1216, 811),
    "2:3": (811, 1216),
}


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
    if result.startswith(("Refused:", "Pending:")):
        # Policy refusal, or a paid Civitai job still rendering (delivered
        # later): neither is a failure to retry or fall back from.
        return False
    return True


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
    job: ImageJob,
    resolved_model: str,
    artifacts_dir: Path,
    nsfw: bool,
    auto_pick: bool,
) -> str:
    """Call a single image backend with ``job``. Raises on hard
    misconfiguration. ``nsfw``/``resolved_model``/``auto_pick`` are this
    attempt's (a fallback attempt can differ from the job)."""
    prompt, aspect_ratio = job.prompt, job.aspect_ratio
    if provider in ("openai", "ollama", "a1111", "comfyui", "fal", "replicate", "modelslab"):
        from ophelia.providers.image_backends_extra import (
            _a1111_image,
            _comfyui_image,
            _fal_image,
            _modelslab_image,
            _ollama_image,
            _openai_image,
            _replicate_image,
        )
    if provider in ("xai-oauth", "xai") and job.image:
        return await _xai_image_edit(
            settings, stack, prompt, job.image, resolved_model, artifacts_dir
        )
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
        return await _civitai_image(settings, job, resolved_model, artifacts_dir, nsfw=nsfw)
    if provider == "modelslab":
        return await _modelslab_image(
            settings, prompt, aspect_ratio, resolved_model, artifacts_dir, nsfw=nsfw
        )
    raise RuntimeError(
        f"Image generation not configured for provider '{provider}'. "
        "Set OPHELIA_PROVIDER_IMAGE to one of: xai-oauth, xai, openai, ollama, "
        "pollinations, a1111, comfyui, fal, replicate, civitai, modelslab."
    )


async def generate_image(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    *,
    artifacts_dir: Path,
    **options,
) -> str:
    """Text form of run_image_job (kept for callers that want a string).
    ``options`` are ImageJob fields (aspect_ratio, nsfw, model, loras...)."""
    job = ImageJob.from_kwargs(prompt, **options)
    return str(await run_image_job(settings, stack, job, artifacts_dir))


async def run_image_job(
    settings: Settings,
    stack: ProviderStack,
    job: ImageJob,
    artifacts_dir: Path,
) -> ImageResult:
    """Route one image request. SFW goes to the image provider (Grok by
    default), explicit to the NSFW provider (Civitai). A failed generation is
    not retried unless ``job.attempts`` > 1 (the owner asked her to keep
    trying); only connection-level errors get one automatic retry."""
    job = job.but(aspect_ratio=normalize_aspect_ratio(job.aspect_ratio))
    plan = image_routing.route(settings, stack, job)
    if plan.refused:
        return plan.refusal
    job, provider, nsfw = plan.job, plan.provider, plan.job.nsfw
    resolved_model, auto_pick = plan.model, plan.auto_pick

    gate = get_model_gate()
    nsfw_tag = " [nsfw]" if nsfw else ""
    cap = max(1, int(getattr(settings, "image_max_attempts", 5) or 1))
    tries = max(1, min(int(job.attempts or 1), cap))
    attempts_list = [provider, *plan.fallbacks]
    errors: list[str] = []
    result = ""

    for attempt_idx, attempt_provider in enumerate(attempts_list):
        if attempt_idx == 0:
            attempt_model, attempt_auto = resolved_model, auto_pick
        else:
            attempt_model, attempt_auto = image_routing.resolve_model(
                stack, attempt_provider, "", nsfw=nsfw, auto_pick=True
            )
        # A suggestive image Grok refused may be mature on Civitai (owner only).
        attempt_nsfw = nsfw or (attempt_idx > 0 and job.fallback_mature
                                and attempt_provider not in _CENSORED_IMAGE_PROVIDERS)
        tried = 0
        network_retry_used = False
        while tried < (tries if attempt_idx == 0 else 1):
            try:
                async with gate.session(
                    "image", attempt_model or "civitai-auto", attempt_provider
                ):
                    result = await _dispatch_image_provider(
                        attempt_provider,
                        settings=settings,
                        stack=stack,
                        job=job,
                        resolved_model=attempt_model,
                        artifacts_dir=artifacts_dir,
                        nsfw=attempt_nsfw,
                        auto_pick=attempt_auto,
                    )
            except Exception as e:
                errors.append(f"{attempt_provider}: {e}")
                log.warning(
                    "image.attempt_exception", provider=attempt_provider, error=str(e)[:200]
                )
                _route_ledger(settings, provider=attempt_provider, model=attempt_model,
                              ok=False, error=str(e), primary=provider, nsfw=attempt_nsfw,
                              prompt=job.prompt)
                if _is_connection_error(e) and not network_retry_used:
                    network_retry_used = True  # never reached the backend: free retry
                    await asyncio.sleep(1.0)
                    continue
                break
            tried += 1
            failed = _image_result_failed(result)
            _route_ledger(settings, provider=attempt_provider, model=attempt_model,
                          ok=not failed, error=result if failed else "", primary=provider,
                          nsfw=attempt_nsfw, prompt=job.prompt)
            if not failed:
                if attempt_idx > 0:
                    log.info("image.fallback_succeeded", primary=provider, used=attempt_provider,
                             reason=errors[0][:200] if errors else "")
                if result.startswith(("Refused:", "Pending:")):
                    return ImageResult.from_text(result, provider=attempt_provider,
                                                 model=attempt_model)
                label = attempt_model or "auto"
                why = f" because {errors[0][:160]}" if errors else ""
                notes = f"; fell back from {provider}{why}" if attempt_idx > 0 else ""
                if tried > 1:
                    notes += f"; succeeded on attempt {tried}"
                out = ImageResult.from_text(result, provider=attempt_provider,
                                            model=attempt_model)
                return out.but(
                    text=f"{result} (backend: {attempt_provider}/{label}{nsfw_tag}{notes})")
            errors.append(f"{attempt_provider}: {result[:300]}")
            log.warning("image.attempt_failed", provider=attempt_provider, attempt=tried,
                        error=result[:200])

    summary = errors[-1] if errors else (result or "unknown error")
    tried_note = f" ({tries} attempts)" if tries > 1 else ""
    return ImageResult(status=FAILED, text=f"Image generation failed{tried_note}: {summary}",
                       provider=provider)


def _route_ledger(settings: Settings, *, provider: str, model: str | None, ok: bool,
                  error: str, primary: str, nsfw: bool, prompt: str) -> None:
    """One line per image attempt on any backend (Civitai also writes its own
    detailed line), so a silent fallback is visible afterwards."""
    if provider == "civitai" and ok:
        return  # the Civitai pipeline already logged this success in detail
    try:
        from ophelia.providers.civitai.pipeline import write_ledger

        write_ledger(settings, {
            "ts": time.time(), "kind": "attempt", "provider": provider,
            "model": model or "auto", "ok": ok, "error": (error or "")[:300],
            "primary": primary, "fell_back": provider != primary, "nsfw": nsfw,
            "prompt": (prompt or "")[:200],
        })
    except Exception as e:
        log.debug("image.route_ledger_failed", error=str(e))


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return True
    text = str(exc).lower()
    return any(n in text for n in ("connection", "temporarily unavailable", "name resolution"))


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


async def _xai_image_edit(
    settings: Settings,
    stack: ProviderStack,
    prompt: str,
    image: str,
    model: str,
    artifacts_dir: Path,
) -> str:
    """Grok image edit (POST /v1/images/edits, JSON). The OpenAI SDK's
    images.edit sends multipart, which xAI rejects, so this is raw HTTP."""
    backend = stack.backend("image")
    assert isinstance(backend, XAIBackend)
    src = (image or "").strip()
    if not src.startswith(("http://", "https://", "data:")):
        p = Path(src).expanduser()
        if not p.is_file():
            return f"Image edit source not found: {src}"
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
            p.suffix.lower(), "image/png"
        )
        src = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    token = await backend.bearer_fresh()
    base = settings.xai_base_url.rstrip("/")
    body = {"model": model, "prompt": prompt, "image": {"url": src, "type": "image_url"}}
    async with httpx.AsyncClient(timeout=180.0) as http:
        r = await http.post(
            f"{base}/images/edits", headers={"Authorization": f"Bearer {token}"}, json=body
        )
    if r.status_code >= 400:
        return f"Grok image edit failed HTTP {r.status_code}: {r.text[:300]}"
    data = (r.json() or {}).get("data") or [{}]
    url = data[0].get("url")
    b64 = data[0].get("b64_json")
    if not url and not b64:
        return f"Grok image edit returned no image: {str(r.json())[:300]}"
    saved = await _save_image_artifact(
        artifacts_dir=artifacts_dir, prompt=prompt, url=url, b64=b64, model=model
    )
    if not saved:
        return "Grok image edit failed to save."
    log.info("image.saved", provider="xai", model=model, path=str(saved), edit=True)
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


async def _civitai_image(
    settings: Settings,
    job: ImageJob,
    model: str,
    artifacts_dir: Path,
    *,
    nsfw: bool,
) -> str:
    """Civitai orchestration: see ophelia.providers.civitai.pipeline."""
    from ophelia.providers.civitai import GenerationRequest, run_generation

    return await run_generation(
        settings,
        GenerationRequest(
            prompt=job.prompt, aspect_ratio=job.aspect_ratio, nsfw=nsfw, model=model or "",
            quality=job.quality, loras=job.loras, negative_prompt=job.negative_prompt,
            image=job.image, strength=job.strength, count=job.count,
            deliver_to=job.deliver_to, pose_image=job.pose_image,
            style_terms=list(job.style_terms),
        ),
        artifacts_dir,
    )


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
