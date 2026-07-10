"""Civitai site API + orchestration helpers for image generation.

Site API (https://civitai.com/api/v1) — browse checkpoints/LoRAs, AIR URNs,
trainedWords (trigger words), baseModel.

Orchestration API (https://orchestration.civitai.com) — submit imageGen
workflows (txt2img createImage, img2img createVariant), upload blobs for
local source images, mature-content gating.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog

from ophelia.config import Settings

log = structlog.get_logger()

SITE_API = "https://civitai.com/api/v1"
_AIR_RE = re.compile(
    r"^urn:air:(?P<eco>[^:]+):(?P<kind>[^:]+):(?P<source>[^:]+):(?P<id>.+)$",
    re.IGNORECASE,
)

# baseModel strings from the site API → orchestration ecosystem + prompt style.
_BASE_MODEL_MAP: list[tuple[tuple[str, ...], str, str]] = [
    (("illustrious", "noobai", "noob"), "sdxl", "danbooru"),
    (("pony",), "sdxl", "danbooru"),
    (("sdxl", "sd xl", "sd_xl"), "sdxl", "mixed"),
    (("sd 1.5", "sd1.5", "sd 1", "sd1", "sd15"), "sd1", "danbooru"),
    (("flux", "flux.1", "flux1"), "flux1", "natural"),
    (("sd 3", "sd3", "stable diffusion 3"), "sd3", "natural"),
    (("qwen",), "qwen", "natural"),
]

_DEFAULT_NEGATIVES: dict[str, str] = {
    "sd1": (
        "worst quality, low quality, normal quality, blurry, jpeg artifacts, "
        "bad anatomy, bad hands, missing fingers, extra digits, watermark, text"
    ),
    "sdxl": (
        "worst quality, low quality, blurry, jpeg artifacts, bad anatomy, "
        "bad hands, watermark, text, logo"
    ),
    "flux1": "",
    "sd3": "",
    "qwen": "",
}

_PROMPT_STYLE_HINTS: dict[str, str] = {
    "danbooru": (
        "Use Danbooru-style tags (comma-separated), not long prose. "
        "Lead with quality tags (masterpiece, best quality, absurdres), then "
        "subject tags (1girl, solo, …), then clothing/pose/scene. "
        "ALWAYS include every LoRA/checkpoint trigger word from trainedWords. "
        "Put junk in negative_prompt — many anime checkpoints expect a strong negative."
    ),
    "mixed": (
        "SDXL accepts tag-style OR short natural language. Quality tags still help "
        "(masterpiece, best quality). Include LoRA trigger words verbatim. "
        "Use a solid negative_prompt (worst quality, low quality, blurry)."
    ),
    "natural": (
        "Write a clear natural-language prompt (Flux/Qwen-style). "
        "Do NOT spam Danbooru tags. Include LoRA trigger phrases if any. "
        "Negative prompts are optional / often unused on Flux."
    ),
}


@dataclass
class CivitaiResource:
    """One searchable Civitai resource (checkpoint or LoRA version)."""

    name: str
    type: str  # Checkpoint | LORA | ...
    model_id: int
    version_id: int
    version_name: str
    air: str
    base_model: str
    trained_words: list[str] = field(default_factory=list)
    nsfw: bool = False
    download_count: int = 0
    thumbs_up: int = 0
    description: str = ""

    @property
    def ecosystem(self) -> str:
        return ecosystem_from_air_or_base(self.air, self.base_model)

    @property
    def prompt_style(self) -> str:
        return prompt_style_for(self.ecosystem, self.base_model)

    def to_agent_block(self) -> str:
        triggers = ", ".join(self.trained_words) if self.trained_words else "(none listed)"
        style = self.prompt_style
        hint = _PROMPT_STYLE_HINTS.get(style, _PROMPT_STYLE_HINTS["mixed"])
        neg = default_negative_for(self.ecosystem)
        lines = [
            f"- {self.name} [{self.type}] v{self.version_name}",
            f"  air: {self.air}",
            f"  baseModel: {self.base_model or '?'} → ecosystem={self.ecosystem}, "
            f"prompt_style={style}",
            f"  trigger_words: {triggers}",
            f"  stats: downloads={self.download_count}, 👍={self.thumbs_up}"
            + (" | NSFW-tagged" if self.nsfw else ""),
            f"  prompt tip: {hint}",
        ]
        if neg:
            lines.append(f"  suggested_negative: {neg}")
        if self.description:
            lines.append(f"  about: {self.description[:160]}")
        return "\n".join(lines)


def ecosystem_from_air_or_base(air: str = "", base_model: str = "") -> str:
    m = _AIR_RE.match((air or "").strip())
    if m:
        eco = m.group("eco").lower()
        # Normalize site AIR ecosystems to orchestration names.
        if eco in ("sd15", "sd1.5"):
            return "sd1"
        if eco in ("flux", "flux.1"):
            return "flux1"
        return eco
    low = (base_model or "").strip().lower()
    for keys, eco, _style in _BASE_MODEL_MAP:
        if any(k in low for k in keys):
            return eco
    return "sdxl"  # safest community default for NSFW anime/realistic gens


def prompt_style_for(ecosystem: str, base_model: str = "") -> str:
    low = (base_model or "").strip().lower()
    for keys, eco, style in _BASE_MODEL_MAP:
        if any(k in low for k in keys):
            return style
    eco = (ecosystem or "").lower()
    if eco in ("sd1", "sd15"):
        return "danbooru"
    if eco in ("flux1", "flux", "sd3", "qwen"):
        return "natural"
    if eco == "sdxl":
        return "mixed"
    return "mixed"


def default_negative_for(ecosystem: str) -> str:
    return _DEFAULT_NEGATIVES.get((ecosystem or "").lower(), _DEFAULT_NEGATIVES["sdxl"])


def _auth_headers(settings: Settings) -> dict[str, str]:
    key = (settings.civitai_api_key or "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _site_headers(settings: Settings) -> dict[str, str]:
    # Site API accepts the same API token when present; public search works without.
    return _auth_headers(settings)


async def search_models(
    settings: Settings,
    query: str,
    *,
    types: str = "Checkpoint",
    limit: int = 5,
    nsfw: bool | None = True,
    sort: str = "Most Downloaded",
) -> list[CivitaiResource]:
    """Search Civitai models; return best version of each hit with AIR + triggers."""
    q = (query or "").strip()
    if not q:
        return []
    params: dict[str, Any] = {
        "limit": max(1, min(int(limit), 20)),
        "query": q,
        "types": types,
        "sort": sort,
    }
    # nsfw=true → include mature; false → SFW only; None → API default
    if nsfw is True:
        params["nsfw"] = "true"
    elif nsfw is False:
        params["nsfw"] = "false"

    url = f"{SITE_API}/models"
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(url, headers=_site_headers(settings), params=params)
        if r.status_code >= 400:
            log.warning("civitai.search_failed", status=r.status_code, body=r.text[:200])
            raise RuntimeError(f"Civitai search failed HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()

    out: list[CivitaiResource] = []
    for item in data.get("items") or []:
        versions = item.get("modelVersions") or []
        if not versions:
            continue
        # Prefer a version that already has an air / canGenerate when present.
        ver = versions[0]
        for v in versions:
            if v.get("air") or v.get("canGenerate"):
                ver = v
                break
        air = (ver.get("air") or "").strip()
        if not air:
            # Synthesize from ids when the list endpoint omits air.
            mid = int(item.get("id") or 0)
            vid = int(ver.get("id") or 0)
            eco = ecosystem_from_air_or_base("", ver.get("baseModel") or "")
            kind = "lora" if (item.get("type") or "").upper() == "LORA" else "checkpoint"
            if mid and vid:
                air = f"urn:air:{eco}:{kind}:civitai:{mid}@{vid}"
        if not air:
            continue
        stats = item.get("stats") or {}
        desc = (item.get("description") or "")
        # Strip crude HTML
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        out.append(
            CivitaiResource(
                name=str(item.get("name") or "unknown"),
                type=str(item.get("type") or types),
                model_id=int(item.get("id") or 0),
                version_id=int(ver.get("id") or 0),
                version_name=str(ver.get("name") or ""),
                air=air,
                base_model=str(ver.get("baseModel") or ""),
                trained_words=list(ver.get("trainedWords") or []),
                nsfw=bool(item.get("nsfw")),
                download_count=int(stats.get("downloadCount") or 0),
                thumbs_up=int(stats.get("thumbsUpCount") or 0),
                description=desc,
            )
        )
    return out


async def get_version(settings: Settings, version_id: int) -> CivitaiResource | None:
    """Fetch a single model version (canonical air + trainedWords)."""
    url = f"{SITE_API}/model-versions/{int(version_id)}"
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.get(url, headers=_site_headers(settings))
        if r.status_code >= 400:
            return None
        ver = r.json()
    model = ver.get("model") or {}
    return CivitaiResource(
        name=str(model.get("name") or ver.get("name") or "unknown"),
        type=str(model.get("type") or "Checkpoint"),
        model_id=int(model.get("id") or 0),
        version_id=int(ver.get("id") or 0),
        version_name=str(ver.get("name") or ""),
        air=str(ver.get("air") or ""),
        base_model=str(ver.get("baseModel") or ""),
        trained_words=list(ver.get("trainedWords") or []),
        nsfw=bool(model.get("nsfw")),
        download_count=int((ver.get("stats") or {}).get("downloadCount") or 0),
        thumbs_up=int((ver.get("stats") or {}).get("thumbsUpCount") or 0),
    )


def parse_loras(raw: Any) -> dict[str, float]:
    """Accept dict, JSON string, or 'urn:strength,urn:strength' / 'urn,urn'."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        out: dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                out[str(k)] = 0.8
        return out
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return parse_loras(data)
        except json.JSONDecodeError:
            pass
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            urn, _, strength = part.partition("|")
        elif "=" in part:
            urn, _, strength = part.partition("=")
        elif part.startswith("urn:air:"):
            urn, strength = part, "0.8"
        else:
            urn, strength = part, "0.8"
        try:
            out[urn.strip()] = float(strength.strip())
        except ValueError:
            out[urn.strip()] = 0.8
    return out


def ensure_triggers_in_prompt(prompt: str, triggers: list[str]) -> str:
    """Prepend any missing trigger words so LoRAs/checkpoints actually fire."""
    text = (prompt or "").strip()
    low = text.lower()
    missing = [t for t in triggers if t and t.lower() not in low]
    if not missing:
        return text
    return ", ".join(missing) + (", " + text if text else "")


async def upload_local_image(settings: Settings, path: Path) -> str:
    """Upload a local image to Civitai blob storage; return a usable URL."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    base = settings.civitai_base_url.rstrip("/")
    data = p.read_bytes()
    suffix = p.suffix.lower() or ".png"
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
    headers = {
        "Authorization": f"Bearer {settings.civitai_api_key}",
        "Content-Type": mime,
    }
    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(
            f"{base}/v2/consumer/blobs",
            headers=headers,
            content=data,
            params={"fileName": p.name},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Civitai blob upload failed HTTP {r.status_code}: {r.text[:300]}"
            )
        body = r.json() if r.content else {}
    url = (
        body.get("url")
        or body.get("signedUrl")
        or body.get("downloadUrl")
        or body.get("blobUrl")
    )
    if not url and body.get("id"):
        # Some responses only return an id — fetch blob metadata.
        blob_id = body["id"]
        async with httpx.AsyncClient(timeout=30.0) as http:
            g = await http.get(
                f"{base}/v2/consumer/blobs/{blob_id}",
                headers={"Authorization": f"Bearer {settings.civitai_api_key}"},
            )
            if g.status_code < 400:
                meta = g.json()
                url = meta.get("url") or meta.get("signedUrl")
    if not url:
        raise RuntimeError(f"Civitai blob upload returned no URL: {body}")
    return str(url)


async def resolve_image_url(settings: Settings, image: str) -> str:
    """http(s) URL passthrough, or upload a local path / data URL."""
    raw = (image or "").strip()
    if not raw:
        raise ValueError("empty image")
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("data:"):
        return raw
    p = Path(raw).expanduser()
    if p.is_file():
        return await upload_local_image(settings, p)
    raise FileNotFoundError(
        f"Image source not found (need http(s) URL or local path): {raw}"
    )


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
    """Build an orchestration imageGen input for txt2img or img2img."""
    eco = (ecosystem or ecosystem_from_air_or_base(model_air)).lower()
    operation = "createVariant" if image_url else "createImage"

    # Flux without a specific checkpoint URN → simple flux engine path.
    if not model_air or model_air.lower() in ("flux", "auto"):
        if eco in ("flux1", "flux") or not model_air or model_air.lower() == "flux":
            step: dict[str, Any] = {
                "engine": "flux",
                "prompt": prompt,
                "width": width,
                "height": height,
            }
            if image_url:
                # Flux engine may not support createVariant — prefer sdcpp flux1.
                step = {
                    "engine": "sdcpp",
                    "ecosystem": "flux1",
                    "operation": operation,
                    "prompt": prompt,
                    "width": width,
                    "height": height,
                    "image": image_url,
                    "strength": float(strength),
                }
            return step

    # Checkpoint / LoRA path via sdcpp.
    if eco in ("flux1", "flux"):
        orch_eco = "flux1"
    elif eco in ("sd15", "sd1.5"):
        orch_eco = "sd1"
    else:
        orch_eco = eco or "sdxl"

    step = {
        "engine": "sdcpp",
        "ecosystem": orch_eco,
        "operation": operation,
        "prompt": prompt,
        "width": width,
        "height": height,
    }
    if model_air and model_air.lower() not in ("flux", "auto"):
        step["model"] = model_air
    if negative_prompt:
        step["negativePrompt"] = negative_prompt
    elif orch_eco in ("sd1", "sdxl"):
        step["negativePrompt"] = default_negative_for(orch_eco)
    if loras:
        step["loras"] = loras
    if image_url:
        step["image"] = image_url
        step["strength"] = float(strength)
    if cfg_scale is not None:
        step["cfgScale"] = float(cfg_scale)
    elif orch_eco in ("sd1", "sdxl"):
        step["cfgScale"] = 7.0
    if steps is not None:
        step["steps"] = int(steps)
    elif orch_eco in ("sd1", "sdxl"):
        step["steps"] = 25
    return step


async def pick_best_resources(
    settings: Settings,
    intent: str,
    *,
    nsfw: bool = True,
    want_lora: bool = True,
) -> tuple[CivitaiResource | None, list[CivitaiResource], str]:
    """Heuristic: pick a strong checkpoint (+ optional LoRAs) for an intent.

    Returns (checkpoint, loras, rationale).
    """
    intent = (intent or "").strip()
    # Bias search queries by vibe.
    low = intent.lower()
    if any(w in low for w in ("anime", "waifu", "1girl", "manga", "illustrious", "pony")):
        ck_query = "illustrious anime"
    elif any(w in low for w in ("realistic", "photo", "portrait", "photograph")):
        ck_query = "realistic vision sdxl"
    elif any(w in low for w in ("furry", "anthro")):
        ck_query = "pony diffusion"
    else:
        ck_query = intent[:80] or "sdxl"

    checkpoints = await search_models(
        settings, ck_query, types="Checkpoint", limit=5, nsfw=nsfw
    )
    checkpoint = checkpoints[0] if checkpoints else None

    loras: list[CivitaiResource] = []
    if want_lora and intent:
        # Narrow LoRA search to a short subject phrase.
        lora_q = intent[:60]
        try:
            loras = await search_models(
                settings, lora_q, types="LORA", limit=3, nsfw=nsfw
            )
            # Prefer LoRAs matching the checkpoint ecosystem.
            if checkpoint:
                eco = checkpoint.ecosystem
                matched = [x for x in loras if x.ecosystem == eco]
                loras = matched or loras
                loras = loras[:2]
        except Exception as e:
            log.debug("civitai.lora_search_failed", error=str(e))

    rationale_parts = []
    if checkpoint:
        rationale_parts.append(
            f"checkpoint={checkpoint.name} ({checkpoint.air}, style={checkpoint.prompt_style})"
        )
    if loras:
        rationale_parts.append(
            "loras=" + ", ".join(f"{x.name}@{x.air}" for x in loras)
        )
    return checkpoint, loras, "; ".join(rationale_parts) or "no matches"


def format_search_results(resources: list[CivitaiResource], *, header: str) -> str:
    if not resources:
        return f"{header}\n(no results)"
    blocks = [header, ""]
    for r in resources:
        blocks.append(r.to_agent_block())
        blocks.append("")
    blocks.append(
        "Next: call generate_image with model=<air>, loras={air: strength}, "
        "negative_prompt=..., and a prompt that includes the trigger_words "
        "using the prompt_style tip above. For img2img pass image=<path or url>."
    )
    return "\n".join(blocks).strip()
