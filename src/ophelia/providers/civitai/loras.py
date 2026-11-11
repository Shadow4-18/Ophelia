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

from ophelia.fileio import atomic_write_json
import structlog

from ophelia.providers.civitai import feedback, site
from ophelia.providers.civitai.air import base_family
from ophelia.providers.civitai.catalog import Recipe

log = structlog.get_logger()

# (prompt pattern, search query, the LoRA name must match, name must not
# match, group). One LoRA per group: two positions in one picture fight.
CONCEPTS: list[tuple[str, str, str, str, str]] = [
    (r"x-?ray|cross-?section|internal view", "x-ray", r"x-?ray|cross", r"glasses|phone",
     "effect"),
    (r"ahegao", "ahegao", r"ahegao", "", "face"),
    (r"tentacles?", "tentacles", r"tentacle", "", "partner"),
    (r"reverse cowgirl", "reverse cowgirl", r"reverse cowgirl", "", "position"),
    (r"(?<!reverse )cowgirl|girl on top", "cowgirl", r"cowgirl|girl on top|riding",
     r"reverse", "position"),
    (r"missionary", "missionary", r"missionary", "", "position"),
    (r"doggy ?style|from behind sex|sex from behind", "doggystyle", r"doggy|from behind",
     "", "position"),
    (r"mating press", "mating press", r"mating press", "", "position"),
    (r"prone bone", "prone bone", r"prone", "", "position"),
    (r"full nelson", "full nelson", r"full nelson", "", "position"),
    (r"spooning|spoon position", "spooning", r"spoon", "", "position"),
    (r"standing sex|suspended congress|lifted sex|against (?:the )?wall",
     "standing sex", r"standing|suspended|lift|wall", "", "position"),
    (r"69|sixty-?nine", "69 position", r"69|sixty", "", "position"),
    (r"blowjob|fellatio|deepthroat", "blowjob", r"blowjob|fellatio|deepthroat", r"x-?ray",
     "oral"),
    (r"cunnilingus|eating (?:her )?(?:out|pussy)", "cunnilingus", r"cunnilingus|licking",
     "", "oral"),
    (r"paizuri|titjob|titfuck", "paizuri", r"paizuri|titjob|titfuck", "", "act"),
    (r"footjob", "footjob", r"footjob", "", "act"),
    (r"handjob", "handjob", r"handjob", "", "act"),
    (r"fingering", "fingering", r"finger", "", "act"),
    (r"bukkake|cum on \w+|cum covered|covered in cum|cum on face|facial(?! expression)", "cum",
     r"cum|bukkake|facial", "", "fluid"),
    (r"creampie|cum inside|cum dripping", "creampie", r"creampie|cum", "", "fluid"),
    (r"lactation|lactating|breast milk", "lactation", r"lactat|milk", "", "fluid"),
    (r"squirt|squirting", "squirting", r"squirt", "", "fluid"),
    (r"shibari|rope bondage|bound with rope", "shibari", r"shibari|rope", "", "restraint"),
    (r"bondage|restrained|tied up", "bondage", r"bondage|restrain|tied", "", "restraint"),
    (r"glory ?hole", "glory hole", r"glory ?hole", "", "setting"),
    (r"futanari|futa", "futanari", r"futa", "", "body"),
    (r"spitroast|double penetration|\bdp\b", "double penetration",
     r"double penetration|spitroast|\bdp\b", "", "position"),
    (r"after sex|afterglow", "after sex", r"after ?sex", "", "effect"),
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
        atomic_write_json(path, data)
    except Exception as e:
        log.debug("civitai.lora_cache_failed", error=str(e))


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "civitai_auto_loras", True))


def concepts_in(prompt: str) -> list[tuple[str, str, str]]:
    """(query, name must match, name must not match) for concepts in the prompt."""
    return [(q, m, n) for q, m, n, _ in concepts_with_groups(prompt)]


def concepts_with_groups(prompt: str) -> list[tuple[str, str, str, str]]:
    """(query, must, must_not, group), one per group, in prompt order."""
    low = (prompt or "").lower()
    hits = []
    for pat, query, must, must_not, group in CONCEPTS:
        m = re.search(rf"(?<![a-z0-9])(?:{pat})(?![a-z0-9])", low)
        if m:
            hits.append((m.start(), query, must, must_not, group))
    out, groups = [], set()
    for _, query, must, must_not, group in sorted(hits):
        if group in groups:
            continue  # the first one she named wins its group
        groups.add(group)
        out.append((query, must, must_not, group))
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
    def rank(r) -> float:
        # Downloads, nudged by how well liked it is (thumbs per download).
        liked = min(1.0, (r.thumbs_up + 1) / max(1, r.download_count) * 20)
        return r.download_count * (0.6 + 0.4 * liked)

    for r in sorted(results, key=rank, reverse=True):
        name = r.name.lower()
        if base_family(r.base_model, r.air) != family or r.download_count < _MIN_DOWNLOADS:
            continue
        if not re.search(must, name) or (must_not and re.search(must_not, name)):
            continue
        option = {"air": r.air, "name": r.name, "triggers": trigger_tags(r.trained_words),
                  "category": r.category, "rec": r.rec_weight}
        if found is None:
            found = dict(option, options=[])
        found["options"].append(option)
        if len(found["options"]) >= 3:
            break
    cache[key] = {"ts": time.time(), "lora": found}
    _store(settings, cache)
    return found


def _best_option(found: dict, scored: dict, pairs: dict,
                 others: list[str]) -> tuple[dict | None, str]:
    """Of the top few LoRAs for a concept, the one he liked most, alone and
    together with this checkpoint and the other LoRAs (rank order breaks ties)."""
    options = found.get("options") or [found]
    best, best_score = None, 0.0
    for i, opt in enumerate(options):
        single = feedback.multiplier(scored.get(opt["air"]))
        if single < 0.6:
            continue
        score = single * feedback.pair_multiplier(pairs, opt["air"], others) * (1 - 0.1 * i)
        if score > best_score:
            best, best_score = opt, score
    if best is None:
        return None, f"skipped LoRA {options[0]['name']} (he didn't like it before)"
    if best["air"] != options[0]["air"]:
        return best, f"{best['name']} over {options[0]['name']} (went better with this before)"
    return best, ""


async def concept_picks(settings: Any, recipe: Recipe, prompt: str,
                        others: tuple[str, ...] = ()) -> tuple[list, list[str]]:
    """(LoraPicks, notes) for concepts the prompt names, one per group. Among
    a concept's top LoRAs, the one that went best with this checkpoint and
    ``others`` (her LoRAs) in images he reacted to wins."""
    from ophelia.providers.civitai.stack import LoraPick

    if not (enabled(settings) and recipe.loras and recipe.family in _FAMILY_BASE):
        return [], []
    scored = feedback.scores(settings)
    pairs = feedback.pair_scores(settings)
    picks: list[LoraPick] = []
    notes: list[str] = []
    for query, must, must_not, group in concepts_with_groups(prompt)[:_MAX_AUTO]:
        found = await _find(settings, query, must, must_not, recipe.family)
        if not found:
            continue
        lora, why = _best_option(found, scored, pairs,
                                 [recipe.model] + [p.air for p in picks] + list(others))
        if why:
            notes.append(why)
        if not lora or lora["air"] in {p.air for p in picks}:
            continue
        picks.append(LoraPick(
            air=lora["air"], name=lora["name"], category=lora.get("category") or "concept",
            rec=lora.get("rec"), source="concept", group=group, query=query,
            triggers=list(lora.get("triggers") or []),
        ))
    return picks, notes


async def pick_loras(settings: Any, recipe: Recipe, prompt: str) -> tuple[dict[str, float],
                                                                         list[str], list[str]]:
    """({air: weight}, trigger tags to add, notes) for concepts in the prompt."""
    picks, notes = await concept_picks(settings, recipe, prompt)
    if not picks:
        return {}, [], notes
    base_w = _WEIGHT.get(len(picks), 0.6)
    loras: dict[str, float] = {}
    triggers: list[str] = []
    for p in picks:
        loras[p.air] = base_w
        triggers.extend(p.triggers)
        notes.append(f"auto LoRA {p.name} @{base_w}")
    return loras, triggers, notes


# --- style LoRAs (from a style reference) ------------------------------------

# Style words worth a LoRA search; generic ones ("high contrast", "soft
# lighting") match thousands of unrelated LoRAs.
_STYLEISH = re.compile(
    r"\b(style|anime|manga|watercolou?r|oil|painting|painted|ink|sketch|lineart|line art|"
    r"cel|pixel|comic|retro|vintage|\d0s|ukiyo|impasto|pastel|gouache|chibi|noir|"
    r"cyberpunk|vaporwave|synthwave|gothic|art nouveau|art deco|pop art|graffiti|"
    r"airbrush|crayon|charcoal|pencil|woodblock|lowpoly|low poly|claymation|"
    r"polaroid|film|analog|vhs|cinematic|ghibli|disney|pixar)\b", re.I)
_STOP = {"style", "art", "with", "and", "the", "colors", "colours", "look", "shading"}


def style_queries(terms: list[str], limit: int = 3) -> list[tuple[str, str]]:
    """(search query, name must match) for the most distinctive style terms."""
    out: list[tuple[str, str]] = []
    for term in terms:
        t = (term or "").strip().lower()
        if not t or not _STYLEISH.search(t):
            continue
        words = [w for w in re.findall(r"[a-z0-9]+", t) if len(w) >= 3 and w not in _STOP]
        if not words:
            continue
        out.append((t, "|".join(re.escape(w) for w in words)))
        if len(out) >= limit:
            break
    return out


async def style_picks(settings: Any, recipe: Recipe, terms: list[str]) -> tuple[list, list[str]]:
    """([LoraPick], notes): the most downloaded LoRA in the model's family for
    the reference's most distinctive style term."""
    from ophelia.providers.civitai.stack import LoraPick

    if not (enabled(settings) and recipe.loras and recipe.family in _FAMILY_BASE):
        return [], []
    scored = feedback.scores(settings)
    for query, must in style_queries(terms):
        lora = await _find(settings, query, must, "", recipe.family)
        if not lora or feedback.multiplier(scored.get(lora["air"])) < 0.6:
            continue
        return ([LoraPick(air=lora["air"], name=lora["name"], category="style",
                          rec=min(0.7, lora.get("rec") or 0.6), source="style",
                          triggers=list(lora.get("triggers") or []))],
                [f"style LoRA {lora['name']} (for '{query}')"])
    return [], []


async def pick_style_lora(settings: Any, recipe: Recipe,
                          terms: list[str]) -> tuple[dict[str, float], list[str], list[str]]:
    """({air: weight}, trigger tags, notes) for a style reference's terms."""
    picks, notes = await style_picks(settings, recipe, terms)
    if not picks:
        return {}, [], []
    p = picks[0]
    return {p.air: 0.6}, list(p.triggers), [f"style LoRA {p.name} @0.6 (for "
                                            + notes[0].split("(for ", 1)[1]]
