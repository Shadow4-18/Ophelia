"""Concept LoRAs auto mode adds on its own.

Base checkpoints draw some acts and effects badly (x-ray, ahegao, specific
sex positions, tentacles). When a prompt names one of those and she gave
no LoRAs herself, this finds the most downloaded LoRA for that concept in
the checkpoint's family (Illustrious, Pony or plain SDXL) and adds it at a
moderate weight, with its trigger word. Searches are cached for a week so
a repeat costs nothing; a failed search just means no LoRA.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import structlog

from ophelia.providers.civitai import feedback, site
from ophelia.providers.civitai.air import base_family
from ophelia.providers.civitai.catalog import Recipe

log = structlog.get_logger()

# (prompt pattern, search query, the LoRA name must match, name must not match)
CONCEPTS: list[tuple[str, str, str, str]] = [
    (r"x-?ray|cross-?section|internal view", "x-ray", r"x-?ray|cross", r"glasses|phone"),
    (r"ahegao", "ahegao", r"ahegao", ""),
    (r"tentacles?", "tentacles", r"tentacle", ""),
    (r"cowgirl|girl on top", "cowgirl", r"cowgirl|girl on top|riding", ""),
    (r"missionary", "missionary", r"missionary", ""),
    (r"doggy ?style|from behind sex", "doggystyle", r"doggy", ""),
    (r"mating press", "mating press", r"mating press", ""),
    (r"blowjob|fellatio|deepthroat", "blowjob", r"blowjob|fellatio|deepthroat", r"x-?ray"),
    (r"paizuri|titjob|titfuck", "paizuri", r"paizuri|titjob|titfuck", ""),
    (r"bukkake|cum on \w+|cum covered|covered in cum|cum on face", "cum",
     r"cum|bukkake|facial", ""),
    (r"lactation|lactating|breast milk", "lactation", r"lactat|milk", ""),
    (r"shibari|rope bondage|bound with rope", "shibari", r"shibari|rope", ""),
    (r"glory ?hole", "glory hole", r"glory ?hole", ""),
    (r"futanari|futa", "futanari", r"futa", ""),
    (r"spitroast|double penetration|\bdp\b", "double penetration",
     r"double penetration|spitroast|\bdp\b", ""),
    (r"squirt|squirting", "squirting", r"squirt", ""),
    (r"bondage|restrained|tied up", "bondage", r"bondage|restrain|tied", ""),
]

_FAMILY_BASE = {"illustrious": "Illustrious", "pony": "Pony", "sdxl": "SDXL 1.0"}
_MIN_DOWNLOADS = 2000
_CACHE_DAYS = 7
_MAX_AUTO = 2
_WEIGHT = {1: 0.75, 2: 0.6}


def _cache_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "civitai_lora_cache.json"


def _load(settings: Any) -> dict[str, Any]:
    try:
        data = json.loads(_cache_path(settings).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _store(settings: Any, data: dict[str, Any]) -> None:
    try:
        path = _cache_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug("civitai.lora_cache_failed", error=str(e))


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "civitai_auto_loras", True))


def concepts_in(prompt: str) -> list[tuple[str, str, str]]:
    """(query, name must match, name must not match) for concepts in the prompt."""
    low = (prompt or "").lower()
    out = []
    for pat, query, must, must_not in CONCEPTS:
        if re.search(rf"(?<![a-z0-9])(?:{pat})(?![a-z0-9])", low):
            out.append((query, must, must_not))
    return out


def trigger_tags(words: list[str], limit: int = 3) -> list[str]:
    """The first trained word as a few short tags (no <lora:...> syntax)."""
    if not words:
        return []
    raw = re.sub(r"<lora:[^>]*>", "", str(words[0]))
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    return [t for t in tags if len(t) <= 40][:limit]


async def _find(settings: Any, query: str, must: str, must_not: str,
                family: str) -> dict[str, Any] | None:
    base = _FAMILY_BASE[family]
    cache = _load(settings)
    key = f"{family}|{query}"
    hit = cache.get(key)
    if isinstance(hit, dict) and time.time() - float(hit.get("ts", 0)) < _CACHE_DAYS * 86400:
        return hit.get("lora")
    found: dict[str, Any] | None = None
    try:
        results = await site.search_models(settings, query, types="LORA", limit=10,
                                           nsfw=True, base_model=base)
    except Exception as e:
        log.info("civitai.auto_lora_search_failed", query=query, error=str(e)[:200])
        return None  # not cached: try again next time
    for r in sorted(results, key=lambda r: r.download_count, reverse=True):
        name = r.name.lower()
        if base_family(r.base_model, r.air) != family or r.download_count < _MIN_DOWNLOADS:
            continue
        if not re.search(must, name) or (must_not and re.search(must_not, name)):
            continue
        found = {"air": r.air, "name": r.name, "triggers": trigger_tags(r.trained_words)}
        break
    cache[key] = {"ts": time.time(), "lora": found}
    _store(settings, cache)
    return found


async def pick_loras(settings: Any, recipe: Recipe, prompt: str) -> tuple[dict[str, float],
                                                                         list[str], list[str]]:
    """({air: weight}, trigger tags to add, notes) for concepts in the prompt."""
    if not (enabled(settings) and recipe.loras and recipe.family in _FAMILY_BASE):
        return {}, [], []
    found: list[dict[str, Any]] = []
    for query, must, must_not in concepts_in(prompt)[:_MAX_AUTO]:
        lora = await _find(settings, query, must, must_not, recipe.family)
        if lora and lora["air"] not in {f["air"] for f in found}:
            found.append(lora)
    if not found:
        return {}, [], []
    scored = feedback.scores(settings)
    base_w = _WEIGHT.get(len(found), 0.6)
    loras: dict[str, float] = {}
    triggers: list[str] = []
    notes: list[str] = []
    for lora in found:
        mult = feedback.multiplier(scored.get(lora["air"]))
        if mult < 0.6:
            notes.append(f"skipped LoRA {lora['name']} (he didn't like it before)")
            continue
        loras[lora["air"]] = base_w
        triggers.extend(lora.get("triggers") or [])
        notes.append(f"auto LoRA {lora['name']} @{base_w}")
    return loras, triggers, notes
