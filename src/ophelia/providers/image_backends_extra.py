"""Image backends Ophelia doesn't use day to day.

She runs on Grok (SFW), Civitai (explicit) and Pollinations (free last
resort); those stay in media.py. These are kept for setups that point
OPHELIA_PROVIDER_IMAGE / _NSFW at them (the setup wizard still offers
them), out of the main code path: OpenAI, Ollama, A1111, ComfyUI, fal,
Replicate and ModelsLab.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path

import httpx
import structlog

from ophelia.config import Settings
from ophelia.providers.media import (
    _dims,
    _first_image_url,
    _save_image_artifact,
    _save_image_bytes,
)
from ophelia.providers.router import ProviderStack

log = structlog.get_logger()


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
