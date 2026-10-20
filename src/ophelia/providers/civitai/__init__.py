"""Civitai image generation (NSFW backend).

Modules:
  air           AIR URNs, model families, LoRA compatibility
  catalog       the models she can use (recipes), quality profiles, sizes, cost
  prompting     style detection, per-model quality/rating tags and negatives
  site          civitai.com site API: search, version metadata
  orchestration request bodies per model, submit/poll/download, blob upload
  pipeline      plan + run one generation, ledger
  pending       deliver jobs that finish after the chat wait
"""

from ophelia.providers.civitai.air import (
    air_kind,
    base_family,
    ecosystem_from_air_or_base,
    filter_loras_for_checkpoint,
    is_air,
    lora_compatible_with_checkpoint,
    lora_family_error,
    parse_loras,
    prompt_style_for,
    sanitize_air,
    version_id,
)
from ophelia.providers.civitai.catalog import (
    LUSTIFY_AIR,
    PONY_V6_AIR,
    RECIPES,
    SDXL_BASE_AIR,
    WAI_ILLUSTRIOUS_AIR,
    Recipe,
    catalog_text,
    recipe_by_name,
    recipe_for_air,
    render_settings,
    resolve_checkpoint_alias,
)
from ophelia.providers.civitai.prompting import (
    default_negative_for,
    detect_style,
    detect_style_with_reason,
    ensure_triggers_in_prompt,
    format_prompt,
    maybe_quality_prefix,
    negative_for,
    prompt_guide,
)
from ophelia.providers.civitai.site import (
    CivitaiResource,
    format_search_results,
    get_version,
    search_models,
)
from ophelia.providers.civitai.orchestration import (
    build_input,
    build_step_input,
    resolve_image_url,
    upload_local_image,
)
from ophelia.providers.civitai.pipeline import (
    GenerationRequest,
    plan_generation,
    run_generation,
)

__all__ = [
    "LUSTIFY_AIR", "PONY_V6_AIR", "RECIPES", "SDXL_BASE_AIR", "WAI_ILLUSTRIOUS_AIR",
    "CivitaiResource", "GenerationRequest", "Recipe", "air_kind", "base_family",
    "build_input", "build_step_input", "catalog_text", "default_negative_for",
    "detect_style", "detect_style_with_reason", "ecosystem_from_air_or_base",
    "ensure_triggers_in_prompt", "filter_loras_for_checkpoint", "format_prompt",
    "format_search_results", "get_version", "is_air", "lora_compatible_with_checkpoint",
    "lora_family_error", "maybe_quality_prefix", "negative_for", "parse_loras",
    "plan_generation", "prompt_guide", "prompt_style_for", "recipe_by_name",
    "recipe_for_air", "render_settings", "resolve_checkpoint_alias", "resolve_image_url",
    "run_generation", "sanitize_air", "search_models", "upload_local_image", "version_id",
]
