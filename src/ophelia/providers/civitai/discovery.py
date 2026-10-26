"""Checkpoints auto mode finds on Civitai by itself.

The catalog's pools are a hand-picked start. Once a week this asks the
Civitai site API for the month's highest rated checkpoints of each family
(Illustrious, Pony, SDXL) and adds the best few to each style's rotation
at a lower weight. Checkpoints she pinned herself (found with
search_civitai_models) join too once the owner has reacted well to them.
Anything Civitai won't run gets benched by the pipeline like any other
pool member, and his reactions move all of them up or down.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog

from ophelia.providers.civitai import feedback, site
from ophelia.providers.civitai.air import base_family, is_air, model_id, sanitize_air
from ophelia.providers.civitai.catalog import RECIPES, Recipe, recipe_for_air

log = structlog.get_logger()

_FAMILIES = ("illustrious", "pony", "sdxl")
_REFRESH_DAYS = 7
_RETRY_HOURS = 6  # after a failed refresh
_PER_STYLE = 4
_MIN_DOWNLOADS = 5000
_DISCOVERED_WEIGHT = 0.7
_FAVORITE_WEIGHT = 1.2
_FAVORITE_MULT = 1.3  # feedback multiplier a pinned checkpoint needs to join
_REALISM = re.compile(r"real|photo|lifelike|raw\b|cinematic|skin", re.IGNORECASE)
_ANIME = re.compile(r"anime|hentai|2d|manga|toon|illustr", re.IGNORECASE)
_SKIP = re.compile(r"furry|anthro|feral|pokemon|\bvideo\b", re.IGNORECASE)
_task: asyncio.Task | None = None


def enabled(settings: Any) -> bool:
    # ``is True``: MagicMock / SimpleNamespace test settings stay offline.
    return getattr(settings, "civitai_discover", False) is True


def _path(settings: Any) -> Path:
    return Path(settings.data_dir) / "civitai_discovered.json"


def _load(settings: Any) -> dict[str, Any]:
    try:
        data = json.loads(_path(settings).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def style_for(family: str, name: str, *, nsfw: bool = True) -> str:
    """Which rotation a checkpoint belongs in ("" = none of them)."""
    if _SKIP.search(name):
        return ""  # niche: only when she pins it
    if family == "pony":
        return "realistic" if _REALISM.search(name) else "pony"
    if family == "illustrious":
        return "realistic" if _REALISM.search(name) and not _ANIME.search(name) else "anime"
    if family == "sdxl":
        # Plain SDXL merges: only explicit-capable photoreal ones are worth it.
        return "realistic" if nsfw and not _ANIME.search(name) else ""
    return ""


def _curated_ids() -> set[int]:
    return {mid for r in RECIPES.values() if r.model and (mid := model_id(r.model))}


def _recipe(entry: dict[str, Any], *, weight: float, note: str, key: str) -> Recipe | None:
    air = sanitize_air(str(entry.get("air", "")))
    if not is_air(air):
        return None
    base = recipe_for_air(air, str(entry.get("base_model", "")))
    if base.key != "custom":
        return None  # already a catalog recipe
    style = str(entry.get("style", ""))
    return replace(
        base, key=key, label=str(entry.get("name") or air)[:60], style=style or base.style,
        pool=style, weight=weight, notes=note,
        pony_source="photo, realistic" if base.family == "pony" and style == "realistic"
        else "",
    )


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "model"


async def refresh(settings: Any) -> int:
    """Ask Civitai for this month's best checkpoints. Returns how many were kept."""
    curated = _curated_ids()
    found: list[dict[str, Any]] = []
    per_style: dict[str, int] = {}
    # One browse of everything, no base filter: the list endpoint drops
    # versions that don't match a filter, which would hide that a model has
    # moved on to another base (its Pony version is then a legacy one).
    try:
        results = await site.search_models(settings, "", types="Checkpoint", limit=100,
                                           nsfw=True, sort="Highest Rated", period="Month")
    except Exception as e:
        log.info("civitai.discover_failed", error=str(e)[:200])
        data = _load(settings)
        data["failed_at"] = time.time()
        _write(settings, data)
        return 0
    for r in results:
        # By baseModel alone: the AIR's "sdxl" can be made up for unknown bases.
        family = base_family(r.base_model)
        if family not in _FAMILIES or r.model_id in curated:
            continue
        if r.download_count < _MIN_DOWNLOADS:
            continue
        if r.availability and r.availability.lower() != "public":
            continue  # early access costs Buzz to use
        if r.newest_base_model and r.newest_base_model != r.base_model:
            continue  # the model moved on to another base; this version is legacy
        style = style_for(family, r.name, nsfw=r.nsfw)
        if not style or per_style.get(style, 0) >= _PER_STYLE:
            continue
        per_style[style] = per_style.get(style, 0) + 1
        found.append({"air": r.air, "name": r.name, "base_model": r.base_model,
                      "style": style, "downloads": r.download_count})
    _write(settings, {"ts": time.time(), "models": found})
    log.info("civitai.discovered", count=len(found),
             models=[f"{m['style']}:{m['name']}" for m in found])
    return len(found)


def _write(settings: Any, data: dict[str, Any]) -> None:
    try:
        path = _path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug("civitai.discover_save_failed", error=str(e))


def stale(settings: Any) -> bool:
    data = _load(settings)
    now = time.time()
    if now - float(data.get("failed_at", 0) or 0) < _RETRY_HOURS * 3600:
        return False
    return now - float(data.get("ts", 0) or 0) > _REFRESH_DAYS * 86400


def refresh_in_background(settings: Any) -> None:
    """Start a refresh if the list is old; the current pick doesn't wait."""
    global _task
    if not enabled(settings) or not stale(settings):
        return
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.get_running_loop().create_task(refresh(settings))
    except RuntimeError:
        pass


def discovered(settings: Any, style: str) -> list[Recipe]:
    if not enabled(settings):
        return []
    out = []
    for m in _load(settings).get("models") or []:
        if m.get("style") != style:
            continue
        r = _recipe(m, weight=_DISCOVERED_WEIGHT, key=f"found-{_slug(str(m.get('name')))}",
                    note="Found on Civitai's highest rated this month.")
        if r:
            out.append(r)
    return out


def favorites(settings: Any, style: str) -> list[Recipe]:
    """Checkpoints she pinned herself that he reacted well to."""
    from ophelia.providers.civitai.pipeline import ledger_path

    scored = feedback.scores(settings)
    if not scored:
        return []
    try:
        lines = ledger_path(settings).read_text(encoding="utf-8").splitlines()[-2000:]
    except Exception:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        air = str(rec.get("model") or "")
        if rec.get("recipe") == "custom" and is_air(air):
            seen[air] = rec
    out = []
    for air, rec in seen.items():
        if feedback.multiplier(scored.get(air)) < _FAVORITE_MULT:
            continue
        base_model = str(rec.get("base_model") or "")
        family = base_family(base_model, air)
        name = str(rec.get("label") or air)
        if style_for(family, name) != style:
            continue
        r = _recipe({"air": air, "name": name, "base_model": base_model, "style": style},
                    weight=_FAVORITE_WEIGHT, key=f"fav-{model_id(air) or _slug(name)}",
                    note="One you pinned that he liked.")
        if r:
            out.append(r)
    return out


def extra_pool(settings: Any, style: str) -> list[Recipe]:
    """Discovered + favorite checkpoints for a style's rotation."""
    out: list[Recipe] = []
    seen: set[str] = set()
    for r in favorites(settings, style) + discovered(settings, style):
        if r.model not in seen:
            seen.add(r.model)
            out.append(r)
    return out
