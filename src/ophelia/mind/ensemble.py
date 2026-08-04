"""
Neuro-style multi-mind ensemble — architecture + shipped layers.

Ensemble v0: ProviderStack roles (chat, consciousness, vision, …) — separate
models, shared memory, one model gate.

Shipped decision / specialist layers:
  - Director (`OPHELIA_DIRECTOR`) — speak / react / defer / skip
  - VoiceMind (`OPHELIA_VOICE_MIND_MODE`) — TTS rewrite
  - ReactionMind (`OPHELIA_REACTION`) — fast one-liner when director picks react

Planned (not wired as live specialists yet): filter, music, full avatar bridge.
See docs/neuro-ensemble.md.
"""

from __future__ import annotations

from typing import Literal

# Roles wired in ProviderStack today (see ophelia.providers.router.ProviderRole)
ActiveRole = Literal[
    "chat",
    "consciousness",
    "reaction",
    "vision",
    "curator",
    "image",
    "video",
]

# Specialist minds (app layers; reaction also has a ProviderStack role).
# `avatar` has a partial bridge (psyche → Live2D / VRoid / VRChat params).
PlannedMind = Literal[
    "director",
    "filter",
    "voice",
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

# App-layer specialists that are live (not necessarily separate ProviderRoles).
SHIPPED_SPECIALISTS: tuple[str, ...] = (
    "director",
    "voice",
    "reaction",
)

PLANNED_MINDS: tuple[PlannedMind, ...] = (
    "director",
    "filter",
    "voice",
    "avatar",
    "music",
)

ENSEMBLE_DOC = "docs/neuro-ensemble.md"
