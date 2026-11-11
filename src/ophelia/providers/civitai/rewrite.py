"""Rewriting her prompt for the model that will actually draw it.

She writes the prompt before auto mode picks a model, so a sentence lands
on an anime checkpoint that only reads danbooru tags, or a tag pile lands
on Chroma/Flux which want sentences. When the prompt's shape doesn't match
the picked model, the chat model rewrites it into that model's format,
keeping every detail she asked for. The rewrite is checked before use (same
people, nothing refused, no minors); otherwise her prompt goes through as is.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

import structlog

from ophelia.providers.civitai.catalog import Recipe
from ophelia.providers.civitai.prompting import cast, minor_terms, split_tags

log = structlog.get_logger()

LLM = Callable[[str, str], Awaitable[str | None]]
_llm: LLM | None = None
_TIMEOUT = 25.0

_RULES = (
    "Keep every detail she asked for: each person's look, clothing or nudity, the act, "
    "pose, expression, setting, lighting, camera. Never add or remove people, never "
    "change anyone's gender or the act, never add a watermark, text or a person she didn't "
    "mention. Everyone is an adult. No quality words (masterpiece, best quality, score_*) "
    "and no negative words: those are added later. Reply with the prompt only, no quotes, "
    "no explanation."
)

SYSTEM = {
    "danbooru": (
        "You turn image requests into prompts for an anime SDXL model (Illustrious) that "
        "only understands danbooru tags. Output comma-separated danbooru tags in this "
        "order: count tags (1girl, 1boy, 2girls, hetero/yuri), then the act or main pose, "
        "then each person's features grouped together (hair, eyes, body, clothing), then "
        "expression, then camera (pov, from above, close-up), then setting and lighting. "
        "Use real danbooru tags (doggystyle, large breasts, thighhighs, sex from behind). "
        "Up to about 40 tags. " + _RULES
    ),
    "pony": (
        "You turn image requests into prompts for a Pony Diffusion SDXL model that reads "
        "danbooru tags. Output comma-separated danbooru tags: count tags (1girl, 1boy, "
        "hetero), the act or pose, each person's features grouped together, expression, "
        "camera, setting, lighting. Use real danbooru tags. Up to about 40 tags. " + _RULES
    ),
    "mixed": (
        "You turn image requests into prompts for a photorealistic SDXL model. Output a "
        "short photographic description followed by key tags, e.g. 'photo of a woman with "
        "long red hair kneeling on a bed, soft window light, 35mm, shallow depth of field, "
        "freckles, nude'. Put each person's features right next to them. Under 70 words. "
        + _RULES
    ),
    "natural": (
        "You turn image requests into prompts for a model that reads plain English "
        "(Chroma/Flux-style). Write 2 to 4 sentences: one sentence per person describing "
        "how they look, then one saying who does what to whom and the pose, then the "
        "setting, lighting and camera. Concrete and visual, no tag lists. Under 110 "
        "words. " + _RULES
    ),
}

_REFUSAL = re.compile(r"\b(i can'?t|i cannot|i won'?t|i'?m (sorry|unable)|as an ai|"
                      r"not able to (help|assist)|against (my|the) (policy|guidelines))\b",
                      re.I)


def install(agent: Any) -> None:
    """Use the agent's curator model (with its fallbacks) for rewrites."""
    async def ask(system: str, user: str) -> str | None:
        from ophelia.providers.fallback import call_with_fallback, extra_body_for
        from ophelia.providers.model_gate import get_model_gate

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def _make_call(cl, mdl, prov):
            return await cl.chat.completions.create(
                model=mdl, messages=messages, max_tokens=400,
                extra_body=extra_body_for(agent.settings, prov))

        resp = await call_with_fallback(
            agent.settings, agent.stack, role="curator",
            primary_provider=agent.stack.name("curator"),
            primary_model=agent._model("curator"),
            primary_client=await agent._client("curator"),
            make_call=_make_call, gate=get_model_gate(), log_tag="civitai.rewrite")
        return (resp.choices[0].message.content or "").strip()

    set_llm(ask)


def set_llm(fn: LLM | None) -> None:
    global _llm
    _llm = fn


def mode(settings: Any) -> str:
    """auto (default): rewrite when her prompt's shape doesn't fit the model |
    always | off."""
    v = str(getattr(settings, "civitai_prompt_rewrite", "auto") or "auto").strip().lower()
    if v in ("off", "false", "0", "no", "never"):
        return "off"
    return "always" if v in ("always", "on", "true", "1") else "auto"


def shape(prompt: str) -> str:
    """tags | sentences | short."""
    text = (prompt or "").strip()
    words = len(re.findall(r"[A-Za-z']+", text))
    if words < 5:
        return "short"
    parts = split_tags(text)
    avg = words / max(1, len(parts))
    sentence_marks = len(re.findall(r"\b(the|a|an|is|are|her|his|with|and|while|on|in)\b",
                                    text.lower()))
    if len(parts) >= 4 and avg <= 3.5:
        return "tags"
    if avg > 6 or sentence_marks / max(1, words) > 0.18:
        return "sentences"
    return "tags"


def needs_rewrite(settings: Any, recipe: Recipe, prompt: str) -> bool:
    m = mode(settings)
    if m == "off" or _llm is None:
        return False
    if m == "always":
        return True
    s = shape(prompt)
    if recipe.prompt_style in ("danbooru", "pony"):
        return s == "sentences"
    if recipe.prompt_style == "natural":
        return s == "tags"
    return False  # "mixed" models read both


def accept(original: str, rewritten: str) -> str:
    """Why the rewrite can't be used ("" when it can)."""
    if not rewritten or len(rewritten) < 8:
        return "empty"
    if _REFUSAL.search(rewritten[:200]):
        return "refused"
    if minor_terms(rewritten) and not minor_terms(original):
        return "added a minor"
    w0, m0 = cast(original)
    w1, m1 = cast(rewritten)
    if (w0 or m0) and (w0, m0) != (w1, m1):
        return f"changed who's in it ({w0}w{m0}m → {w1}w{m1}m)"
    if len(rewritten) > 1500:
        return "too long"
    return ""


async def rewrite(settings: Any, recipe: Recipe, prompt: str, *,
                  nsfw: bool) -> tuple[str, str]:
    """(prompt for the model, note). Her prompt unchanged when no rewrite is
    needed or the rewrite fails the checks."""
    if not needs_rewrite(settings, recipe, prompt):
        return prompt, ""
    style = recipe.prompt_style if recipe.prompt_style in SYSTEM else "mixed"
    user = (f"Model: {recipe.label}.\n" + ("This is an explicit adult image; keep it "
            "explicit.\n" if nsfw else "") + f"Request: {prompt.strip()}")
    try:
        out = await asyncio.wait_for(_llm(SYSTEM[style], user), timeout=_TIMEOUT)
    except Exception as e:
        log.info("civitai.rewrite_failed", error=str(e)[:200])
        return prompt, ""
    out = (out or "").strip().strip('"').strip("'").strip()
    out = re.sub(r"^(prompt|tags)\s*:\s*", "", out, flags=re.I).strip()
    why = accept(prompt, out)
    if why:
        log.info("civitai.rewrite_rejected", why=why, out=out[:200])
        return prompt, f"rewrite skipped ({why})"
    log.info("civitai.rewrite", style=style, before=prompt[:160], after=out[:200])
    return out, f"rewrote the prompt as {'tags' if style in ('danbooru', 'pony') else 'a description' if style == 'mixed' else 'sentences'} for {recipe.key}"
