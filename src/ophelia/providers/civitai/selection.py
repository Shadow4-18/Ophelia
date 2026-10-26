"""Which checkpoint of a style's pool auto mode uses this time.

Each style (anime, pony, realistic) has a pool of strong checkpoints in
the catalog. Picks are weighted by the pool weight, by the owner's
reactions to past images (feedback.py), and away from the checkpoint used
for the last image, so consecutive requests don't all look alike. A
checkpoint Civitai rejected lately sits out for a week.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import structlog

from ophelia.providers.civitai import feedback
from ophelia.providers.civitai.catalog import Recipe, pool

log = structlog.get_logger()

_BENCH_DAYS = 7
_REPEAT_PENALTY = 0.35  # same checkpoint as the last image of this style
_RECENT_PENALTY = 0.7  # used in the few images before that


def _bench_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "civitai_benched.json"


def benched(settings: Any) -> dict[str, float]:
    try:
        data = json.loads(_bench_path(settings).read_text(encoding="utf-8"))
    except Exception:
        return {}
    cutoff = time.time() - _BENCH_DAYS * 86400
    return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > cutoff}


def bench(settings: Any, air: str, reason: str = "") -> None:
    """Keep a checkpoint Civitai refused out of rotation for a while."""
    try:
        data = benched(settings)
        data[air] = time.time()
        path = _bench_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        log.warning("civitai.benched", model=air, reason=reason[:200])
    except Exception as e:
        log.debug("civitai.bench_failed", error=str(e))


def _recent_models(settings: Any, pool_airs: set[str], n: int = 4) -> list[str]:
    """Checkpoints of this pool used for the last ``n`` images, newest first."""
    from ophelia.providers.civitai.pipeline import ledger_path

    try:
        lines = ledger_path(settings).read_text(encoding="utf-8").splitlines()[-200:]
    except Exception:
        return []
    out: list[str] = []
    for line in reversed(lines):
        try:
            model = json.loads(line).get("model")
        except Exception:
            continue
        if model in pool_airs:
            out.append(model)
            if len(out) >= n:
                break
    return out


def weights(settings: Any, style: str, family: str = "") -> list[tuple[Recipe, float]]:
    members = [r for r in pool(style) if not family or r.family == family]
    off = benched(settings)
    members = [r for r in members if r.model not in off] or members[:1]
    recent = _recent_models(settings, {r.model for r in members})
    scored = feedback.scores(settings)
    out = []
    for r in members:
        w = r.weight * feedback.multiplier(scored.get(r.model))
        if recent and r.model == recent[0]:
            w *= _REPEAT_PENALTY
        elif r.model in recent[1:]:
            w *= _RECENT_PENALTY
        out.append((r, w))
    return out


def pick(settings: Any, style: str, head: Recipe, *, family: str = "",
         rng: random.Random | None = None) -> tuple[Recipe, str]:
    """(recipe, why). ``head`` is the style's main recipe, used when the
    pool is empty or variety is off. ``family`` keeps to checkpoints a
    given LoRA family runs on."""
    mode = str(getattr(settings, "civitai_variety", "on") or "on").strip().lower()
    if mode in ("off", "false", "0", "no", "fixed"):
        return head, ""
    options = weights(settings, style, family)
    if len(options) < 2:
        return head, ""
    rng = rng or random.Random()
    total = sum(w for _, w in options)
    x = rng.uniform(0, total)
    chosen = options[-1][0]
    for r, w in options:
        x -= w
        if x <= 0:
            chosen = r
            break
    return chosen, "" if chosen.key == head.key else f"rotated in from the {style} pool"
