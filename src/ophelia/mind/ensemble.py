"""
Neuro-style multi-mind ensemble — future architecture stub.

Today Ophelia uses ProviderStack roles (chat, consciousness, vision, …) as
ensemble v0: separate models, shared memory, one model gate.

When OPHELIA_ENSEMBLE_ENABLED is implemented, a Director will coordinate
PLANNED_MINDS alongside existing LLM roles. Nothing here runs yet.
"""

from __future__ import annotations

from typing import Literal

# Roles wired today (see ophelia.providers.router.ProviderRole)
ActiveRole = Literal[
    "chat",
    "consciousness",
    "vision",
    "curator",
    "image",
    "video",
]

# Minds to add for streaming / Neuro parity.
# `avatar` has a partial bridge (psyche → Live2D params + workstation stage);
# full VTube Studio / Cubism runtime remains planned.
PlannedMind = Literal[
    "director",
    "filter",
    "voice",
    "reaction",
    "avatar",
    "music",
]

EnsembleRole = ActiveRole | PlannedMind

# Priority when model gate is contended (lower = sooner). Director overrides later.
DEFAULT_PRIORITY: dict[EnsembleRole, int] = {
    "reaction": 0,
    "chat": 1,
    "filter": 2,
    "voice": 3,
    "vision": 4,
    "consciousness": 5,
    "director": 6,
    "image": 7,
    "video": 8,
    "curator": 9,
    "avatar": 10,
    "music": 11,
}

PLANNED_MINDS: tuple[PlannedMind, ...] = (
    "director",
    "filter",
    "voice",
    "reaction",
    "avatar",
    "music",
)

ENSEMBLE_DOC = "docs/neuro-ensemble.md"
