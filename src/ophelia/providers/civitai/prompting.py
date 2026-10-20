"""Prompt shaping per model: style detection, quality/rating tags, negatives.

Ophelia writes the prompt; this only adds what each model needs to do its
best and nothing else: no character trigger words, no LoRA triggers.
"""

from __future__ import annotations

import re

from ophelia.providers.civitai.catalog import Recipe

# Explicit style → recipe rules for auto mode (first match wins). Keywords
# match whole words only, so "anthropic" isn't "anthro".
STYLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("pony", ("pony", "score_9", "score_8_up", "furry", "anthro")),
    (
        "illustrious",
        ("anime", "waifu", "1girl", "1boy", "manga", "illustrious", "danbooru", "2d",
         "vtuber", "hentai", "chibi"),
    ),
    (
        "realistic",
        ("photo", "photos", "photograph", "photoreal", "photorealistic", "realistic",
         "raw photo", "real life", "irl", "dslr"),
    ),
]


def _has_word(low: str, word: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", low))


def detect_style_with_reason(intent: str) -> tuple[str, str]:
    """(style, keyword that decided it); style "sdxl" means no keyword matched."""
    low = (intent or "").lower()
    for style, words in STYLE_RULES:
        for w in words:
            if _has_word(low, w):
                return style, w
    return "sdxl", ""


def detect_style(intent: str) -> str:
    return detect_style_with_reason(intent)[0]


def _prefix_missing(text: str, tags: list[str]) -> str:
    low = text.lower()
    missing = [t for t in tags if t.lower() not in low]
    return (", ".join(missing) + ", " + text) if missing else text


def _append_missing(text: str, tags: list[str]) -> str:
    low = text.lower()
    missing = [t for t in tags if t.lower() not in low]
    return (text + ", " + ", ".join(missing)) if missing else text


def format_prompt(recipe: Recipe, prompt: str, *, nsfw: bool) -> str:
    """The prompt as this model likes it. Light touch: WAI's author warns
    that piling on quality tags makes images blurrier."""
    text = (prompt or "").strip()
    low = text.lower()
    style = recipe.prompt_style
    if style == "pony":
        if "score_9" not in low:
            text = "score_9, score_8_up, score_7_up, " + text
        if "source_" not in low:
            src = "source_furry" if _has_word(low, "furry") or _has_word(low, "anthro") \
                else "source_anime"
            text = _append_missing(text, [src])
        if nsfw and "rating_" not in low:
            text = _append_missing(text, ["rating_explicit"])
    elif style == "danbooru":
        tags = ["masterpiece", "best quality"]
        if recipe.family == "illustrious":
            tags.append("amazing quality")  # WAI's recommended trio
        text = _prefix_missing(text, tags)
        if nsfw and not (_has_word(low, "nsfw") or _has_word(low, "explicit")):
            text = _append_missing(text, ["nsfw"])
    if len(text) > recipe.prompt_limit:
        text = text[: recipe.prompt_limit].rsplit(",", 1)[0]
    return text


_NEGATIVES: dict[str, str] = {
    "illustrious": "bad quality, worst quality, worst detail, sketch, watermark, signature",
    "pony": (
        "score_6, score_5, score_4, worst quality, low quality, bad anatomy, bad hands, "
        "watermark, signature"
    ),
    "sdxl": (
        "worst quality, low quality, blurry, jpeg artifacts, bad anatomy, bad hands, "
        "watermark, text, logo"
    ),
    "realistic": (
        "worst quality, low quality, blurry, deformed, bad anatomy, bad hands, "
        "watermark, text, cartoon, anime, 3d render"
    ),
    "sd1": (
        "worst quality, low quality, normal quality, blurry, jpeg artifacts, "
        "bad anatomy, bad hands, missing fingers, extra digits, watermark, text"
    ),
    "anima": "worst quality, low quality, blurry, bad anatomy, deformed hands",
    "zimage": "blurry, low quality, deformed hands, watermark",
    "qwen": "blurry, low detail",
}

_UNCENSOR = "censored, mosaic censoring, bar censor"


def negative_for(recipe: Recipe, *, nsfw: bool) -> str:
    if not recipe.negative:
        return ""
    if recipe.family == "sdxl" and recipe.style == "realistic":
        neg = _NEGATIVES["realistic"]
    else:
        neg = _NEGATIVES.get(recipe.family, "")
    if nsfw and recipe.family in ("illustrious", "pony", "anima") and neg:
        neg = f"{neg}, {_UNCENSOR}"
    return neg


_GUIDES: dict[str, str] = {
    "danbooru": (
        "Danbooru tags, comma-separated: subject (1girl, solo), body/hair/eyes, "
        "outfit or nudity, pose, expression, camera (from above, close-up), "
        "setting, lighting. Quality and nsfw tags are added for you."
    ),
    "pony": (
        "Danbooru-style tags. score_*/source_*/rating_* tags are added for you; "
        "add source_realistic or source_cartoon yourself if that's the look."
    ),
    "mixed": (
        "Short natural description or tags both work: subject, body, outfit, pose, "
        "setting, lighting, camera (e.g. 35mm photo, shallow depth of field)."
    ),
    "natural": (
        "One or two plain sentences, like describing a photo to someone. No tag spam."
    ),
}


def prompt_guide(recipe: Recipe) -> str:
    return _GUIDES.get(recipe.prompt_style, _GUIDES["mixed"])


# --- legacy helpers (kept for callers/tests written against the old API) ----

_PROMPT_STYLE_HINTS = {
    "danbooru": _GUIDES["danbooru"],
    "mixed": _GUIDES["mixed"],
    "natural": _GUIDES["natural"],
}


def default_negative_for(ecosystem: str) -> str:
    eco = (ecosystem or "").lower()
    if eco in ("flux1", "sd3", "qwen", "flux2"):
        return ""
    return _NEGATIVES.get(eco, _NEGATIVES["sdxl"])


def maybe_quality_prefix(prompt: str, style: str) -> str:
    """Quality tags for danbooru-style families — never character triggers."""
    text = (prompt or "").strip()
    if style == "pony":
        return text if "score_9" in text.lower() else "score_9, score_8_up, score_7_up, " + text
    if style not in ("illustrious", "anime"):
        return text
    return _prefix_missing(text, ["masterpiece", "best quality"])


def ensure_triggers_in_prompt(prompt: str, triggers: list[str], *, mode: str = "off") -> str:
    """Optionally merge trigger words into a prompt (default: never)."""
    text = (prompt or "").strip()
    if mode in ("off", "quality") or not triggers:
        return text
    low = text.lower()
    safe: list[str] = []
    for t in triggers:
        t = (t or "").strip()
        if not t or t.lower() in low or len(t) > 40 or t.count(",") >= 2:
            continue
        if any(bad in t.lower() for bad in ("pussy", "nipple", "penis", "sex", "nude", "naked")):
            continue
        safe.append(t)
    if not safe:
        return text
    if mode == "prepend":
        return ", ".join(safe) + ", " + text
    return text + ", " + ", ".join(safe)
