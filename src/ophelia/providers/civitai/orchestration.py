"""Civitai orchestration API: build imageGen inputs per model, submit,
poll, read outputs, upload source images.

Request shapes follow developer.civitai.com/orchestration/recipes (sdxl,
anima, zimage, qwen, flux1, flux2). Unknown fields are rejected with a 400,
so each ecosystem only gets the fields its schema lists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import structlog

from ophelia.config import Settings
from ophelia.providers.civitai.catalog import (
    RECIPES,
    SDXL_BASE_AIR,
    WAI_ILLUSTRIOUS_AIR,
    Recipe,
    recipe_for_air,
    sampling_for,
)

log = structlog.get_logger()

_USER_AGENT = "Ophelia/1.0 (+https://github.com/Shadow4-18/Ophelia)"
TERMINAL_OK = ("succeeded",)
TERMINAL_BAD = ("failed", "expired", "canceled", "cancelled")


def build_input(
    recipe: Recipe,
    *,
    prompt: str,
    width: int,
    height: int,
    steps: int,
    negative: str = "",
    loras: dict[str, float] | None = None,
    quantity: int = 1,
    seed: int | None = None,
    image_url: str | None = None,
    strength: float = 0.7,
    embeddings: list[str] | None = None,
) -> dict[str, Any]:
    """The imageGen ``input`` object for one request. ``embeddings`` are
    textual-inversion AIRs (SDXL/SD1 only), referenced by name in the
    negative prompt."""
    variant = bool(image_url)
    op = "createVariant" if variant else "createImage"
    q = max(1, min(int(quantity), recipe.max_quantity))

    # Flux.2 text-to-image has its own engine; its img2img runs on sdcpp.
    if recipe.engine == "flux2" and not variant:
        step: dict[str, Any] = {
            "engine": "flux2", "model": recipe.model, "operation": op,
            "prompt": prompt, "width": width, "height": height, "quantity": q,
            "cfgScale": recipe.cfg, "steps": steps, **recipe.extra,
        }
        if negative and recipe.negative:
            step["negativePrompt"] = negative
        if loras:
            step["loras"] = loras
        if seed is not None:
            step["seed"] = seed
        return step

    step = {"engine": "sdcpp", "ecosystem": recipe.ecosystem, "operation": op}
    if recipe.engine == "flux2":
        step.update(recipe.extra)  # sdcpp flux2Klein variant: modelVersion, no model
    elif recipe.model_field and recipe.model:  # anima takes no model field at all
        step[recipe.model_field] = recipe.model
        step.update(recipe.extra)
    step["prompt"] = prompt
    # Qwen infers the size from the source image on variants.
    if not (variant and recipe.ecosystem == "qwen"):
        step["width"] = width
        step["height"] = height
    if negative and recipe.negative:
        step["negativePrompt"] = negative
    step["cfgScale"] = recipe.cfg
    step["steps"] = steps
    step["quantity"] = q
    sampler, schedule = sampling_for(recipe)
    if sampler:
        step["sampleMethod"] = sampler
        step["schedule"] = schedule
    if embeddings and recipe.ecosystem in ("sdxl", "sd1") and recipe.engine != "flux2":
        step["embeddings"] = list(embeddings)
    if loras:
        step["loras"] = loras
    if seed is not None:
        step["seed"] = seed
    if variant:
        step["image"] = image_url
        step["strength"] = float(strength)
    return step


def build_step_input(
    *,
    prompt: str,
    width: int,
    height: int,
    model_air: str = "",
    ecosystem: str = "",
    negative_prompt: str = "",
    loras: dict[str, float] | None = None,
    image_url: str | None = None,
    strength: float = 0.7,
    cfg_scale: float | None = None,
    steps: int | None = None,
) -> dict[str, Any]:
    """Old single-call API: build an input for a checkpoint AIR (or a default
    SDXL checkpoint). Never emits the retired engine:'flux' path."""
    air = (model_air or "").strip()
    if not air.startswith("urn:air:"):
        air = WAI_ILLUSTRIOUS_AIR if "illustrious" in (ecosystem or "").lower() else SDXL_BASE_AIR
    recipe = recipe_for_air(air)
    if cfg_scale is not None:
        from dataclasses import replace

        recipe = replace(recipe, cfg=float(cfg_scale))
    return build_input(
        recipe, prompt=prompt, width=width, height=height,
        steps=steps if steps is not None else recipe.steps,
        negative=negative_prompt, loras=loras, image_url=image_url, strength=strength,
    )


# --- reading workflows ------------------------------------------------------


def status_of(wf: dict) -> str:
    return str(wf.get("status") or wf.get("state") or "").lower()


def output_images(wf: dict) -> list[dict]:
    """Every image entry under steps[].output.images[] (url may be missing
    while a blob is still being prepared)."""
    out: list[dict] = []
    for step in wf.get("steps") or []:
        output = (step or {}).get("output") or {}
        for img in output.get("images") or []:
            if isinstance(img, dict):
                out.append(img)
    return out


def failure_reason(wf: dict) -> str:
    """Why a workflow failed, as specifically as the response says."""
    parts: list[str] = []
    for step in wf.get("steps") or []:
        step = step or {}
        for key in ("reason", "error", "message", "failureReason"):
            val = step.get(key)
            if val:
                parts.append(str(val))
        output = step.get("output") or {}
        for img in output.get("images") or []:
            if isinstance(img, dict) and img.get("blockedReason"):
                parts.append(f"blocked: {img['blockedReason']}")
    for key in ("reason", "error", "message"):
        if wf.get(key):
            parts.append(str(wf[key]))
    return "; ".join(dict.fromkeys(parts)) or str(wf)[:300]


def cost_of(wf: dict) -> Any:
    return wf.get("cost") or (wf.get("transactions") or None)


# --- HTTP -------------------------------------------------------------------


class CivitaiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.civitai_base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {(settings.civitai_api_key or '').strip()}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }

    async def submit(self, body: dict, *, wait: int, mature: bool) -> httpx.Response:
        params: dict[str, str] = {"wait": str(int(wait))}
        if mature:
            params["allowMatureContent"] = "true"
        async with httpx.AsyncClient(timeout=wait + 30.0, follow_redirects=True) as http:
            return await http.post(
                f"{self.base}/v2/consumer/workflows", headers=self.headers,
                params=params, json=body,
            )

    async def get(self, workflow_id: str, *, wait: int = 0) -> dict | None:
        async with httpx.AsyncClient(timeout=wait + 30.0, follow_redirects=True) as http:
            r = await http.get(
                f"{self.base}/v2/consumer/workflows/{workflow_id}",
                headers=self.headers, params={"wait": str(int(wait))},
            )
        if r.status_code >= 400:
            log.debug("civitai.poll_http", status=r.status_code, body=r.text[:200])
            return None
        return r.json()

    async def download(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
            dl = await http.get(
                url, headers={"Authorization": f"Bearer {(self.settings.civitai_api_key or '').strip()}"}
            )
            if dl.status_code >= 400:
                dl = await http.get(url)
            return dl


async def check_key(settings: Settings) -> tuple[int, str]:
    """One free authenticated call (list her latest workflow): (HTTP status,
    body excerpt). 200 means Civitai accepts the key."""
    client = CivitaiClient(settings)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        r = await http.get(f"{client.base}/v2/consumer/workflows", headers=client.headers,
                           params={"take": "1"})
    return r.status_code, r.text[:400]


def describe_key_source(settings: Settings) -> list[str]:
    """Where the key she's using comes from, without printing it."""
    import os

    from ophelia.setup.env_io import env_path

    key = (settings.civitai_api_key or "")
    lines = []
    if not key.strip():
        return ["No CIVITAI_API_KEY is set."]
    shown = f"{key.strip()[:4]}…{key.strip()[-4:]}" if len(key.strip()) > 10 else "(short)"
    lines.append(f"Key in use: {shown} ({len(key.strip())} characters)")
    if key != key.strip():
        lines.append("  It has spaces or line breaks around it (now trimmed when sent).")
    if os.environ.get("CIVITAI_API_KEY"):
        lines.append("  It comes from the shell environment (export CIVITAI_API_KEY=...), which "
                     "overrides ~/.ophelia/.env. Remove that export (check ~/.bashrc) or "
                     "update it too.")
    else:
        path = env_path()
        count = 0
        if path.is_file():
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                left = raw.strip().removeprefix("export ").split("=", 1)[0].strip()
                if left == "CIVITAI_API_KEY":
                    count += 1
        lines.append(f"  From {path}" + (f" ({count} lines set it; the LAST one is used)"
                                         if count > 1 else ""))
    return lines


async def upload_local_image(settings: Settings, path: Path) -> str:
    """Upload a local image to Civitai blob storage; return a usable URL."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    base = settings.civitai_base_url.rstrip("/")
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(p.suffix.lower(), "application/octet-stream")
    headers = {
        "Authorization": f"Bearer {(settings.civitai_api_key or '').strip()}",
        "Content-Type": mime,
        "User-Agent": _USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
        r = await http.post(
            f"{base}/v2/consumer/blobs", headers=headers, content=p.read_bytes(),
            params={"fileName": p.name},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Civitai blob upload failed HTTP {r.status_code}: {r.text[:300]}")
        body = r.json() if r.content else {}
        url = body.get("url") or body.get("signedUrl") or body.get("downloadUrl") \
            or body.get("blobUrl")
        if not url and body.get("id"):
            g = await http.get(
                f"{base}/v2/consumer/blobs/{body['id']}",
                headers={k: v for k, v in headers.items() if k != "Content-Type"},
            )
            if g.status_code < 400:
                meta = g.json()
                url = meta.get("url") or meta.get("signedUrl")
    if not url:
        raise RuntimeError(f"Civitai blob upload returned no URL: {body}")
    return str(url)


async def resolve_image_url(settings: Settings, image: str) -> str:
    """http(s)/data URL passthrough, or upload a local path."""
    raw = (image or "").strip()
    if not raw:
        raise ValueError("empty image")
    if raw.startswith(("http://", "https://", "data:")):
        return raw
    p = Path(raw).expanduser()
    if p.is_file():
        return await upload_local_image(settings, p)
    raise FileNotFoundError(f"Image source not found (need http(s) URL or local path): {raw}")


__all__ = [
    "RECIPES", "CivitaiClient", "build_input", "build_step_input", "cost_of",
    "failure_reason", "output_images", "resolve_image_url", "status_of",
    "upload_local_image",
]
