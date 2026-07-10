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


def _dims(aspect_ratio: str, *, clamp_max: int | None = None) -> tuple[int, int]:
    """Pixel dimensions for an aspect ratio, ~1MP, multiples of 8."""
    ar = (aspect_ratio or "1:1").strip()
    w, h = _ASPECT_DIMS.get(ar, (1024, 1024))
    if ar not in _ASPECT_DIMS:
        try:
            aw, ah = ar.split(":")
            r = float(aw) / float(ah)
            base = 1024
            if r >= 1:
                w, h = base, int(round(base / r))
            else:
                h, w = base, int(round(base / r))
        except Exception:
            w, h = 1024, 1024
    w = max(256, (w // 8) * 8)
    h = max(256, (h // 8) * 8)
    if clamp_max:
        w = min(w, clamp_max)
        h = min(h, clamp_max)
    return w, h


async def _save_image_bytes(
    content: bytes, artifacts_dir: Path, provider: str, prompt: str
) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    slug = abs(hash(prompt)) % 10**8
    out = artifacts_dir / f"{provider}_{stamp}_{slug}.png"
    out.write_bytes(content)
    return out


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

    provider = stack.image_provider_for(nsfw=nsfw)
    # Defensive: never let an explicit prompt reach a censored backend, even if
    # the user mis-configured OPHELIA_IMAGE_NSFW_PROVIDER to point at one.
    if nsfw and provider in _CENSORED_IMAGE_PROVIDERS:
        return (
            f"Refused: explicit image would route to {provider}, which refuses NSFW and "
            "may flag the account. Set OPHELIA_IMAGE_NSFW_PROVIDER to an uncensored "
            "backend (pollinations/a1111/comfyui/fal/replicate/civitai/modelslab/ollama)."
        )

    resolved_model = (model or "").strip() or stack.image_model_for(provider, nsfw=nsfw)
    gate = get_model_gate()
    nsfw_tag = " [nsfw]" if nsfw else ""

    async with gate.session("image", resolved_model, provider):
        if provider in ("xai-oauth", "xai"):
            result = await _xai_image(
                settings, stack, prompt, aspect_ratio, resolved_model, artifacts_dir
            )
        elif provider == "openai":
            result = await _openai_image(
                settings, stack, prompt, resolved_model, artifacts_dir
            )
        elif provider == "ollama":
            result = await _ollama_image(
                settings, prompt, resolved_model, artifacts_dir
            )
        elif provider == "pollinations":
            result = await _pollinations_image(
                settings, prompt, aspect_ratio, resolved_model, artifacts_dir, nsfw=nsfw
            )
        elif provider == "a1111":
            result = await _a1111_image(
                settings, prompt, aspect_ratio, resolved_model, artifacts_dir
            )
        elif provider == "comfyui":
            result = await _comfyui_image(
                settings, prompt, aspect_ratio, resolved_model, artifacts_dir
            )
        elif provider == "fal":
            result = await _fal_image(
                settings, prompt, aspect_ratio, resolved_model, artifacts_dir
            )
        elif provider == "replicate":
            result = await _replicate_image(
                settings, prompt, aspect_ratio, resolved_model, artifacts_dir
            )
        elif provider == "civitai":
            result = await _civitai_image(
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
        elif provider == "modelslab":
            result = await _modelslab_image(
                settings, prompt, aspect_ratio, resolved_model, artifacts_dir, nsfw=nsfw
            )
        else:
            raise RuntimeError(
                f"Image generation not configured for provider '{provider}'. "
                "Set OPHELIA_PROVIDER_IMAGE to one of: xai-oauth, xai, openai, ollama, "
                "pollinations, a1111, comfyui, fal, replicate, civitai, modelslab."
            )
    # Annotate the result with which backend actually ran, so the agent
    # (and the owner) can see whether it was Grok, Pollinations, etc. —
    # prevents the agent from claiming "I used Grok" when the image role
    # silently fell through to Pollinations because xAI wasn't configured.
    if result.startswith("Image saved to"):
        result = f"{result} (backend: {provider}/{resolved_model}{nsfw_tag})"
    return result


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
    auto_pick: bool = False,
) -> str:
    """Civitai orchestration — txt2img (createImage) or img2img (createVariant).

    Model may be an AIR URN, 'flux', or empty (falls back to CIVITAI_IMAGE_MODEL /
    auto-pick). LoRAs are {air: strength}. Local image paths are uploaded as blobs.
    """
    from ophelia.providers import civitai as civ

    w, h = _dims(aspect_ratio)
    base = settings.civitai_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.civitai_api_key}",
        "Content-Type": "application/json",
    }

    model_air = (model or "").strip()
    explicit_flux = model_air.lower() == "flux"
    # Prefer explicit call arg, then env default. Fix the old bug where a URN
    # in OPHELIA_IMAGE_NSFW_MODEL was wrongly sent as engine=.
    if not model_air or model_air.lower() in ("auto",):
        env_model = (settings.civitai_image_model or "").strip()
        if env_model:
            model_air = env_model

    lora_map = civ.parse_loras(loras)
    triggers: list[str] = []
    pick_note = ""
    ecosystem = civ.ecosystem_from_air_or_base(model_air)

    # Auto-pick: explicit auto_pick, or nothing configured yet (not bare "flux").
    should_pick = bool(auto_pick) or (
        not explicit_flux
        and not model_air
        and not (settings.civitai_image_model or "").strip()
    )
    if should_pick:
        try:
            ck, picked_loras, rationale = await civ.pick_best_resources(
                settings, prompt, nsfw=nsfw, want_lora=not lora_map
            )
            if ck:
                model_air = ck.air
                ecosystem = ck.ecosystem
                triggers.extend(ck.trained_words)
                pick_note = rationale
            for lr in picked_loras:
                lora_map.setdefault(lr.air, 0.8)
                triggers.extend(lr.trained_words)
        except Exception as e:
            log.warning("civitai.auto_pick_failed", error=str(e))
            pick_note = f"auto_pick_failed: {e}"
            if not model_air:
                model_air = "flux"
                ecosystem = "flux1"

    # If we have an AIR but no triggers yet, optionally enrich from site API.
    if model_air.startswith("urn:air:") and "@" in model_air and not triggers:
        try:
            vid_s = model_air.rsplit("@", 1)[-1].split("+")[0]
            if vid_s.isdigit():
                ver = await civ.get_version(settings, int(vid_s))
                if ver:
                    triggers.extend(ver.trained_words)
                    ecosystem = ver.ecosystem or ecosystem
        except Exception as e:
            log.debug("civitai.version_enrich_failed", error=str(e))

    final_prompt = civ.ensure_triggers_in_prompt(prompt, triggers)
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
    body = {"steps": [{"$type": "imageGen", "input": step_input}]}
    mature = "true" if (nsfw or settings.image_nsfw_allowed) else "false"
    log.info(
        "civitai.submit",
        operation=step_input.get("operation"),
        ecosystem=step_input.get("ecosystem"),
        engine=step_input.get("engine"),
        model=step_input.get("model") or step_input.get("engine"),
        loras=len(lora_map),
        img2img=bool(image_url),
        pick=pick_note or None,
    )

    async with httpx.AsyncClient(timeout=120.0) as http:
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
        dl = await http.get(url)
        if dl.status_code >= 400:
            dl = await http.get(url, headers={"Authorization": f"Bearer {settings.civitai_api_key}"})
        dl.raise_for_status()
        saved = await _save_image_bytes(dl.content, artifacts_dir, "civitai", prompt)

    used = step_input.get("model") or step_input.get("engine") or model_air or "flux"
    meta = f"Image saved to {saved}"
    extras = [f"civitai {step_input.get('operation', 'createImage')}"]
    extras.append(f"model={used}")
    if lora_map:
        extras.append(f"loras={len(lora_map)}")
    if pick_note:
        extras.append(f"picked: {pick_note}")
    if triggers:
        extras.append("triggers_injected=" + ", ".join(triggers[:6]))
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
