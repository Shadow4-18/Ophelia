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

# More of the most downloaded / highest rated NSFW-capable checkpoints per
# family (site API, 2026-10). They rotate in auto mode for variety.
HASSAKU_IL_AIR = "urn:air:sdxl:checkpoint:civitai:140272@2615702"  # v3.4
NOVA_ANIME_AIR = "urn:air:sdxl:checkpoint:civitai:376130@2940478"  # IL v19.0
PREFECT_IL_AIR = "urn:air:sdxl:checkpoint:civitai:1224788@2864868"  # v8
ONE_OBSESSION_AIR = "urn:air:sdxl:checkpoint:civitai:1318945@3218603"  # v24
AUTISMMIX_AIR = "urn:air:sdxl:checkpoint:civitai:288584@324619"
PREFECT_PONY_AIR = "urn:air:sdxl:checkpoint:civitai:439889@2114187"  # v6
WAI_ANI_PONY_AIR = "urn:air:sdxl:checkpoint:civitai:404154@1767402"  # v14.0
CYBERREALISTIC_PONY_AIR = "urn:air:sdxl:checkpoint:civitai:443821@2884631"  # v18.0
PORNMASTER_AIR = "urn:air:sdxl:checkpoint:civitai:82543@2551619"  # Pro-SDXL-V8
BIG_LOVE_AIR = "urn:air:sdxl:checkpoint:civitai:897413@2867688"  # Ultra5
REALISM_IL_AIR = "urn:air:sdxl:checkpoint:civitai:974693@2831949"  # v5.5

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
    pool: str = ""  # auto-mode rotation pool (anime|pony|realistic), "" = not pooled
    weight: float = 1.0  # share of its pool before feedback
    pony_source: str = ""  # tags in place of source_anime (realistic Pony merges)
    sampler: str = ""  # sdcpp sampleMethod; "" = the family default (sampling_for)
    schedule: str = ""  # sdcpp schedule; "" = the family default
    # Non-sdcpp engines (comfy, seedream, google, fal, ...): how the request
    # is shaped, straight from the orchestration spec. Keys: fixed (fields
    # sent as-is), op (send operation), size ("wh" | "aspect" |
    # "image_size" | "none"), aspects (allowed ratios), cfg (field name or
    # ""), steps, seed. Empty = the sdcpp/flux2 paths.
    api: dict[str, Any] = field(default_factory=dict)

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
        if self.cost == "api":
            return 0.0  # priced by Civitai per engine; the real cost is logged after
        return 8 * q


RECIPES: dict[str, Recipe] = {
    r.key: r
    for r in (
        Recipe(
            key="illustrious", label="WAI-Illustrious v17 (anime, SDXL)",
            ecosystem="sdxl", family="illustrious", style="anime",
            model=WAI_ILLUSTRIOUS_AIR, steps=20, cfg=6.0, prompt_style="danbooru",
            base_model="Illustrious", pool="anime", weight=3.0,
            notes="Best anime/hentai checkpoint for the money. Danbooru tags.",
        ),
        Recipe(
            key="hassaku", label="Hassaku XL Illustrious v3.4 (anime)",
            ecosystem="sdxl", family="illustrious", style="anime",
            model=HASSAKU_IL_AIR, steps=25, cfg=5.5, prompt_style="danbooru",
            base_model="Illustrious", pool="anime", weight=2.0,
            notes="Hentai-focused Illustrious merge; softer colors.",
        ),
        Recipe(
            key="nova-anime", label="Nova Anime XL IL v19 (anime)",
            ecosystem="sdxl", family="illustrious", style="anime",
            model=NOVA_ANIME_AIR, steps=25, cfg=5.0, prompt_style="danbooru",
            base_model="Illustrious", pool="anime", weight=2.0,
            notes="Bright, clean modern anime look.",
        ),
        Recipe(
            key="prefect-il", label="Prefect Illustrious XL v8 (anime)",
            ecosystem="sdxl", family="illustrious", style="anime",
            model=PREFECT_IL_AIR, steps=25, cfg=5.0, prompt_style="danbooru",
            base_model="Illustrious", pool="anime", weight=1.5,
            notes="Detailed, semi-glossy anime; strong anatomy.",
        ),
        Recipe(
            key="one-obsession", label="One Obsession v24 (anime)",
            ecosystem="sdxl", family="illustrious", style="anime",
            model=ONE_OBSESSION_AIR, steps=25, cfg=5.0, prompt_style="danbooru",
            base_model="Illustrious", pool="anime", weight=1.0,
            notes="Stylised, painterly Illustrious merge.",
        ),
        Recipe(
            key="pony", label="Pony Diffusion V6 XL (anime/furry, SDXL)",
            ecosystem="sdxl", family="pony", style="pony",
            model=PONY_V6_AIR, steps=25, cfg=7.0, prompt_style="pony",
            base_model="Pony", pool="pony", weight=2.0,
            notes="Huge LoRA library. Needs score_* and source_* tags.",
        ),
        Recipe(
            key="autismmix", label="AutismMix SDXL (Pony merge)",
            ecosystem="sdxl", family="pony", style="pony",
            model=AUTISMMIX_AIR, steps=25, cfg=7.0, prompt_style="pony",
            base_model="Pony", pool="pony", weight=2.0,
            notes="Pony merge with steadier anatomy; Pony LoRAs work.",
        ),
        Recipe(
            key="prefect-pony", label="Prefect Pony XL v6",
            ecosystem="sdxl", family="pony", style="pony",
            model=PREFECT_PONY_AIR, steps=25, cfg=6.0, prompt_style="pony",
            base_model="Pony", pool="pony", weight=1.5,
            notes="Polished, detailed Pony merge.",
        ),
        Recipe(
            key="wai-pony", label="WAI-ANI-PONYXL v14",
            ecosystem="sdxl", family="pony", style="pony",
            model=WAI_ANI_PONY_AIR, steps=25, cfg=7.0, prompt_style="pony",
            base_model="Pony", pool="pony", weight=1.5,
            notes="Clean anime Pony merge.",
        ),
        Recipe(
            key="lustify", label="LUSTIFY ZENITH V9 (photoreal NSFW, SDXL)",
            ecosystem="sdxl", family="sdxl", style="realistic",
            model=LUSTIFY_AIR, steps=30, cfg=4.5, prompt_style="mixed",
            base_model="SDXL 1.0", pool="realistic", weight=3.0,
            notes="Photoreal, explicit-capable. Tags or natural language.",
        ),
        Recipe(
            key="pornmaster", label="PornMaster Pro SDXL v8 (photoreal NSFW)",
            ecosystem="sdxl", family="sdxl", style="realistic",
            model=PORNMASTER_AIR, steps=30, cfg=5.0, prompt_style="mixed",
            base_model="SDXL 1.0", pool="realistic", weight=2.0,
            notes="Explicit photoreal specialist.",
        ),
        Recipe(
            key="biglove", label="Big Love Ultra5 (photoreal NSFW)",
            ecosystem="sdxl", family="sdxl", style="realistic",
            model=BIG_LOVE_AIR, steps=30, cfg=4.5, prompt_style="mixed",
            base_model="SDXL 1.0", pool="realistic", weight=1.5,
            notes="Amateur-photo look, natural skin.",
        ),
        Recipe(
            key="cyberrealistic-pony", label="CyberRealistic Pony v18 (photoreal, Pony)",
            ecosystem="sdxl", family="pony", style="realistic",
            model=CYBERREALISTIC_PONY_AIR, steps=30, cfg=4.5, prompt_style="pony",
            base_model="Pony", pool="realistic", weight=1.5,
            pony_source="photo, realistic",
            notes="Photoreal with Pony's pose range and LoRAs.",
        ),
        Recipe(
            key="realism-il", label="Realism Illustrious v5.5 (photoreal, Illustrious)",
            ecosystem="sdxl", family="illustrious", style="realistic",
            model=REALISM_IL_AIR, steps=25, cfg=5.0, prompt_style="danbooru",
            base_model="Illustrious", pool="realistic", weight=1.0,
            notes="Photoreal that reads danbooru tags; Illustrious LoRAs work.",
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

CHROMA_HD_AIR = "urn:air:chroma:checkpoint:civitai:1330309@2164239"  # v1.0-HD
UNCANNY_CHROMA_AIR = "urn:air:chroma:checkpoint:civitai:2086389@2517681"  # v1.3
PONY_V7_AIR = "urn:air:ponyv7:checkpoint:civitai:1901521@2152373"  # v7.0 base

_FAL_SIZES = ("square_hd", "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9")
_GOOGLE_ASPECTS = ("21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16")
_COMMON_ASPECTS = ("21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16", "9:21")


def _comfy(key: str, label: str, ecosystem: str, model: str | None, steps: int, cfg: float,
           *, negative: bool = True, max_q: int = 4, min_side: int = 64, nsfw: str = "weak",
           style: str = "general", notes: str = "", **extra: Any) -> Recipe:
    """A Civitai ComfyUI base model (the 'families' list on the site)."""
    air = extra.pop("model_air", "")
    model = air or model  # chroma / ponyV7 take a checkpoint AIR as the model
    fixed: dict[str, Any] = {"ecosystem": ecosystem}
    if model is not None:
        fixed["model"] = model
    fixed.update(extra)
    return Recipe(
        key=key, label=label, ecosystem=ecosystem, family="other", style=style,
        engine="comfy", model=str(model or ""), steps=steps, cfg=cfg, img2img=False,
        loras=False, negative=negative, max_quantity=max_q, min_side=min_side,
        max_steps=steps if cfg <= 1 else 50,  # distilled/turbo: more steps over-cook
        prompt_style="natural", cost="api", nsfw=nsfw, notes=notes,
        api={"fixed": fixed, "op": True, "size": "wh", "cfg": "cfgScale", "steps": True,
             "seed": True},
    )


def _hosted(key: str, label: str, engine: str, fixed: dict[str, Any], *, size: str = "wh",
            aspects: tuple[str, ...] = (), cfg_field: str = "", cfg: float = 0.0,
            steps: int = 0, op: bool = False, seed: bool = True, negative: bool = False,
            max_q: int = 4, min_side: int = 512, max_side: int = 2048,
            notes: str = "") -> Recipe:
    """A commercial engine Civitai resells (Seedream, Nano Banana, Flux Pro, ...).
    Their own safety filters apply, so they are never picked for explicit
    images."""
    return Recipe(
        key=key, label=label, ecosystem=engine, family="other", style="general",
        engine=engine, model=str(fixed.get("model") or fixed.get("version") or ""),
        steps=steps or 1, cfg=cfg, img2img=False, loras=False, negative=negative,
        max_quantity=max_q, min_side=min_side, max_side=max_side, max_steps=max(steps, 1),
        prompt_style="natural", cost="api", nsfw="weak", notes=notes,
        api={"fixed": fixed, "op": op, "size": size, "aspects": aspects, "cfg": cfg_field,
             "steps": bool(steps), "seed": seed},
    )


# Every other base model ("family") Civitai can run, from the orchestration
# spec (2026-11). Text-to-image only, no LoRAs; auto mode never picks them,
# she names them with model=<key>.
EXTRA_RECIPES: tuple[Recipe, ...] = (
    _comfy("chroma", "Chroma v1 HD (uncensored Flux-style, photoreal/any)", "chroma", None,
           28, 3.5, nsfw="strong", style="realistic", model_air=CHROMA_HD_AIR,
           notes="Uncensored, natural language, good anatomy."),
    _comfy("chroma-uncanny", "UnCanny (photoreal Chroma)", "chroma", None, 28, 3.5,
           nsfw="strong", style="realistic", model_air=UNCANNY_CHROMA_AIR,
           notes="Photoreal Chroma fine-tune."),
    _comfy("pony-v7", "Pony V7 (AuraFlow)", "ponyV7", None, 25, 7.0, nsfw="ok",
           style="pony", model_air=PONY_V7_AIR, notes="Natural language plus style tags."),
    _comfy("krea2", "Krea 2 (raw)", "krea2", "raw", 20, 4.0, nsfw="ok", style="realistic",
           notes="Photographic, natural skin."),
    _comfy("krea2-turbo", "Krea 2 Turbo", "krea2", "turbo", 8, 1.0, nsfw="ok",
           style="realistic", notes="Fast Krea 2."),
    _comfy("hidream", "HiDream I1 (full)", "hidream", None, 30, 5.0, variant="full",
           notes="Strong prompt following."),
    _comfy("hidream-fast", "HiDream I1 fast", "hidream", None, 16, 1.0, variant="fast"),
    _comfy("hidream-o1", "HiDream O1", "hidream-o1", "HiDream-O1-Image", 40, 5.0,
           notes="Newest HiDream, detailed."),
    _comfy("ernie", "ERNIE Image", "ernie", "ernie", 20, 4.0),
    _comfy("ernie-turbo", "ERNIE Image Turbo", "ernie", "turbo", 8, 1.0),
    _comfy("boogu", "Boogu", "boogu", "base", 20, 4.0),
    _comfy("boogu-turbo", "Boogu Turbo", "boogu", "turbo", 4, 1.0),
    _comfy("lens", "Lens", "lens", "normal", 20, 4.0),
    _comfy("lens-turbo", "Lens Turbo", "lens", "turbo", 4, 1.0),
    _comfy("ming", "Ming Design", "ming", "design", 12, 1.0, min_side=256,
           notes="Design/graphic look."),
    _comfy("moss", "MOSS Image", "mossImage", "base", 25, 2.5),
    _comfy("moss-sft", "MOSS Image SFT", "mossImage", "sft", 25, 2.5),
    _comfy("flux3-klein", "Flux.3 Klein", "flux3Klein", "base", 50, 3.5),
    _comfy("flux3-klein-fast", "Flux.3 Klein distilled", "flux3Klein", "distilled", 16, 1.0),
    _comfy("mageflow", "MageFlow 4B", "mageflow", "4b", 20, 5.0, min_side=512),
    _comfy("mageflow-turbo", "MageFlow 4B Turbo", "mageflow", "4b-turbo", 4, 1.0,
           min_side=512),
    _comfy("ideogram4", "Ideogram 4 (open)", "ideogram4", None, 12, 7.0, negative=False,
           notes="Good at text in images."),
    _comfy("fibo2", "FIBO 2", "fibo2", "dev", 30, 5.0),
    _comfy("qwen-2.1", "Qwen-Image 2.1", "qwen", "2.1", 25, 1.0),
    _comfy("qwen-2.1-turbo", "Qwen-Image 2.1 Turbo", "qwen", "2.1-turbo", 8, 1.0),
    _hosted("qwen-3", "Qwen Image 3.0 Pro (API)", "qwen", {"model": "3.0-pro"}, op=True,
            negative=True, max_q=6, min_side=256),
    _hosted("qwen-2", "Qwen Image 2.0 Pro (API)", "qwen", {"model": "2.0-pro"}, op=True,
            negative=True, max_q=6, min_side=256),
    _hosted("qwen2-fal", "Qwen Image 2 (fal)", "fal", {"model": "qwen2"}, op=True,
            size="image_size", negative=True),
    _hosted("seedream", "Seedream 5.0 Pro", "seedream", {"version": "v5.0-pro"},
            cfg_field="guidanceScale", cfg=2.5, min_side=256, max_side=4096,
            notes="ByteDance; very good photoreal and text."),
    _hosted("seedream-4.5", "Seedream 4.5", "seedream", {"version": "v4.5"},
            cfg_field="guidanceScale", cfg=2.5, min_side=256, max_side=4096),
    _hosted("seedream-4", "Seedream 4", "seedream", {"version": "v4"},
            cfg_field="guidanceScale", cfg=2.5, min_side=256, max_side=4096),
    _hosted("nano-banana-2", "Nano Banana 2 (Google)", "google", {"model": "nano-banana-2"},
            size="aspect", aspects=_GOOGLE_ASPECTS),
    _hosted("nano-banana-2.1", "Nano Banana 2.1 (Google)", "google",
            {"model": "nano-banana-2.1"}, size="aspect", aspects=_GOOGLE_ASPECTS, seed=False),
    _hosted("nano-banana-pro", "Nano Banana Pro (Google)", "google",
            {"model": "nano-banana-pro"}, size="aspect", aspects=_GOOGLE_ASPECTS, seed=False),
    _hosted("imagen4", "Imagen 4 (Google)", "google", {"model": "imagen4"}, size="aspect",
            aspects=("1:1", "16:9", "9:16", "3:4", "4:3"), negative=True),
    _hosted("flux3", "Flux.3 (fal)", "fal", {"model": "flux3"}, op=True, size="aspect",
            aspects=("21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16"),
            seed=False),
    _hosted("ideogram", "Ideogram 4.5 (fal)", "fal", {"model": "ideogram45"}, op=True,
            size="image_size", notes="Best at text and typography."),
    _hosted("reve", "Reve (fal)", "fal", {"model": "reve"}, op=True, size="aspect",
            aspects=("21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16"),
            seed=False),
    _hosted("muse", "Muse Image (fal)", "fal", {"model": "museImage"}, op=True,
            size="aspect", aspects=("21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16"),
            seed=False),
    _hosted("mai", "MAI Image (Microsoft, fal)", "fal", {"model": "maiImage"}, op=True,
            size="aspect", aspects=_GOOGLE_ASPECTS, seed=False),
    _hosted("wan-image", "Wan 2.7 Image (fal)", "wan", {"version": "v2.7", "provider": "fal"},
            op=True, size="image_size", cfg_field="cfgScale", cfg=3.5, negative=True),
    _hosted("flux2-pro", "Flux.2 Pro", "flux2", {"model": "pro"}, op=True),
    _hosted("flux2-max", "Flux.2 Max", "flux2", {"model": "max"}, op=True),
    _hosted("flux2-flex", "Flux.2 Flex", "flux2", {"model": "flex"}, op=True,
            cfg_field="guidanceScale", cfg=3.5, steps=28),
    _hosted("flux2-dev", "Flux.2 Dev", "flux2", {"model": "dev"}, op=True,
            cfg_field="guidanceScale", cfg=2.5, steps=28),
    _hosted("flux-ultra", "Flux.1 Pro Ultra", "flux1-pro", {"model": "ultra"}, size="aspect",
            aspects=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "9:21")),
    _hosted("flux-kontext", "Flux.1 Kontext Pro", "flux1-kontext", {"model": "pro"},
            size="aspect", aspects=_COMMON_ASPECTS, cfg_field="guidanceScale", cfg=3.5),
    _hosted("flux-kontext-max", "Flux.1 Kontext Max", "flux1-kontext", {"model": "max"},
            size="aspect", aspects=_COMMON_ASPECTS, cfg_field="guidanceScale", cfg=3.5),
    _hosted("gpt-image-2", "GPT Image 2 (OpenAI)", "openai", {"model": "gpt-image-2"},
            op=True, seed=False, min_side=256, max_side=3840),
)
RECIPES.update({r.key: r for r in EXTRA_RECIPES})

# Auto-pick: style → recipe for explicit requests (all NSFW-strong SDXL
# fine-tunes; tested on the phone 2026-10-20). Each is the head of its
# pool; the others in the pool rotate in (see selection.py).
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
    "hassakuxl": "hassaku", "nova": "nova-anime", "novaanime": "nova-anime",
    "novaanimexl": "nova-anime", "prefect": "prefect-il", "prefectillustrious": "prefect-il",
    "oneobsession": "one-obsession", "autism": "autismmix", "autismmixsdxl": "autismmix",
    "prefectpony": "prefect-pony", "waipony": "wai-pony", "wai-ani-ponyxl": "wai-pony",
    "cyberrealistic": "cyberrealistic-pony", "cyberrealisticpony": "cyberrealistic-pony",
    "big-love": "biglove", "porn-master": "pornmaster", "realismillustrious": "realism-il",
    # Other families, by the names on the site.
    "krea": "krea2", "krea2raw": "krea2", "krea-2": "krea2", "krea2turbo": "krea2-turbo",
    "flux.1krea": "krea2", "fluxkrea": "krea2",
    "seedream5": "seedream", "seedream-5": "seedream", "seedream4": "seedream-4",
    "seedream-4.0": "seedream-4", "seedream45": "seedream-4.5",
    "nanobanana": "nano-banana-2", "nano-banana": "nano-banana-2",
    "nanobanana2": "nano-banana-2", "nanobananapro": "nano-banana-pro",
    "nanobanana2.1": "nano-banana-2.1", "gemini": "nano-banana-2", "imagen": "imagen4",
    "hidream-i1": "hidream", "hidreamo1": "hidream-o1",
    "ideogram4open": "ideogram4", "ideogram45": "ideogram", "ideogram4.5": "ideogram",
    "mingdesign": "ming", "muse-image": "muse", "museimage": "muse", "maiimage": "mai",
    "mage-flow": "mageflow", "mageflow4b": "mageflow", "wan": "wan-image",
    "wan2.7": "wan-image", "wanimage": "wan-image",
    "qwen2": "qwen-2", "qwen-image-2": "qwen-2", "qwen3": "qwen-3", "qwen-image-3": "qwen-3",
    "qwen2.1": "qwen-2.1", "ernieimage": "ernie", "moss-image": "moss", "mossimage": "moss",
    "kontext": "flux-kontext", "flux-kontext-pro": "flux-kontext", "fluxkontext": "flux-kontext",
    "flux.1-kontext": "flux-kontext", "flux1-kontext": "flux-kontext",
    "flux.2": "flux2-pro", "flux.2-pro": "flux2-pro", "flux2pro": "flux2-pro",
    "flux.2-dev": "flux2-dev", "flux.2-klein": "flux2-klein",
    "flux.3": "flux3", "flux3-pro": "flux3", "flux.3-klein": "flux3-klein",
    "flux-pro-ultra": "flux-ultra", "flux1-ultra": "flux-ultra", "flux.1-ultra": "flux-ultra",
    "gpt-image": "gpt-image-2", "gptimage2": "gpt-image-2", "openai": "gpt-image-2",
    "chroma1": "chroma", "chroma-hd": "chroma", "uncanny": "chroma-uncanny",
    "ponyv7": "pony-v7", "pony7": "pony-v7",
}


def pool(style: str) -> list[Recipe]:
    """Recipes that take turns for a style in auto mode."""
    return [r for r in RECIPES.values() if r.pool == style]


# Sampler per family. Civitai's sdcpp default is plain euler/discrete, which
# under-cooks SDXL photoreal models (smeared hands, doubled limbs): photoreal
# runs DPM++ 2M Karras like their authors recommend, anime/Pony Euler a.
_SAMPLING = {
    "anime": ("euler_a", "discrete"),
    "realistic": ("dpm++2m", "karras"),
}


def sampling_for(recipe: Recipe) -> tuple[str, str]:
    """(sampleMethod, schedule) for SDXL/SD1 checkpoints; ("", "") elsewhere."""
    if recipe.ecosystem not in ("sdxl", "sd1"):
        return "", ""
    if recipe.sampler:
        return recipe.sampler, recipe.schedule or "discrete"
    if recipe.style == "realistic" or (recipe.family == "sdxl" and recipe.style != "anime"):
        return _SAMPLING["realistic"]
    return _SAMPLING["anime"]


# Negative embeddings (textual inversions) per family: (AIR, trigger word).
# They go in the request's embeddings list and their trigger words in the
# negative prompt; each fixes what plain negative words can't (hands, extra
# limbs, the plastic look). Picked from the most downloaded on Civitai, with
# file names equal to their trigger words so the engine finds them.
LAZYNEG = ("urn:air:sdxl:embedding:civitai:1302719@2121199", "lazyneg")
LAZYHAND = ("urn:air:sdxl:embedding:civitai:1302719@2268235", "lazyhand")
ZPDXL3 = ("urn:air:sdxl:embedding:civitai:332646@720175", "zPDXL3")
ZPDXL_REAL = ("urn:air:sdxl:embedding:civitai:332646@482268", "zPDXLrl")
DEEPNEG_XL = ("urn:air:sdxl:embedding:civitai:407448@454217", "DeepNegative_xl_v1")
NEG_HANDS_XL = ("urn:air:sdxl:embedding:civitai:583583@651084", "NEGATIVE_HANDS")


def negative_embeddings(recipe: Recipe) -> list[tuple[str, str]]:
    if recipe.ecosystem != "sdxl" or not recipe.negative:
        return []
    if recipe.family == "illustrious":
        return [LAZYNEG, LAZYHAND]
    if recipe.family == "pony":
        return [ZPDXL3, ZPDXL_REAL] if recipe.style == "realistic" else [ZPDXL3]
    if recipe.family == "sdxl" and recipe.style != "anime":
        return [DEEPNEG_XL, NEG_HANDS_XL]
    return []


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
    """One line per main recipe, then the other families on one line each
    group, for her tool description / search results."""
    lines = []
    extra_keys = {r.key for r in EXTRA_RECIPES}
    for r in RECIPES.values():
        if r.key in extra_keys:
            continue
        w, h, steps = render_settings(r, "standard", "1:1")
        est = r.estimate_buzz(w, h, steps, 1)
        extra = [] if r.img2img else ["txt2img only"]
        if r.nsfw != "strong":
            extra.append(f"nsfw {r.nsfw}")
        tail = f" ({', '.join(extra)})" if extra else ""
        line = f"- {r.key}: {r.label}, ~{est:.0f} Buzz{tail}"
        lines.append(f"{line}. {r.notes}" if notes else line)
    open_ = [r for r in EXTRA_RECIPES if r.engine == "comfy"]
    hosted = [r for r in EXTRA_RECIPES if r.engine != "comfy"]
    lines.append(
        "- Other families (text-to-image only, no LoRAs, priced by Civitai): "
        + ", ".join(f"{r.key}{'*' if r.nsfw == 'strong' else ''}" for r in open_)
        + ". * = uncensored, fine for explicit."
    )
    lines.append(
        "- Hosted engines (SFW only, their own filters block nudity): "
        + ", ".join(r.key for r in hosted) + "."
    )
    if notes:
        lines.extend(f"  - {r.key}: {r.label}. {r.notes}".rstrip(". ") + "."
                     for r in EXTRA_RECIPES)
    return "\n".join(lines)
