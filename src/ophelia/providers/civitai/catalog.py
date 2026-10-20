"""What Ophelia can generate with on Civitai, and how each model runs best.

A *recipe* is one model plus the settings its authors / Civitai's docs
recommend (steps, cfg, prompt style, negatives). Auto mode picks the
cheapest recipe that is good at the request's style; ``quality`` raises the
render profile (more steps, bigger canvas) only when asked. Ophelia can also
name a recipe, or pin any checkpoint AIR from search_civitai_models.

Costs are estimates from Civitai's published formulas (Buzz). The real
charge comes back on the workflow and is logged next to the estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from ophelia.providers.civitai.air import (
    air_kind,
    base_family,
    ecosystem_from_air_or_base,
    is_air,
    sanitize_air,
)

# Checkpoint AIRs (verified on the site API, 2026-10).
WAI_ILLUSTRIOUS_AIR = "urn:air:sdxl:checkpoint:civitai:827184@2883731"  # v17.0
PONY_V6_AIR = "urn:air:sdxl:checkpoint:civitai:257749@290640"
LUSTIFY_AIR = "urn:air:sdxl:checkpoint:civitai:573152@3045803"  # ZENITH (V9), SDXL
SDXL_BASE_AIR = "urn:air:sdxl:checkpoint:civitai:101055@128078"
FLUX1_DEV_DIFFUSER = "urn:air:flux1:diffuser:civitai:618692@691639"

QUALITIES = ("standard", "high", "max")
STYLES = ("anime", "pony", "realistic", "general")


@dataclass(frozen=True)
class Recipe:
    key: str
    label: str
    ecosystem: str  # orchestration ecosystem value
    family: str  # LoRA family: illustrious|pony|sdxl|sd1|anima|zimage|qwen|flux
    style: str  # anime|pony|realistic|general
    engine: str = "sdcpp"
    model: str = ""  # checkpoint AIR, or a named model ("turbo", "20b", "klein")
    model_field: str = "model"  # "model" | "diffuserModel" | "" (none allowed)
    extra: dict[str, Any] = field(default_factory=dict)
    steps: int = 20
    cfg: float = 7.0
    img2img: bool = True
    loras: bool = True
    negative: bool = True
    max_quantity: int = 4
    size_multiple: int = 16
    min_side: int = 64
    max_side: int = 2048
    max_steps: int = 50
    prompt_style: str = "mixed"  # danbooru|pony|natural|mixed
    prompt_limit: int = 10000
    nsfw: str = "strong"  # strong|ok|weak — how well it renders explicit content
    base_model: str = ""  # site-API baseModel, for LoRA checks
    cost: str = "sdxl"  # cost formula key
    notes: str = ""

    def estimate_buzz(self, width: int, height: int, steps: int, quantity: int) -> float:
        px = width * height
        q = max(1, quantity)
        if self.cost == "sdxl":
            return 8 * (px / 1024**2) * (steps / 20) * q
        if self.cost == "anima":
            return 8 * (px / 1024**2) * (steps / 25) * q
        if self.cost == "zimage-turbo":
            return 8 * (px / 1024**2) * (steps / 9) * q
        if self.cost == "zimage-base":
            return 20 * (px / 1024**2) * (steps / 20) * q
        if self.cost == "qwen":
            return 30 * (px / 1328**2) * (steps / 20) * q
        if self.cost == "flux1":
            return 0.5 * steps * (1 if self.cfg == 1 else 2) * q
        if self.cost == "flux2-klein":
            return 0.3 * steps * (1 if self.cfg == 1 else 2) * q
        if self.cost == "flux2-dev":
            return 40 * math.ceil(px / 1_000_000) * q
        return 8 * q


RECIPES: dict[str, Recipe] = {
    r.key: r
    for r in (
        Recipe(
            key="illustrious", label="WAI-Illustrious v17 (anime, SDXL)",
            ecosystem="sdxl", family="illustrious", style="anime",
            model=WAI_ILLUSTRIOUS_AIR, steps=20, cfg=6.0, prompt_style="danbooru",
            base_model="Illustrious",
            notes="Best anime/hentai checkpoint for the money. Danbooru tags.",
        ),
        Recipe(
            key="pony", label="Pony Diffusion V6 XL (anime/furry, SDXL)",
            ecosystem="sdxl", family="pony", style="pony",
            model=PONY_V6_AIR, steps=25, cfg=7.0, prompt_style="pony",
            base_model="Pony",
            notes="Huge LoRA library. Needs score_* and source_* tags.",
        ),
        Recipe(
            key="lustify", label="LUSTIFY ZENITH V9 (photoreal NSFW, SDXL)",
            ecosystem="sdxl", family="sdxl", style="realistic",
            model=LUSTIFY_AIR, steps=25, cfg=4.5, prompt_style="mixed",
            base_model="SDXL 1.0",
            notes="Photoreal, explicit-capable. Tags or natural language.",
        ),
        Recipe(
            key="sdxl", label="SDXL 1.0 base (general)",
            ecosystem="sdxl", family="sdxl", style="general",
            model=SDXL_BASE_AIR, steps=20, cfg=7.0, prompt_style="mixed",
            base_model="SDXL 1.0", nsfw="weak",
            notes="Plain base model. Weak at people; a last resort.",
        ),
        Recipe(
            key="anima", label="Anima (newer anime model)",
            ecosystem="anima", family="anima", style="anime",
            model_field="", steps=30, cfg=4.0, img2img=False, prompt_style="danbooru",
            cost="anima", nsfw="ok", max_quantity=4,
            notes="Anime-tuned, text-to-image only. Anima LoRAs only.",
        ),
        Recipe(
            key="zimage-turbo", label="Z-Image Turbo (fast photoreal)",
            ecosystem="zImage", family="zimage", style="realistic",
            model="turbo", steps=9, cfg=1.0, img2img=False, negative=False,
            prompt_style="natural", cost="zimage-turbo", nsfw="ok", max_steps=12,
            notes="Cheap and fast; natural-language prompts; cfg stays at 1.",
        ),
        Recipe(
            key="zimage-base", label="Z-Image Base (photoreal, higher quality)",
            ecosystem="zImage", family="zimage", style="realistic",
            model="base", steps=24, cfg=4.0, img2img=False, prompt_style="natural",
            cost="zimage-base", nsfw="ok",
            notes="Slower and pricier than turbo; accepts negatives and LoRAs.",
        ),
        Recipe(
            key="qwen", label="Qwen-Image 20B (strong prompt following)",
            ecosystem="qwen", family="qwen", style="general",
            model="20b", extra={"version": "latest"}, steps=20, cfg=2.5,
            size_multiple=8, prompt_style="natural", cost="qwen", nsfw="weak",
            notes="Follows long detailed prompts and text in images. Weak at nudity.",
        ),
        Recipe(
            key="flux1", label="Flux.1 dev (natural language)",
            ecosystem="flux1", family="flux", style="general",
            model=FLUX1_DEV_DIFFUSER, model_field="diffuserModel", steps=28, cfg=3.5,
            min_side=832, max_side=1216, prompt_style="natural", prompt_limit=1000,
            cost="flux1", nsfw="weak",
            notes="Good composition; censored base, so weak at nudity.",
        ),
        Recipe(
            key="flux2-klein", label="Flux.2 Klein 4B",
            ecosystem="flux2Klein", engine="flux2", family="flux", style="general",
            model="klein", extra={"modelVersion": "4b"}, steps=20, cfg=5.0,
            min_side=512, prompt_style="natural", prompt_limit=1000,
            cost="flux2-klein", nsfw="weak",
            notes="Newest Flux, cheap tier. Weak at nudity.",
        ),
    )
}

# Auto-pick: style → recipe for explicit requests (all NSFW-strong SDXL
# fine-tunes; tested on the phone 2026-10-20).
AUTO_RECIPE: dict[str, str] = {
    "anime": "illustrious",
    "illustrious": "illustrious",
    "pony": "pony",
    "realistic": "lustify",
    "general": "illustrious",
    "sdxl": "illustrious",
}

# Render profiles: (steps multiplier, megapixels). "standard" is each
# model's recommended settings; the others cost more and are only used when
# the owner asks for higher quality.
PROFILES: dict[str, tuple[float, float]] = {
    "standard": (1.0, 1.0),
    "high": (1.2, 1.25),
    "max": (1.4, 1.5),
}

_ALIASES = {
    "pony": "pony", "ponyxl": "pony", "pony-xl": "pony", "ponydiffusion": "pony",
    "ponydiffusionv6": "pony",
    "illustrious": "illustrious", "illustrious-xl": "illustrious",
    "illustriousxl": "illustrious", "wai": "illustrious", "wai-illustrious": "illustrious",
    "anime": "illustrious",
    "lustify": "lustify", "realistic": "lustify", "photoreal": "lustify",
    "sdxl": "sdxl",
    "zimage": "zimage-turbo", "z-image": "zimage-turbo",
    "flux": "flux1", "flux1": "flux1", "flux2": "flux2-klein", "klein": "flux2-klein",
}


def recipe_by_name(name: str) -> Recipe | None:
    key = (name or "").strip().lower().replace(" ", "").replace("_", "-")
    if key in RECIPES:
        return RECIPES[key]
    alias = _ALIASES.get(key)
    return RECIPES.get(alias) if alias else None


def resolve_checkpoint_alias(raw: str) -> str:
    """Map short names (pony, illustrious, ...) to their checkpoint AIRs."""
    r = recipe_by_name(raw)
    return r.model if r and is_air(r.model) and r.ecosystem == "sdxl" else ""


def _family_defaults(family: str) -> tuple[int, float, str]:
    """(steps, cfg, prompt_style) for a pinned checkpoint of this family."""
    return {
        "illustrious": (25, 6.0, "danbooru"),
        "pony": (25, 7.0, "pony"),
        "sd1": (25, 7.0, "danbooru"),
    }.get(family, (20, 7.0, "mixed"))


def recipe_for_air(air: str, base_model: str = "") -> Recipe:
    """A recipe for any pinned checkpoint AIR (e.g. from a search)."""
    clean = sanitize_air(air)
    for r in RECIPES.values():
        if r.model and r.model == clean:
            return r
    eco = ecosystem_from_air_or_base(clean, base_model)
    fam = base_family(base_model, clean)
    if eco == "flux1":
        field_ = "diffuserModel" if air_kind(clean) == "diffuser" else "model"
        return replace(RECIPES["flux1"], key="custom", label=clean, model=clean,
                       model_field=field_, base_model=base_model)
    steps, cfg, pstyle = _family_defaults(fam)
    style = {"illustrious": "anime", "pony": "pony"}.get(fam, "general")
    return Recipe(
        key="custom", label=clean, ecosystem="sd1" if eco == "sd1" else "sdxl",
        family=fam if fam != "other" else "sdxl", style=style, model=clean,
        steps=steps, cfg=cfg, prompt_style=pstyle, base_model=base_model,
        nsfw="ok", notes="Pinned checkpoint.",
    )


def _round_to(value: float, multiple: int) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


_ASPECTS = {
    "1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3), "3:4": (3, 4),
    "3:2": (3, 2), "2:3": (2, 3),
}


def dimensions(recipe: Recipe, aspect_ratio: str, megapixels: float = 1.0) -> tuple[int, int]:
    """Width/height near ``megapixels`` (of 1024²), on the recipe's grid and
    within its size limits."""
    aw, ah = _ASPECTS.get(aspect_ratio, (1, 1))
    area = 1024 * 1024 * megapixels
    w = math.sqrt(area * aw / ah)
    h = w * ah / aw
    m = recipe.size_multiple
    w_i = min(recipe.max_side, max(recipe.min_side, _round_to(w, m)))
    h_i = min(recipe.max_side, max(recipe.min_side, _round_to(h, m)))
    return w_i, h_i


def render_settings(
    recipe: Recipe, quality: str, aspect_ratio: str
) -> tuple[int, int, int]:
    """(width, height, steps) for a quality profile."""
    step_mult, mp = PROFILES.get(quality, PROFILES["standard"])
    steps = min(recipe.max_steps, max(1, int(round(recipe.steps * step_mult))))
    w, h = dimensions(recipe, aspect_ratio, mp)
    return w, h, steps


def catalog_text(*, notes: bool = True) -> str:
    """One line per recipe, for her tool description / search results."""
    lines = []
    for r in RECIPES.values():
        w, h, steps = render_settings(r, "standard", "1:1")
        est = r.estimate_buzz(w, h, steps, 1)
        extra = [] if r.img2img else ["txt2img only"]
        if r.nsfw != "strong":
            extra.append(f"nsfw {r.nsfw}")
        tail = f" ({', '.join(extra)})" if extra else ""
        line = f"- {r.key}: {r.label}, ~{est:.0f} Buzz{tail}"
        lines.append(f"{line}. {r.notes}" if notes else line)
    return "\n".join(lines)
