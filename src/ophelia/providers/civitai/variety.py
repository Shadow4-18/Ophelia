"""Variety for auto mode: fill in what the prompt left open.

A prompt like "1girl, nude, on a bed" leaves camera, lighting and
expression to the model, which then picks the same safe default every
time (front view, flat light, neutral face). This adds one choice per
category the prompt doesn't mention, avoiding the ones used lately, so a
run of similar requests doesn't come back as the same picture. Categories
the prompt already covers are never touched, and solo poses are only added
when nobody else is in the scene.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import structlog

from ophelia.providers.civitai.prompting import _has_word, is_solo, split_tags

log = structlog.get_logger()

# category → (words that mean the prompt already covers it, tag options,
# natural-language options).
_BANKS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "camera": (
        ("from above", "from below", "from side", "from behind", "close-up", "closeup",
         "cowboy shot", "full body", "upper body", "lower body", "portrait", "pov",
         "angle", "shot", "view", "lens", "mm", "selfie", "mirror", "wide"),
        ("from above", "from below", "from side", "dutch angle", "close-up",
         "cowboy shot", "full body", "upper body", "pov", "wide shot", "from behind"),
        ("low-angle shot", "overhead shot", "close-up", "full-body shot",
         "shot from the side", "over-the-shoulder view", "wide shot", "mirror selfie"),
    ),
    "lighting": (
        ("light", "lights", "lighting", "lit", "sunlight", "sunset", "sunrise", "neon",
         "candle", "candlelight", "moonlight", "shadow", "shadows", "golden hour", "glow",
         "dark", "night", "backlighting", "flash"),
        ("soft lighting", "rim lighting", "backlighting", "dramatic lighting", "sunlight",
         "neon lights", "candlelight", "moonlight", "golden hour", "volumetric lighting",
         "window light"),
        ("soft window light", "warm candlelight", "neon glow", "golden-hour sunlight",
         "moody low-key lighting", "harsh camera flash", "moonlight through blinds"),
    ),
    "setting": (
        ("room", "bedroom", "bed", "couch", "sofa", "beach", "shower", "bath", "bathroom",
         "outdoors", "outside", "indoors", "background", "office", "forest", "city",
         "street", "pool", "kitchen", "car", "studio", "onsen", "hotel", "garden", "park",
         "balcony", "window", "rooftop", "throne", "castle", "gym", "locker room",
         "simple background", "white background", "alley", "alleyway", "ledge", "stairs",
         "staircase", "train", "subway", "bar", "club", "nightclub", "cafe", "church",
         "cathedral", "dungeon", "shrine", "temple", "library", "classroom", "rain",
         "snow", "field", "meadow", "lake", "river", "ocean", "sea", "desert", "mountain",
         "space", "cyberpunk city", "tokyo", "neon city", "rooftops", "bedsheets",
         "floor", "wall", "doorway", "hallway", "corridor", "elevator", "mirror"),
        ("bedroom", "bathroom", "shower", "hotel room", "couch", "office", "beach", "onsen",
         "forest", "rooftop at night", "kitchen", "library", "balcony", "gothic cathedral",
         "neon city street", "pool"),
        ("in a dim bedroom", "in a steamy shower", "in a hotel room", "on a leather couch",
         "in an office after hours", "on a beach", "in a hot spring", "on a balcony at night",
         "in a candlelit gothic room", "in a kitchen"),
    ),
    "expression": (
        ("smile", "smiling", "smirk", "blush", "blushing", "expression", "looking at",
         "eyes closed", "closed eyes", "ahegao", "moaning", "open mouth", "tongue",
         "crying", "angry", "pout", "wink", "biting lip", "lip bite", "face", "grin"),
        ("seductive smile", "blush", "half-closed eyes", "looking at viewer", "parted lips",
         "smirk", "biting lip", "embarrassed", "looking back", "wink"),
        ("with a teasing smile", "blushing", "with a sultry look", "biting her lip",
         "smirking at the camera", "looking back over her shoulder"),
    ),
    "pose": (
        ("sitting", "lying", "kneeling", "standing", "squatting", "leaning", "all fours",
         "bent over", "spread", "straddling", "pose", "posing", "arched", "crossed legs",
         "arms up", "on back", "on stomach", "on side", "stretching", "walking", "hand on",
         "hands on"),
        ("sitting", "lying on back", "lying on stomach", "kneeling", "standing",
         "leaning forward", "arms up", "hand on own hip", "crossed legs", "arched back",
         "on side", "stretching"),
        ("sitting on the edge of the bed", "lying back", "kneeling", "leaning forward",
         "stretching her arms up", "lying on her side", "arching her back"),
    ),
}

_HISTORY = 6  # recent picks per category to avoid
_MAX_TAGS = 35  # a prompt this long was written with intent: leave it alone


def _state_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "civitai_variety.json"


def _load(settings: Any) -> dict[str, list[str]]:
    try:
        data = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(settings: Any, data: dict[str, list[str]]) -> None:
    try:
        path = _state_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug("civitai.variety_save_failed", error=str(e))


def covered(prompt: str, category: str) -> bool:
    low = (prompt or "").lower()
    return any(_has_word(low, w) for w in _BANKS[category][0])


def enabled(settings: Any) -> bool:
    v = str(getattr(settings, "civitai_variety", "on") or "on").strip().lower()
    return v not in ("off", "false", "0", "no", "fixed")


def add_variety(
    settings: Any, prompt: str, *, natural: bool, rng: random.Random | None = None
) -> tuple[str, list[str]]:
    """(prompt with fill-ins, the fill-ins). Unchanged when nothing is open."""
    text = (prompt or "").strip()
    if not text or len(split_tags(text)) >= _MAX_TAGS:
        return text, []
    rng = rng or random.Random()
    state = _load(settings)
    added: list[str] = []
    for category, (_, tags, phrases) in _BANKS.items():
        if covered(text, category):
            continue
        if category == "pose" and not is_solo(text):
            continue  # a pose for one person would fight the scene she described
        options = list(phrases if natural else tags)
        recent = state.get(category, [])
        fresh = [o for o in options if o not in recent] or options
        pick = rng.choice(fresh)
        added.append(pick)
        state[category] = (recent + [pick])[-_HISTORY:]
    if not added:
        return text, []
    _save(settings, state)
    return text + ", " + ", ".join(added), added
