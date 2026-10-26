"""One Civitai generation, start to finish.

plan_generation() decides everything that will be sent: model (auto or
pinned), LoRAs (checked for family), prompt and negatives shaped for that
model, size/steps for the quality profile, image count. run_generation()
submits it, waits up to OPHELIA_CIVITAI_WAIT_SECONDS, saves every image and
logs one ledger line. Slower jobs are handed to the pending store and
delivered when Civitai finishes, so a paid image is never dropped.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog

from ophelia.config import Settings
from ophelia.providers.civitai import loras as auto_loras
from ophelia.providers.civitai import selection, site, variety
from ophelia.providers.civitai.air import (
    air_kind,
    base_family,
    ecosystem_from_air_or_base,
    is_air,
    lora_family_error,
    model_id,
    parse_loras,
    sanitize_air,
    version_id,
)
from ophelia.providers.civitai.catalog import (
    AUTO_RECIPE,
    QUALITIES,
    RECIPES,
    Recipe,
    recipe_by_name,
    recipe_for_air,
    render_settings,
)
from ophelia.providers.civitai.orchestration import (
    CivitaiClient,
    build_input,
    cost_of,
    failure_reason,
    output_images,
    resolve_image_url,
    status_of,
    TERMINAL_BAD,
    TERMINAL_OK,
)
from ophelia.providers.civitai.prompting import (
    detect_style_with_reason,
    format_prompt,
    minor_terms,
    negative_for,
)

log = structlog.get_logger()

# LoRA ecosystem → the recipe that can carry it (auto mode).
_LORA_ECO_RECIPE = {
    "anima": "anima", "zimage": "zimage-base", "qwen": "qwen", "flux1": "flux1",
    "flux2": "flux2-klein",
}
_LORA_FAMILY_RECIPE = {"illustrious": "illustrious", "pony": "pony", "sdxl": "lustify"}
_POLL_SCHEDULE = (2, 5, 10, 15)
_STALE_HINTS = ("not found", "does not exist", "unavailable", "deleted", "no longer")
_now = time.monotonic  # patched in tests


@dataclass
class GenerationRequest:
    prompt: str
    aspect_ratio: str = "1:1"
    nsfw: bool = False
    model: str = ""  # checkpoint AIR, alias/recipe name, or "" / "auto"
    quality: str = ""  # standard | high | max ("" = settings default)
    loras: Any = None
    negative_prompt: str | None = None
    image: str | None = None
    strength: float = 0.7
    count: int = 1
    deliver_to: str = ""  # channel for images that finish after the wait


@dataclass
class Plan:
    recipe: Recipe
    prompt: str
    negative: str
    width: int
    height: int
    steps: int
    quality: str
    loras: dict[str, float]
    quantity: int
    strength: float
    notes: list[str] = field(default_factory=list)
    lora_triggers: list[str] = field(default_factory=list)
    error: str = ""
    fallback: Recipe | None = None  # the pool's head, if a rotated-in model is refused
    added: list[str] = field(default_factory=list)  # variety fill-ins
    auto_loras: list[str] = field(default_factory=list)  # LoRAs auto mode added
    raw_prompt: str = ""  # before shaping for the model
    aspect: str = "1:1"
    nsfw: bool = False
    custom_negative: bool = False

    def retarget(self, recipe: Recipe) -> None:
        """Shape this plan for another model (the pool head after a refusal)."""
        if recipe.family != self.recipe.family:
            for air in self.auto_loras:
                self.loras.pop(air, None)
        self.recipe = recipe
        self.prompt = format_prompt(recipe, self.raw_prompt, nsfw=self.nsfw)
        if not self.custom_negative:
            self.negative = negative_for(recipe, nsfw=self.nsfw) if recipe.negative else ""
        self.width, self.height, self.steps = render_settings(recipe, self.quality, self.aspect)

    @property
    def est_buzz(self) -> float:
        return round(
            self.recipe.estimate_buzz(self.width, self.height, self.steps, self.quantity), 1
        )


def _checkpoint_overrides(settings: Settings) -> dict[str, str]:
    raw = getattr(settings, "civitai_checkpoints", "") or ""
    out: dict[str, str] = {}
    for part in raw.split(","):
        style, _, air = part.partition("=")
        if style.strip() and is_air(air):
            out[style.strip().lower()] = sanitize_air(air)
    return out


async def _meta(settings: Settings, air: str) -> site.CivitaiResource | None:
    vid = version_id(air)
    if vid is None:
        return None
    try:
        return await site.get_version(settings, vid)
    except Exception as e:
        log.debug("civitai.meta_failed", air=air, error=str(e))
        return None


async def plan_generation(settings: Settings, req: GenerationRequest) -> Plan:
    from ophelia.providers.media import normalize_aspect_ratio

    notes: list[str] = []
    if req.nsfw and (bad := minor_terms(req.prompt)):
        return _refusal("explicit images are of adults only; the prompt describes a minor ("
                        + ", ".join(bad) + "). Rewrite it with adult characters.")
    loras = {sanitize_air(k): v for k, v in parse_loras(req.loras).items() if sanitize_air(k)}
    pin = sanitize_air((req.model or "").strip())
    if pin.lower() in ("auto", "pick", "dynamic", "flux"):
        pin = ""
    # A LoRA is never sent as the model: move it to loras=.
    if pin and air_kind(pin) == "lora":
        loras.setdefault(pin, 0.8)
        notes.append("model was a LoRA, moved to loras")
        pin = ""

    lora_base: dict[str, str] = {}
    triggers: list[str] = []
    for air in list(loras)[:5]:
        meta = await _meta(settings, air)
        if meta:
            lora_base[air] = meta.base_model or ""
            triggers.extend(t for t in meta.trained_words[:3] if t)

    fallback: Recipe | None = None
    if pin and is_air(pin):
        meta = await _meta(settings, pin)
        recipe = recipe_for_air(pin, meta.base_model if meta else "")
        notes.append(f"pinned {recipe.label}")
    elif pin:
        named = recipe_by_name(pin)
        if not named:
            return _refusal(f"unknown model {pin!r}. Use a recipe name ({', '.join(RECIPES)}) "
                            "or a checkpoint AIR from search_civitai_models.")
        recipe = named
        notes.append(f"recipe {recipe.key}")
    else:
        recipe, fallback = _auto_recipe(settings, req, loras, lora_base, notes)

    if req.image and not recipe.img2img:
        if pin:
            return _refusal(f"{recipe.label} can't edit an image (text-to-image only). "
                            "Pick an SDXL model (illustrious, pony, lustify) for img2img.")
        swap = RECIPES["lustify" if recipe.style == "realistic" else "illustrious"]
        notes.append(f"{recipe.key} has no img2img, using {swap.key}")
        recipe, fallback = swap, None

    if loras and not recipe.loras:
        return _refusal(f"{recipe.label} doesn't take LoRAs.")

    prompt = req.prompt
    found: dict[str, float] = {}
    if not loras and not pin and req.nsfw and not req.image:
        found, trig, lora_notes = await auto_loras.pick_loras(settings, recipe, prompt)
        if found:
            loras.update(found)
            lora_base.update({a: recipe.base_model for a in found})
            low = prompt.lower()
            fresh = [t for t in trig if t.lower() not in low]
            if fresh:
                prompt = ", ".join(fresh) + ", " + prompt
        notes.extend(lora_notes)
    added: list[str] = []
    if not req.image and variety.enabled(settings):
        prompt, added = variety.add_variety(settings, prompt,
                                            natural=recipe.prompt_style == "natural")
        if added:
            notes.append("variety: " + ", ".join(added))
    ck_air = recipe.model if is_air(recipe.model) else ""
    mismatches = [
        err for air in loras
        if (err := lora_family_error(ck_air, recipe.base_model, air, lora_base.get(air, ""),
                                     ecosystem=recipe.ecosystem if not ck_air else ""))
    ]
    if mismatches:
        return _refusal(
            "LoRA/checkpoint family mismatch: " + "; ".join(mismatches)
            + ". Use LoRAs of the model's family, or pin a matching model "
            "(model='pony' or model='illustrious'), or leave model empty so the "
            "model follows the LoRA.",
            recipe,
        )

    quality = (req.quality or getattr(settings, "civitai_quality", "") or "standard").lower()
    if quality not in QUALITIES:
        quality = "standard"
    aspect = normalize_aspect_ratio(req.aspect_ratio)
    width, height, steps = render_settings(recipe, quality, aspect)
    max_count = max(1, int(getattr(settings, "civitai_max_count", 4) or 1))
    quantity = max(1, min(int(req.count or 1), recipe.max_quantity, max_count))
    try:
        strength = min(1.0, max(0.05, float(req.strength)))
    except (TypeError, ValueError):
        strength = 0.7
    custom_negative = (req.negative_prompt or "").strip()
    negative = custom_negative or negative_for(recipe, nsfw=req.nsfw)
    return Plan(
        recipe=recipe,
        prompt=format_prompt(recipe, prompt, nsfw=req.nsfw),
        negative=negative if recipe.negative else "",
        width=width, height=height, steps=steps, quality=quality,
        loras=loras, quantity=quantity, strength=strength,
        notes=notes, lora_triggers=triggers, fallback=fallback, added=added,
        auto_loras=list(found), raw_prompt=prompt, aspect=aspect, nsfw=req.nsfw,
        custom_negative=bool(custom_negative),
    )


def _auto_recipe(
    settings: Settings,
    req: GenerationRequest,
    loras: dict[str, float],
    lora_base: dict[str, str],
    notes: list[str],
) -> tuple[Recipe, Recipe | None]:
    """(recipe, the pool head to fall back to if a rotated-in one is refused)."""
    # LoRAs decide the model family (a Pony LoRA gets the Pony checkpoint).
    ecos = {ecosystem_from_air_or_base(a) for a in loras}
    fams = {base_family(b) for b in lora_base.values() if b}
    key = ""
    if len(ecos) == 1 and next(iter(ecos)) in _LORA_ECO_RECIPE:
        key = _LORA_ECO_RECIPE[next(iter(ecos))]
    elif len(fams) == 1 and next(iter(fams)) in _LORA_FAMILY_RECIPE:
        key = _LORA_FAMILY_RECIPE[next(iter(fams))]
    family = ""
    if key:
        notes.append(f"{key} (follows the LoRA family)")
        style = RECIPES[key].style
        family = RECIPES[key].family
    else:
        style, matched = detect_style_with_reason(req.prompt)
        if style == "sdxl":
            style = _default_style(settings)
            matched = ""
        key = AUTO_RECIPE.get(style, "illustrious")
        notes.append(
            f"{key} (style={style}, " + (f"matched {matched!r})" if matched else "default)")
        )
    recipe = RECIPES[key]
    head: Recipe | None = None
    override = _checkpoint_overrides(settings).get(recipe.style) or \
        _checkpoint_overrides(settings).get(style)
    if override and recipe.ecosystem == "sdxl":
        recipe = replace(recipe, model=override, key=f"{recipe.key}*", label=override)
        notes.append(f"checkpoint override {override}")
    elif recipe.pool:
        picked, why = selection.pick(settings, recipe.pool, recipe, family=family)
        if why:
            head, recipe = recipe, picked
            notes.append(f"{picked.key} ({why})")
    log.info("civitai.auto_pick", recipe=recipe.key, model=recipe.model or recipe.ecosystem,
             loras=list(loras) or None, note=notes[-1])
    return recipe, head


def _default_style(settings: Settings) -> str:
    """Style for a prompt with no style words: the setting, or "mix" for
    anime or realistic by turns."""
    style = (getattr(settings, "civitai_default_style", "") or "anime")
    style = str(style).strip().lower()
    if style in ("mix", "mixed", "random", "any"):
        return random.choices(("anime", "realistic"), weights=(3, 2))[0]
    return style


def _refusal(msg: str, recipe: Recipe | None = None) -> Plan:
    return Plan(
        recipe=recipe or RECIPES["illustrious"], prompt="", negative="", width=0, height=0,
        steps=0, quality="standard", loras={}, quantity=0, strength=0.7, error=msg,
    )


# --- ledger + saving --------------------------------------------------------


def ledger_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / "logs" / "image_generations.jsonl"


def write_ledger(settings: Settings, record: dict) -> None:
    """One line per generation (success, failure or pending): what was sent
    and what came back. Never raises."""
    try:
        path = ledger_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), **record}, default=str) + "\n")
    except Exception as e:
        log.debug("image.ledger_failed", error=str(e))


def _save(content: bytes, artifacts_dir: Path, seed: Any, index: int) -> Path:
    from ophelia.providers.media import _image_ext_from_bytes

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stem = f"civitai_{int(time.time())}_{seed or 'x'}_{index}"
    out = artifacts_dir / f"{stem}{_image_ext_from_bytes(content)}"
    n = 1
    while out.exists():
        out = artifacts_dir / f"{stem}-{n}{out.suffix}"
        n += 1
    out.write_bytes(content)
    return out


async def collect_images(
    settings: Settings, client: CivitaiClient, wf: dict, artifacts_dir: Path, seed: Any
) -> tuple[list[Path], int, str]:
    """Download every finished image. (paths, total bytes, error)."""
    images = output_images(wf)
    urls = [img["url"] for img in images if isinstance(img.get("url"), str) and img["url"]]
    if not urls:
        return [], 0, f"Civitai finished but returned no image URL: {failure_reason(wf)}"
    paths: list[Path] = []
    total = 0
    errors: list[str] = []
    for i, url in enumerate(urls):
        dl = await client.download(url)
        if dl.status_code >= 400 or len(dl.content or b"") < 100:
            errors.append(f"download {i + 1} failed HTTP {dl.status_code} ({len(dl.content or b'')} bytes)")
            continue
        paths.append(_save(dl.content, artifacts_dir, seed, i))
        total += len(dl.content)
    return paths, total, "; ".join(errors)


# --- run --------------------------------------------------------------------


def _summary(plan: Plan, record: dict, paths: list[Path], note: str = "") -> str:
    head = "\n".join(f"Image saved to {p}" for p in paths)
    bits = [f"civitai {record.get('operation')}", f"model={plan.recipe.key} ({record.get('model')})"]
    if plan.quality != "standard":
        bits.append(f"quality={plan.quality}")
    if plan.loras:
        bits.append("loras=" + ", ".join(f"{k}:{v}" for k, v in plan.loras.items()))
    if record.get("strength") is not None:
        bits.append(f"strength={record['strength']}")
    if record.get("seed") is not None:
        bits.append(f"seed={record['seed']}")
    bits.append(f"~{plan.est_buzz} Buzz")
    if plan.notes:
        bits.append("picked: " + "; ".join(plan.notes))
    missing = [t for t in plan.lora_triggers if t.lower() not in plan.prompt.lower()]
    if missing:
        bits.append("LoRA trigger words not in your prompt: " + ", ".join(missing[:6]))
    if note:
        bits.append(note)
    return f"{head} [" + "; ".join(bits) + "]"


async def run_generation(
    settings: Settings, req: GenerationRequest, artifacts_dir: Path
) -> str:
    plan = await plan_generation(settings, req)
    record: dict[str, Any] = {
        "provider": "civitai",
        "recipe": plan.recipe.key,
        "model": plan.recipe.model or plan.recipe.ecosystem,
        "loras": dict(plan.loras),
        "prompt": plan.prompt or req.prompt,
        "nsfw": req.nsfw,
        "pick": "; ".join(plan.notes) or None,
        "variety": plan.added or None,
    }
    if plan.error:
        msg = f"Refused: {plan.error}"
        write_ledger(settings, {**record, "error": msg})
        log.warning("civitai.refused", error=plan.error)
        return msg

    image_url = None
    if req.image:
        record.update(strength=plan.strength, source_image=req.image)
        try:
            image_url = await resolve_image_url(settings, req.image)
        except Exception as e:
            msg = f"Civitai img2img source failed: {e}"
            write_ledger(settings, {**record, "error": msg})
            return msg

    seed: int | None = random.randint(1, 2**32 - 1)
    recipe = plan.recipe

    def body_for(r: Recipe, s: int | None) -> dict:
        inp = build_input(
            r, prompt=plan.prompt, width=plan.width, height=plan.height, steps=plan.steps,
            negative=plan.negative, loras=plan.loras or None, quantity=plan.quantity,
            seed=s, image_url=image_url, strength=plan.strength,
        )
        body: dict[str, Any] = {"steps": [{"$type": "imageGen", "input": inp}]}
        if req.nsfw:
            # Mature content forces yellow Buzz, so only explicit requests ask.
            body["allowMatureContent"] = True
        return body

    body = body_for(recipe, seed)
    inp = body["steps"][0]["input"]
    record.update(
        operation=inp.get("operation"), width=plan.width, height=plan.height,
        steps=plan.steps, cfg=recipe.cfg, quality=plan.quality, quantity=plan.quantity,
        negative_prompt=plan.negative or None, seed=seed, est_buzz=plan.est_buzz,
    )
    client = CivitaiClient(settings)
    wait_total = max(30, int(getattr(settings, "civitai_wait_seconds", 180) or 180))
    started = _now()
    log.info("civitai.submit", **{k: v for k, v in record.items() if k != "prompt"},
             prompt=plan.prompt[:300])

    def fail(msg: str) -> str:
        log.warning("civitai.failed", error=msg[:300])
        write_ledger(settings, {**record, "error": msg})
        return msg

    r = await client.submit(body, wait=min(90, wait_total), mature=req.nsfw)
    if r.status_code == 400 and "seed" in r.text.lower():
        seed = None
        record["seed"] = None
        body = body_for(recipe, None)
        r = await client.submit(body, wait=min(90, wait_total), mature=req.nsfw)
    if r.status_code in (400, 404) and is_air(recipe.model) and \
            any(h in r.text.lower() for h in _STALE_HINTS):
        mid = model_id(recipe.model)
        newer = await site.latest_version_air(settings, mid, recipe.base_model) if mid else ""
        if newer and sanitize_air(newer) != recipe.model:
            log.warning("civitai.checkpoint_updated", old=recipe.model, new=newer)
            plan.notes.append(f"stored version gone, used {newer}")
            recipe = replace(recipe, model=sanitize_air(newer))
            record["model"] = recipe.model
            body = body_for(recipe, seed)
            r = await client.submit(body, wait=min(90, wait_total), mature=req.nsfw)
    if r.status_code in (400, 404, 422) and plan.fallback is not None \
            and "buzz" not in r.text.lower():
        # A rotated-in checkpoint Civitai won't run: bench it, use the pool's head.
        selection.bench(settings, recipe.model, f"HTTP {r.status_code}: {r.text[:200]}")
        plan.notes.append(f"{recipe.key} refused (HTTP {r.status_code}), "
                          f"used {plan.fallback.key}")
        recipe = plan.fallback
        plan.retarget(recipe)
        record.update(recipe=recipe.key, model=recipe.model, cfg=recipe.cfg,
                      loras=dict(plan.loras), prompt=plan.prompt, steps=plan.steps,
                      width=plan.width, height=plan.height, est_buzz=plan.est_buzz,
                      negative_prompt=plan.negative or None)
        body = body_for(recipe, seed)
        r = await client.submit(body, wait=min(90, wait_total), mature=req.nsfw)
    if r.status_code >= 400:
        return fail(f"Civitai submit failed HTTP {r.status_code}: {r.text[:300]}")

    wf = r.json()
    wf_id = wf.get("id") or wf.get("workflowId")
    record["workflow_id"] = wf_id
    status = status_of(wf)
    polls = 0
    while True:
        if status in TERMINAL_BAD:
            record["cost"] = cost_of(wf)
            return fail(f"Civitai workflow {status}: {failure_reason(wf)}")
        if status in TERMINAL_OK and any(img.get("url") for img in output_images(wf)):
            break
        remaining = wait_total - (_now() - started)
        if remaining <= 0 or not wf_id:
            break
        delay = _POLL_SCHEDULE[polls] if polls < len(_POLL_SCHEDULE) else 30
        await asyncio.sleep(min(delay, max(1.0, remaining)))
        polls += 1
        fresh = await client.get(wf_id)
        if fresh:
            wf = fresh
            status = status_of(wf)

    if not (status in TERMINAL_OK and any(img.get("url") for img in output_images(wf))):
        if not wf_id:
            return fail(f"Civitai returned no workflow id: {str(wf)[:300]}")
        from ophelia.providers.civitai.pending import PendingStore

        PendingStore(settings).add(
            workflow_id=wf_id, record=record, deliver_to=req.deliver_to,
            artifacts_dir=str(artifacts_dir), summary=_summary(plan, record, []),
        )
        write_ledger(settings, {**record, "pending": True})
        log.info("civitai.pending", workflow_id=wf_id, waited=round(_now() - started))
        return (
            f"Pending: still rendering on Civitai after {wait_total}s (workflow {wf_id}, "
            f"~{plan.est_buzz} Buzz). It'll be sent here automatically when it finishes, "
            "no need to generate it again."
        )

    paths, total, err = await collect_images(settings, client, wf, artifacts_dir, seed)
    record["cost"] = cost_of(wf)
    if not paths:
        return fail(err)
    record.update(paths=[str(p) for p in paths], path=str(paths[0]), bytes=total)
    write_ledger(settings, record)
    log.info("image.saved", provider="civitai", recipe=recipe.key, model=record["model"],
             loras=dict(plan.loras) or None, seed=seed, paths=record["paths"], bytes=total,
             est_buzz=plan.est_buzz, cost=record["cost"])
    return _summary(plan, record, paths, note=err)
