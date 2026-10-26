"""Prompt shaping per model: style detection, quality/rating tags, negatives.

Ophelia writes the prompt; this only adds what each model needs to do its
best and nothing else: no character trigger words, no LoRA triggers.
"""

from __future__ import annotations

import re

from ophelia.providers.civitai.catalog import Recipe

# Style words for auto mode, with weights. Every match scores; the style
# with the most points wins (ties: pony, then illustrious, then realistic).
# Keywords match whole words only, so "anthropic" isn't "anthro".
STYLE_WORDS: list[tuple[str, tuple[tuple[str, int], ...]]] = [
    ("pony", (
        ("score_9", 3), ("score_8_up", 3), ("pony", 3), ("furry", 3), ("anthro", 3),
        ("source_pony", 3), ("source_furry", 3), ("feral", 2), ("kemono", 2),
    )),
    ("illustrious", (
        ("anime", 3), ("hentai", 3), ("illustrious", 3), ("anime style", 3),
        ("manga", 2), ("waifu", 2), ("danbooru", 2), ("2d", 2), ("vtuber", 2),
        ("chibi", 2), ("cel shading", 2), ("doujin", 2), ("genshin", 2),
        ("1girl", 1), ("1boy", 1), ("2girls", 1), ("illustration", 1), ("cartoon", 1),
        ("drawing", 1),
    )),
    ("realistic", (
        ("photo", 3), ("photos", 3), ("photograph", 3), ("photoreal", 3),
        ("photorealistic", 3), ("realistic", 3), ("raw photo", 3), ("real life", 3),
        ("irl", 3), ("dslr", 3), ("real woman", 3), ("real person", 3),
        ("selfie", 2), ("amateur", 2), ("instagram", 2), ("onlyfans", 2), ("polaroid", 2),
        ("35mm", 2), ("lifelike", 2), ("realism", 2), ("skin texture", 2),
        ("film grain", 1), ("candid", 1), ("cosplay", 1), ("bokeh", 1),
    )),
]

# The old first-match table, kept for callers that read it.
STYLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    (style, tuple(w for w, _ in words)) for style, words in STYLE_WORDS
]


def _has_word(low: str, word: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", low))


def style_scores(intent: str) -> dict[str, tuple[int, str]]:
    """{style: (points, strongest keyword)} for every style that matched."""
    low = (intent or "").lower()
    out: dict[str, tuple[int, str]] = {}
    for style, words in STYLE_WORDS:
        points, best, best_w = 0, "", -1
        for w, weight in words:
            if _has_word(low, w):
                points += weight
                if weight > best_w:
                    best, best_w = w, weight
        if best:
            out[style] = (points, best)
    return out


def detect_style_with_reason(intent: str) -> tuple[str, str]:
    """(style, keyword that decided it); style "sdxl" means no keyword matched."""
    scores = style_scores(intent)
    if not scores:
        return "sdxl", ""
    order = [s for s, _ in STYLE_WORDS]
    style = max(scores, key=lambda s: (scores[s][0], -order.index(s)))
    return style, scores[style][1]


def detect_style(intent: str) -> str:
    return detect_style_with_reason(intent)[0]


# --- adults only -------------------------------------------------------------

# Explicit images are of adults. An explicit request with any of these words
# is refused, and explicit negatives steer away from them too.
_MINOR_RE = re.compile(
    r"(?<![a-z0-9])(?:loli|lolis|lolicon|shota|shotas|shotacon|child|children|kid|kids|"
    r"underage|under-age|minor|minors|preteen|pre-teen|tween|toddler|infant|"
    r"little girl|little boy|young girl|young boy|elementary school(?:er)?|"
    r"middle school(?:er)?|junior high|grade school(?:er)?|"
    r"(?:[1-9]|1[0-7])\s*(?:yo|y/o|y\.o\.|years? old|year-old))(?![a-z0-9])"
)

_ADULT_NEGATIVE = "child, loli, underage"


def minor_terms(text: str) -> list[str]:
    """Words describing a minor in ``text`` (lowercase, no repeats)."""
    seen: list[str] = []
    for m in _MINOR_RE.finditer((text or "").lower()):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


# --- tag hygiene -------------------------------------------------------------


def split_tags(text: str) -> list[str]:
    """Comma-separated tags, keeping (weighted:1.2, groups) together."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text or "":
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [t.strip() for t in out if t.strip()]


def _tag_key(tag: str) -> str:
    return re.sub(r"\s+", " ", tag.lower().replace("_", " ")).strip()


def tidy_tags(text: str) -> str:
    """Drop empty and repeated tags ("long hair, long_hair", 1girl twice)."""
    seen: set[str] = set()
    keep: list[str] = []
    for tag in split_tags(text):
        key = _tag_key(tag)
        if key not in seen:
            seen.add(key)
            keep.append(re.sub(r"\s+", " ", tag))
    return ", ".join(keep)


_COUNT_TAG = re.compile(r"(?<![a-z0-9])(?:\d\+?(?:girls?|boys?|others?)|solo|multiple "
                        r"(?:girls|boys)|no humans)(?![a-z0-9])")
_FEMALE = ("woman", "girl", "her", "she", "lady", "female", "waifu", "milf", "wife",
           "girlfriend", "ophelia")
_PARTNER = ("1boy", "man", "men", "boy", "boyfriend", "husband", "penis", "sex", "couple",
            "hetero", "fellatio", "blowjob", "handjob", "paizuri", "futa", "futanari",
            "yuri", "2girls", "threesome", "group", "pov hands", "cowgirl position",
            "missionary", "doggystyle", "straddling", "kissing")


def is_solo(text: str) -> bool:
    low = (text or "").lower()
    return not any(_has_word(low, w) for w in _PARTNER)


def ensure_subject(text: str) -> str:
    """Danbooru models anchor on 1girl/solo: add them when the prompt says
    "her" or "a woman" but has no count tag."""
    low = text.lower()
    if _COUNT_TAG.search(low) or not any(_has_word(low, w) for w in _FEMALE):
        return text
    tags = ["1girl"] + (["solo"] if is_solo(low) else [])
    return ", ".join(tags) + ", " + text


# --- negatives that ended up in the prompt -----------------------------------

# Words that only belong in a negative prompt. In the positive prompt they do
# the opposite of what was meant: "no extra limbs" or "bad anatomy" puts
# limbs and bad anatomy into the picture (the model reads the nouns).
_DEFECT = (
    r"(?:extra|missing|fused|bad|poorly drawn|malformed|mutated|mutilated|deformed|"
    r"disfigured|distorted|broken|twisted|duplicate|cloned|floating|disconnected|"
    r"too many|wrong|weird|ugly|blurry|cropped)"
)
_BODY = (
    r"(?:limbs?|arms?|legs?|fingers?|hands?|feet|foot|toes?|digits?|anatomy|proportions|"
    r"body|bodies|face|faces|eyes|heads?|neck|joints?|nipples?|breasts?|torso|limb)"
)
_DEFECT_TAG = re.compile(
    rf"^(?:{_DEFECT}\s+(?:\w+\s+)?{_BODY}|(?:worst|low|lowest|normal|bad)[ _]quality|lowres|"
    r"low res|jpeg artifacts|watermarks?|signature|username|artist name|mutation|mutations|"
    r"mutated|deformed|disfigured|malformed|extra limbs|missing limbs|long neck|"
    r"bad anatomy|bad hands|out of frame|poorly drawn|ugly|duplicate|"
    r"easynegative|negative_hand|negativexl\w*|unaestheticxl\w*)$"
)
# "no X" / "without X" / "avoid X" where X is a defect, not a clothing tag
# like "no bra" or "no panties" (those are wanted).
_NO_PREFIX = re.compile(r"^(?:no|without|avoid|avoiding|never|not|don't|dont|zero)\s+(.+)$")
_INLINE_NO = re.compile(
    rf"\b(?:with\s+)?(?:no|without|avoiding|avoid)\s+((?:{_DEFECT}\s+)?(?:\w+\s+)?"
    rf"(?:{_BODY}|quality|artifacts|watermarks?|text|logos?|blur))\b",
    re.IGNORECASE,
)
_NEG_SECTION = re.compile(r"(?:^|[\s,.;])(?:negative(?:\s+prompt)?|neg)\s*[:=]\s*(.+)$",
                          re.IGNORECASE | re.DOTALL)


def _core(tag: str) -> str:
    """A tag without weights/brackets: "(extra limbs:1.3)" -> "extra limbs"."""
    t = tag.strip().strip("()[]{}").strip()
    t = re.sub(r":\s*[\d.]+$", "", t).strip().strip("()[]{}").strip()
    return re.sub(r"\s+", " ", t.lower().replace("_", " "))


def is_defect(text: str) -> bool:
    return bool(_DEFECT_TAG.match(_core(text)))


def split_negatives(prompt: str) -> tuple[str, list[str]]:
    """(prompt without negative-only words, the words to move to the negative).

    Catches a "Negative prompt: ..." section, tags like "bad anatomy" or
    "(worst quality:1.4)", "no extra fingers" style tags, and "with no
    extra limbs" inside a sentence."""
    text = (prompt or "").strip()
    moved: list[str] = []
    m = _NEG_SECTION.search(text)
    if m:
        moved.extend(split_tags(m.group(1)))
        text = text[: m.start()].rstrip(" ,.;")
    keep: list[str] = []
    for tag in split_tags(text):
        core = _core(tag)
        no = _NO_PREFIX.match(core)
        if no and is_defect(no.group(1)):
            moved.append(no.group(1))
        elif is_defect(core):
            moved.append(core)
        else:
            keep.append(tag)
    text = ", ".join(keep)

    def _inline(mt: re.Match) -> str:
        moved.append(mt.group(1).lower())
        return ""

    text = _INLINE_NO.sub(_inline, text)
    text = re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;])", r"\1", text))
    text = re.sub(r"(?:,\s*){2,}", ", ", text).strip(" ,;")
    return text, [t.strip() for t in moved if t.strip()]


def merge_negatives(*parts: str) -> str:
    """Join negative prompts, dropping repeated terms."""
    return tidy_tags(", ".join(p for p in parts if p and p.strip()))


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
    style = recipe.prompt_style
    if style in ("danbooru", "pony"):
        text = ensure_subject(tidy_tags(text))
    low = text.lower()
    if style == "pony":
        if "score_9" not in low:
            text = "score_9, score_8_up, score_7_up, " + text
        if recipe.pony_source:
            text = _append_missing(text, split_tags(recipe.pony_source))
        elif "source_" not in low:
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
        "worst quality, low quality, blurry, deformed, disfigured, bad anatomy, "
        "bad proportions, extra limbs, extra arms, extra legs, missing limbs, "
        "extra fingers, missing fingers, fused fingers, mutated hands, poorly drawn hands, "
        "malformed limbs, long neck, duplicate, watermark, text, cartoon, anime, 3d render, "
        "plastic skin"
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
    if recipe.style == "realistic" and recipe.prompt_style != "mixed" and neg:
        neg = f"{neg}, anime, cartoon, 3d render"  # realistic Pony/IL merges drift to 2D
    if nsfw and neg:
        neg = f"{neg}, {_ADULT_NEGATIVE}"
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
