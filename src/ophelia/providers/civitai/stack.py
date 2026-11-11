"""Putting several LoRAs together so they help each other instead of fighting.

Each LoRA pulls the image its own way; two that do the same job (two art
styles, missionary plus cowgirl) fight, and too much total weight fries the
picture (melted anatomy, burnt colours). The planner keeps at most one LoRA
per job that can only have one answer (style, position, outfit), gives each
the weight its creator recommends (or a sensible one for its kind), and
scales auto-added ones down when the stack gets heavy. Her own choices are
never dropped and her explicit weights are kept; she gets a note instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Weight when the creator gives none.
_DEFAULT = {"character": 0.85, "style": 0.6, "pose": 0.75, "clothing": 0.7,
            "concept": 0.7, "detail": 0.5, "other": 0.7}
# How many of each kind make sense in one picture.
_LIMIT = {"character": 2, "style": 1, "pose": 1, "clothing": 1, "concept": 2,
          "detail": 2, "other": 2}
MAX_LORAS = 5
BUDGET = 2.4  # total weight an SDXL picture takes before it starts to fry
_FLOOR = 0.35
_ORDER = {"hers": 0, "concept": 1, "style": 2}


@dataclass
class LoraPick:
    air: str
    name: str = ""
    category: str = "other"
    rec: float | None = None  # creator's recommended weight
    weight: float | None = None  # hers, explicit
    source: str = "auto"  # hers | concept | style
    group: str = ""  # concept group: position, fluid, effect... (one each)
    triggers: list[str] = field(default_factory=list)
    query: str = ""  # the concept it was found for

    @property
    def label(self) -> str:
        return self.name or self.air


def _base_weight(p: LoraPick) -> float:
    if p.weight is not None:
        return p.weight
    w = p.rec if p.rec is not None else _DEFAULT.get(p.category, 0.7)
    return max(0.4, min(1.0, w))


def plan_stack(picks: list[LoraPick]) -> tuple[list[LoraPick], dict[str, float], list[str]]:
    """(kept picks, {air: weight}, notes)."""
    notes: list[str] = []
    kept: list[LoraPick] = []
    count: dict[str, int] = {}
    groups: set[str] = set()
    seen: set[str] = set()
    for p in sorted(picks, key=lambda x: _ORDER.get(x.source, 3)):
        if p.air in seen:
            continue
        seen.add(p.air)
        cat = p.category if p.category in _LIMIT else "other"
        if p.source != "hers":
            if count.get(cat, 0) >= _LIMIT[cat]:
                notes.append(f"left out {p.label} (already a {cat} LoRA)")
                continue
            if p.group and p.group in groups:
                notes.append(f"left out {p.label} (clashes with another {p.group} LoRA)")
                continue
            if len(kept) >= MAX_LORAS:
                notes.append(f"left out {p.label} (enough LoRAs)")
                continue
        count[cat] = count.get(cat, 0) + 1
        if p.group:
            groups.add(p.group)
        kept.append(p)

    weights = {p.air: _base_weight(p) for p in kept}
    fixed = sum(w for p in kept if p.weight is not None for w in [weights[p.air]])
    free = [p for p in kept if p.weight is None]
    free_sum = sum(weights[p.air] for p in free)
    if free and fixed + free_sum > BUDGET:
        room = max(BUDGET - fixed, _FLOOR * len(free))
        scale = room / free_sum
        for p in free:
            weights[p.air] = max(_FLOOR, weights[p.air] * scale)
        notes.append(f"{len(kept)} LoRAs together: auto weights scaled to {scale:.2f}x")
    if fixed > BUDGET:
        notes.append(f"your LoRA weights add up to {fixed:.1f}; above ~{BUDGET} images "
                     "tend to fry, try 0.5-0.7 each")
    for (a, b) in _clashes(kept):
        notes.append(f"{a.label} and {b.label} are both {a.category} LoRAs and may fight")
    return kept, {air: round(w, 2) for air, w in weights.items()}, notes


def _clashes(kept: list[LoraPick]) -> list[tuple[LoraPick, LoraPick]]:
    """Pairs of hers that do the same single-answer job (two styles...)."""
    out = []
    for cat in ("style", "pose", "clothing"):
        same = [p for p in kept if p.category == cat and p.source == "hers"]
        if len(same) > 1:
            out.append((same[0], same[1]))
    return out
