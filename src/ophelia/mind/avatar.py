"""Avatar bridge — psyche → Live2D / VTuber expression parameters.

Maps Ophelia's mood, drives, and speaking state into a stable parameter
bus the workstation UI (and a future VTube Studio / Cubism client) can
consume. No Cubism SDK is bundled; the browser procedural stage uses these
params directly, and a real `.model3.json` can ride the same bus.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ophelia.config import OPHELIA_HOME

# Live2D Cubism-style ids (subset used by most models + our procedural stage).
PARAM_ANGLE_X = "ParamAngleX"
PARAM_ANGLE_Y = "ParamAngleY"
PARAM_ANGLE_Z = "ParamAngleZ"
PARAM_EYE_L_OPEN = "ParamEyeLOpen"
PARAM_EYE_R_OPEN = "ParamEyeROpen"
PARAM_EYE_BALL_X = "ParamEyeBallX"
PARAM_EYE_BALL_Y = "ParamEyeBallY"
PARAM_BROW_L_Y = "ParamBrowLY"
PARAM_BROW_R_Y = "ParamBrowRY"
PARAM_MOUTH_OPEN_Y = "ParamMouthOpenY"
PARAM_MOUTH_FORM = "ParamMouthForm"
PARAM_BODY_ANGLE_X = "ParamBodyAngleX"
PARAM_BREATH = "ParamBreath"


ExpressionId = str  # happy | sad | angry | shy | surprised | thinking | neutral | sleepy | curious


@dataclass
class AvatarState:
    """Snapshot pushed over WebSocket /api/avatar."""

    expression: ExpressionId = "neutral"
    speaking: bool = False
    mouth_open: float = 0.0  # 0..1
    blink: float = 1.0  # 1 = open, 0 = closed
    params: dict[str, float] = field(default_factory=dict)
    label: str = "neutral"
    valence: float = 0.0
    arousal: float = 0.3
    backend: str = "procedural"  # procedural | live2d
    model_url: str | None = None
    model_ready: bool = False
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_avatar_dir(settings_avatar_dir: Path | None = None) -> Path:
    return Path(settings_avatar_dir) if settings_avatar_dir else (OPHELIA_HOME / "avatar")


def find_model3(avatar_dir: Path, configured: str | None = None) -> Path | None:
    """Locate a Cubism model3.json under the avatar directory."""
    root = resolve_avatar_dir(avatar_dir)
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = root / configured
        if candidate.is_file():
            return candidate
    if not root.is_dir():
        return None
    direct = root / "model.model3.json"
    if direct.is_file():
        return direct
    matches = sorted(root.rglob("*.model3.json"))
    return matches[0] if matches else None


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def expression_from_mood(
    *,
    label: str,
    valence: float,
    arousal: float,
    feelings: list[str] | None = None,
    boredom: float = 0.0,
    curiosity: float = 0.0,
) -> ExpressionId:
    """Pick a named expression from psyche signals."""
    text = " ".join([label or "", *(feelings or [])]).lower()
    if any(k in text for k in ("angry", "annoy", "irritat", "frustrat")):
        return "angry"
    if any(k in text for k in ("shy", "embarrass", "fluster", "blush")):
        return "shy"
    if any(k in text for k in ("surpris", "shock", "wow", "startl")):
        return "surprised"
    if any(k in text for k in ("think", "ponder", "curios", "wonder")):
        return "thinking"
    if any(k in text for k in ("sleep", "tired", "drowsy", "bored")) or boredom > 0.72:
        return "sleepy"
    if curiosity > 0.7 and valence >= 0:
        return "curious"
    if any(k in text for k in ("sad", "lonely", "melanch", "hurt", "down")) or valence < -0.35:
        return "sad"
    if any(k in text for k in ("happy", "joy", "excit", "warm", "fond", "play")) or (
        valence > 0.35 and arousal > 0.35
    ):
        return "happy"
    if valence > 0.2:
        return "happy"
    if arousal < 0.2 and boredom > 0.45:
        return "sleepy"
    return "neutral"


def params_from_psyche(
    *,
    label: str = "neutral",
    valence: float = 0.0,
    arousal: float = 0.3,
    feelings: list[str] | None = None,
    boredom: float = 0.0,
    curiosity: float = 0.0,
    social: float = 0.5,
    expressiveness: float = 0.5,
    speaking: bool = False,
    mouth_open: float = 0.0,
    t: float | None = None,
) -> tuple[ExpressionId, dict[str, float]]:
    """Build Live2D-style parameters from psyche + speaking state."""
    now = t if t is not None else time.time()
    expr = expression_from_mood(
        label=label,
        valence=valence,
        arousal=arousal,
        feelings=feelings,
        boredom=boredom,
        curiosity=curiosity,
    )

    breath = 0.5 + 0.5 * math.sin(now * (1.2 + arousal * 1.4))
    # Subtle idle sway
    sway = math.sin(now * 0.55) * (4.0 + arousal * 6.0)
    nod = math.sin(now * 0.35) * (2.0 + boredom * 3.0)

    mouth_form = _clamp(valence * 0.85 + (0.25 if expr == "happy" else 0.0))
    if expr == "sad":
        mouth_form = _clamp(min(mouth_form, -0.35))
    if expr == "angry":
        mouth_form = _clamp(min(mouth_form, -0.15))

    brow = 0.0
    if expr in ("angry", "thinking"):
        brow = -0.45 if expr == "angry" else 0.25
    elif expr == "surprised":
        brow = 0.55
    elif expr == "sad":
        brow = -0.25
    elif expr == "curious":
        brow = 0.2

    eye_open = 1.0
    if expr == "sleepy" or boredom > 0.65:
        eye_open = max(0.35, 1.0 - boredom * 0.55)
    if expr == "surprised":
        eye_open = 1.0

    angle_z = 0.0
    if expr == "shy":
        angle_z = -8.0
    elif expr == "curious":
        angle_z = 6.0 * math.sin(now * 0.4)
    elif curiosity > 0.55:
        angle_z = 4.0

    body_x = _clamp((social - 0.5) * 8.0, -10.0, 10.0)

    open_y = _clamp(mouth_open if speaking else 0.0, 0.0, 1.0)
    if speaking and open_y < 0.08:
        # Soft idle chatter envelope when speaking but no sample yet
        open_y = 0.15 + 0.35 * abs(math.sin(now * 12.0))

    intensity = 0.55 + 0.45 * _clamp(expressiveness, 0.0, 1.0)
    params = {
        PARAM_ANGLE_X: sway * intensity,
        PARAM_ANGLE_Y: nod * 0.6 - (3.0 if expr == "shy" else 0.0),
        PARAM_ANGLE_Z: angle_z * intensity,
        PARAM_EYE_L_OPEN: eye_open,
        PARAM_EYE_R_OPEN: eye_open,
        PARAM_EYE_BALL_X: math.sin(now * 0.25) * 0.15,
        PARAM_EYE_BALL_Y: math.cos(now * 0.2) * 0.1,
        PARAM_BROW_L_Y: brow,
        PARAM_BROW_R_Y: brow,
        PARAM_MOUTH_OPEN_Y: open_y,
        PARAM_MOUTH_FORM: mouth_form,
        PARAM_BODY_ANGLE_X: body_x,
        PARAM_BREATH: breath,
    }
    return expr, params


def mouth_envelope(text: str, elapsed: float, *, cps: float = 14.0) -> float:
    """Approximate lip-sync mouth open from spoken text + elapsed seconds."""
    clean = "".join(ch for ch in (text or "") if ch.isalnum() or ch.isspace())
    if not clean.strip():
        return 0.0
    duration = max(0.35, len(clean) / max(cps, 1.0))
    if elapsed < 0 or elapsed > duration + 0.25:
        return 0.0
    # Syllable-ish oscillation gated by a fade envelope
    progress = elapsed / duration
    fade = 1.0
    if progress < 0.08:
        fade = progress / 0.08
    elif progress > 0.85:
        fade = max(0.0, (1.0 - progress) / 0.15)
    wave = 0.35 + 0.55 * abs(math.sin(elapsed * 14.0 + len(clean) * 0.01))
    # Vowels open wider
    idx = min(len(clean) - 1, int(progress * len(clean)))
    ch = clean[idx].lower()
    vowel_boost = 0.25 if ch in "aeiou" else 0.0
    return _clamp(wave * fade + vowel_boost, 0.0, 1.0)


class AvatarBridge:
    """Stateful avatar controller for the workstation (and future stream outs)."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        avatar_dir: Path | None = None,
        model_path: str | None = None,
        backend: str = "auto",
    ) -> None:
        self.enabled = enabled
        self.avatar_dir = resolve_avatar_dir(avatar_dir)
        self.model_path = model_path
        self.backend_pref = (backend or "auto").lower()
        self._speaking = False
        self._speak_text = ""
        self._speak_started = 0.0
        self._mouth = 0.0
        self._last: AvatarState | None = None

    def model_file(self) -> Path | None:
        return find_model3(self.avatar_dir, self.model_path)

    def resolved_backend(self) -> str:
        if self.backend_pref == "live2d":
            return "live2d"
        if self.backend_pref == "procedural":
            return "procedural"
        return "live2d" if self.model_file() else "procedural"

    def model_url(self) -> str | None:
        path = self.model_file()
        if not path:
            return None
        try:
            rel = path.relative_to(self.avatar_dir)
        except ValueError:
            rel = path.name
        return f"/avatar/{rel.as_posix()}"

    def begin_speak(self, text: str = "") -> None:
        self._speaking = True
        self._speak_text = text or ""
        self._speak_started = time.time()
        self._mouth = 0.2

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def end_speak(self) -> None:
        self._speaking = False
        self._speak_text = ""
        self._mouth = 0.0

    def set_mouth(self, value: float) -> None:
        self._mouth = _clamp(float(value), 0.0, 1.0)

    def tick_mouth(self) -> float:
        if not self._speaking:
            self._mouth = 0.0
            return 0.0
        if self._speak_text:
            self._mouth = mouth_envelope(
                self._speak_text, time.time() - self._speak_started
            )
            if self._mouth <= 0.0 and (time.time() - self._speak_started) > 0.5:
                # Finished approximate utterance
                self.end_speak()
        return self._mouth

    def snapshot(
        self,
        *,
        label: str = "neutral",
        valence: float = 0.0,
        arousal: float = 0.3,
        feelings: list[str] | None = None,
        boredom: float = 0.0,
        curiosity: float = 0.0,
        social: float = 0.5,
        expressiveness: float = 0.5,
    ) -> AvatarState:
        mouth = self.tick_mouth()
        expr, params = params_from_psyche(
            label=label,
            valence=valence,
            arousal=arousal,
            feelings=feelings,
            boredom=boredom,
            curiosity=curiosity,
            social=social,
            expressiveness=expressiveness,
            speaking=self._speaking,
            mouth_open=mouth,
        )
        backend = self.resolved_backend()
        model_url = self.model_url()
        state = AvatarState(
            expression=expr,
            speaking=self._speaking,
            mouth_open=mouth,
            blink=params.get(PARAM_EYE_L_OPEN, 1.0),
            params=params,
            label=label or expr,
            valence=valence,
            arousal=arousal,
            backend=backend,
            model_url=model_url,
            model_ready=bool(model_url) and backend == "live2d",
            updated_at=time.time(),
        )
        self._last = state
        return state

    def last(self) -> AvatarState | None:
        return self._last
