"""Civitai site API (civitai.com/api/v1): search checkpoints/LoRAs, look up
a version's baseModel, trigger words and AIR."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ophelia.config import Settings
from ophelia.providers.civitai.air import ecosystem_from_air_or_base, prompt_style_for
from ophelia.providers.civitai.prompting import _PROMPT_STYLE_HINTS, default_negative_for

log = structlog.get_logger()

SITE_API = "https://civitai.com/api/v1"
_USER_AGENT = "Ophelia/1.0 (+https://github.com/Shadow4-18/Ophelia; local-first agent)"


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
    availability: str = ""  # Public | EarlyAccess | ... ("" = not reported)
    newest_base_model: str = ""  # baseModel of the model's newest version

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


def _auth_headers(settings: Settings) -> dict[str, str]:
    key = (settings.civitai_api_key or "").strip()
    h = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if key:
        # Same personal API key works for Site REST + Orchestration.
        h["Authorization"] = f"Bearer {key}"
    return h


def _site_headers(settings: Settings) -> dict[str, str]:
    return _auth_headers(settings)


async def _site_get(http: httpx.AsyncClient, url: str, settings: Settings,
                    params: dict[str, Any] | None = None) -> httpx.Response:
    """GET on the civitai.com REST API. Some valid keys get 403 there when
    sent as a Bearer header but work as ?token= (the orchestration API takes
    the header fine); public endpoints also work with no key at all. Try
    those in turn so a model search or version lookup doesn't fail on how
    the key is sent."""
    headers = _site_headers(settings)
    r = await http.get(url, headers=headers, params=params)
    key = (settings.civitai_api_key or "").strip()
    if r.status_code not in (401, 403) or not key:
        return r
    plain = {k: v for k, v in headers.items() if k != "Authorization"}
    retry = await http.get(url, headers=plain, params={**(params or {}), "token": key})
    if retry.status_code < 400:
        log.info("civitai.site_auth", via="token", first_status=r.status_code)
        return retry
    anon = await http.get(url, headers=plain, params=params)
    if anon.status_code < 400:
        log.info("civitai.site_auth", via="none", first_status=r.status_code)
    return anon if anon.status_code < 400 else r


async def search_models(
    settings: Settings,
    query: str,
    *,
    types: str = "Checkpoint",
    limit: int = 5,
    nsfw: bool | None = True,
    sort: str = "Most Downloaded",
    base_model: str | None = None,
    period: str | None = None,
) -> list[CivitaiResource]:
    """Search Civitai models; return best version of each hit with AIR + triggers.

    Uses the same CIVITAI_API_KEY as orchestration (Bearer). follow_redirects
    handles civitai.com → www / API host moves. Prefer SDXL/Illustrious hits
    for NSFW gens — Flux is filtered out of auto-pick.
    """
    q = (query or "").strip()
    if not q and not base_model and not period:
        return []  # nothing to search for or browse by
    params: dict[str, Any] = {
        "limit": max(1, min(int(limit), 100)),
        "types": types,
        "sort": sort,
    }
    if q:
        params["query"] = q
    if period:
        params["period"] = period  # AllTime | Year | Month | Week | Day
    # nsfw=true → include mature; false → SFW only; None → API default
    if nsfw is True:
        params["nsfw"] = "true"
    elif nsfw is False:
        params["nsfw"] = "false"
    if base_model:
        params["baseModels"] = base_model

    url = f"{SITE_API}/models"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        r = await _site_get(http, url, settings, params)
        if r.status_code >= 400:
            log.warning(
                "civitai.search_failed",
                status=r.status_code,
                body=r.text[:200],
                url=url,  # not r.url: it may carry ?token=
            )
            raise RuntimeError(
                f"Civitai search failed HTTP {r.status_code}: {r.text[:200]}"
            )
        data = r.json()

    out: list[CivitaiResource] = []
    for item in data.get("items") or []:
        versions = item.get("modelVersions") or []
        if not versions:
            continue
        # One model can have versions for several bases (Big Love: Pony, SDXL,
        # Qwen): with a base filter, take that base's newest version.
        newest_base = str(versions[0].get("baseModel") or "")
        if base_model:
            versions = [v for v in versions if v.get("baseModel") == base_model] or versions
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
                availability=str(ver.get("availability") or ""),
                newest_base_model=newest_base,
            )
        )
    return out


async def get_version(settings: Settings, version_id: int) -> CivitaiResource | None:
    """Fetch a single model version (canonical air + trainedWords)."""
    url = f"{SITE_API}/model-versions/{int(version_id)}"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
        r = await _site_get(http, url, settings)
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



async def latest_version_air(
    settings: Settings, model_id: int, base_model: str = ""
) -> str:
    """AIR of a model's newest published version (same baseModel when given),
    used when a stored version was removed from Civitai."""
    url = f"{SITE_API}/models/{int(model_id)}"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
        r = await _site_get(http, url, settings)
        if r.status_code >= 400:
            return ""
        data = r.json()
    for ver in data.get("modelVersions") or []:
        if base_model and (ver.get("baseModel") or "") != base_model:
            continue
        air = (ver.get("air") or "").strip()
        if air:
            return air
        eco = ecosystem_from_air_or_base("", ver.get("baseModel") or "")
        if ver.get("id"):
            return f"urn:air:{eco}:checkpoint:civitai:{int(model_id)}@{int(ver['id'])}"
    return ""


def format_search_results(resources: list[CivitaiResource], *, header: str) -> str:
    if not resources:
        return f"{header}\n(no results)"
    blocks = [header, ""]
    for r in resources:
        blocks.append(r.to_agent_block())
        blocks.append("")
    blocks.append(
        "Next: call generate_image with model=<air> to pin a checkpoint, and "
        "loras={air: strength} only for LoRAs that match the same baseModel family "
        "(Illustrious↔Illustrious, Pony↔Pony, SDXL↔SDXL). Write the prompt yourself — "
        "auto_pick will NOT inject trigger_words. Include short trigger_words in your "
        "prompt only when you intentionally use a character LoRA."
    )
    return "\n".join(blocks).strip()
