"""Where an image request goes, decided in one place.

Before, the decision was spread over three files: the tool flipped nsfw on
or off and applied guest limits, generate_image rerouted for img2img, pose,
Civitai model names and censored backends, and the pipeline handled the
rest. Two steps now, both pure (no network) and tested on their own:

1. prepare_job(): who's asking. Guest limits; explicit prompts sent without
   nsfw=true go to the uncensored backend; suggestive-but-not-explicit ones
   try Grok first (Civitai only as a fallback).
2. route(): which backend and model, the fallbacks, or a refusal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from ophelia.providers.image_job import FAILED, REFUSED, ImageJob, ImageResult

log = structlog.get_logger()

# Censored backends: never send explicit prompts to these (they refuse and may
# flag the account). NSFW requests are routed away from them.
CENSORED = frozenset({"xai-oauth", "xai", "openai"})
# Backends that can edit a source image (img2img).
IMG2IMG = ("civitai", "xai", "xai-oauth")
# Tried in order when the primary fails (timeouts, filters, 5xx). Civitai
# first (real quality); free Pollinations only as the last resort.
FALLBACK_ORDER = ("civitai", "fal", "replicate", "modelslab", "ollama", "a1111", "comfyui",
                  "pollinations")
_NSFW_BACKENDS = "pollinations/a1111/comfyui/fal/replicate/civitai/modelslab/ollama"

_EXPLICIT_RE = re.compile(
    r"\b(?:nude|naked|nudity|topless|bottomless|nsfw|nipples?|areolas?|"
    r"genitals?|vagina|pussy|penis|cock|dick|cum|orgasm\w*|masturbat\w*|"
    r"fingering|erotic|lewd|hentai|spread (?:legs|pussy)|touching (?:herself|himself)|"
    r"explicit|uncensored|undressed|unclothed|"
    r"(?:no|without)\s+clothes|bare\s+(?:breasts?|chest)|exposed\s+breasts?|"
    r"porn\w*|sex(?:ual)?)\b",
    re.IGNORECASE,
)


def looks_explicit(prompt: str) -> bool:
    """True if an image prompt clearly asks for nudity/sexual content."""
    return bool(_EXPLICIT_RE.search(prompt or ""))


def prepare_job(settings: Any, job: ImageJob, *, is_owner: bool) -> ImageJob:
    """Adjust a request for who's asking."""
    nsfw_tier = getattr(settings, "image_nsfw_allowed", False) is True
    changes: dict[str, Any] = {}
    if not is_owner:
        # Guests: 1:1, one image, one try, standard quality, no pose control.
        # The experience is "see, I can make images", not paid wallpaper.
        changes.update(aspect_ratio="1:1", quality="standard", count=1, attempts=1,
                       pose_image=None)
    if not job.model:
        changes["auto_pick"] = True  # Civitai picks unless she pinned a model
    nsfw = job.nsfw
    # An explicit prompt sent without nsfw=true went to the censored backend,
    # which painted clothes back on and risks flagging the account.
    if not nsfw and is_owner and nsfw_tier and looks_explicit(job.prompt):
        nsfw = True
        log.info("generate_image.nsfw_auto_routed")
    # Grok first for anything that isn't actually explicit (lingerie, a torn
    # dress): those went to paid Civitai. A pinned Civitai model or an edit
    # keeps her choice; if Grok refuses, the fallback allows mature content.
    if nsfw and not looks_explicit(job.prompt) and not job.model and not job.image:
        nsfw = False
        log.info("generate_image.sfw_first", preview=job.prompt[:80])
    changes.update(nsfw=nsfw, fallback_mature=is_owner and nsfw_tier)
    return job.but(**changes)


@dataclass
class Route:
    job: ImageJob
    provider: str = ""
    model: str = ""
    auto_pick: bool = False
    fallbacks: list[str] = field(default_factory=list)
    refusal: ImageResult | None = None

    @property
    def refused(self) -> bool:
        return self.refusal is not None


def resolve_model(stack: Any, provider: str, agent_model: str, *, nsfw: bool,
                  auto_pick: bool) -> tuple[str, bool]:
    """(model to send, auto_pick) for one backend attempt."""
    if provider == "civitai":
        if not agent_model or agent_model.lower() in ("auto", "pick", "dynamic"):
            return "", True
        return agent_model, auto_pick
    resolved = agent_model or stack.image_model_for(provider, nsfw=nsfw)
    if (resolved or "").lower().startswith("urn:air:"):
        log.warning("image.stripped_air_from_non_civitai", provider=provider)
        resolved = stack.image_model_for(provider, nsfw=nsfw)
    return resolved, auto_pick


def fallback_providers(settings: Any, primary: str, *, nsfw: bool) -> list[str]:
    """Configured alternate backends to try when the primary fails."""
    out: list[str] = []
    for name in FALLBACK_ORDER:
        if name == primary or (nsfw and name in CENSORED):
            continue
        try:
            configured = settings.image_backend_configured(name)
        except Exception:
            continue
        if configured is True:  # exact True: stubs/mocks don't enable everything
            out.append(name)
    # Pollinations is usually zero-config: always the last resort for SFW
    # (and NSFW when allowed) unless it was already primary.
    if "pollinations" not in out and primary != "pollinations":
        if not nsfw or getattr(settings, "image_nsfw_allowed", False) is True:
            out.append("pollinations")
    return out


def _is_civitai_model(model: str) -> bool:
    from ophelia.providers.civitai.catalog import recipe_by_name

    return model.lower().startswith("urn:air:") or (
        bool(model) and recipe_by_name(model) is not None)


def route(settings: Any, stack: Any, job: ImageJob) -> Route:
    """Pick the backend and model for ``job``, with fallbacks, or refuse."""
    def refuse(text: str, status: str = REFUSED) -> Route:
        return Route(job=job, refusal=ImageResult(status=status, text=text))

    nsfw = job.nsfw
    if nsfw and not settings.image_nsfw_allowed:
        return refuse(
            "Refused: explicit image requested but OPHELIA_IMAGE_NSFW_ALLOWED is off. "
            "Enable it in ~/.ophelia/.env to route explicit prompts to an uncensored "
            f"backend ({_NSFW_BACKENDS}).")
    provider = stack.image_provider_for(nsfw=nsfw)
    # Never let an explicit prompt reach a censored backend, even when
    # OPHELIA_IMAGE_NSFW_PROVIDER is misconfigured to point at one.
    if nsfw and provider in CENSORED:
        return refuse(
            f"Refused: explicit image would route to {provider}, which refuses NSFW and "
            "may flag the account. Set OPHELIA_IMAGE_NSFW_PROVIDER to an uncensored "
            f"backend ({_NSFW_BACKENDS}).")
    has_civitai = bool(getattr(settings, "civitai_api_key", ""))

    # img2img: Grok edits SFW images, Civitai explicit ones. Other backends
    # would ignore the source image and draw from scratch.
    if job.image and provider not in IMG2IMG:
        if not has_civitai:
            return refuse(f"img2img needs Grok or Civitai; {provider} can't take a source "
                          "image. Call again without image= for a fresh text-to-image.",
                          FAILED)
        log.info("image.reroute_img2img_to_civitai", from_provider=provider)
        provider = "civitai"
    # Pose control only exists on Civitai (ControlNet on SDXL).
    if job.pose_image and provider != "civitai":
        if has_civitai:
            log.info("image.reroute_pose_to_civitai", from_provider=provider)
            provider = "civitai"
        else:
            job = job.but(pose_image=None)
    # A Civitai model (AIR or one of her recipe names) never goes to Grok:
    # it 404'd there and the fallback lost her choice.
    model = job.model
    if model and _is_civitai_model(model) and provider != "civitai":
        if has_civitai:
            log.info("image.reroute_air_to_civitai", from_provider=provider, model=model[:80])
            provider = "civitai"
        else:
            log.warning("image.ignored_civitai_urn", provider=provider, model=model[:80])
            model = ""
    resolved, auto_pick = resolve_model(stack, provider, model, nsfw=nsfw,
                                        auto_pick=job.auto_pick)
    # Free fallbacks for normal images only: never for an edit (it would
    # silently become text-to-image) or Civitai (a failed paid job is an
    # answer to report; the owner decides on retrying).
    fallbacks = [] if (job.image or provider == "civitai") else fallback_providers(
        settings, provider, nsfw=nsfw)
    return Route(job=job, provider=provider, model=resolved, auto_pick=auto_pick,
                 fallbacks=fallbacks)
