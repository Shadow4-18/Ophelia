"""Avatar bridge — psyche → Live2D / VRoid / VRChat / VTuber expression parameters.

Maps Ophelia's mood, drives, and speaking state into a stable parameter
bus the workstation UI can consume:

- Live2D Cubism ids (ParamMouthOpenY, …) for 2D models / procedural stage
- VRM 1.0 expression weights for VRoid Studio exports (.vrm)
- VRChat-style morph / viseme weights for FBX (and glTF) humanoid exports

No Cubism SDK is bundled. VRM / FBX / glTF load in-browser via three.js
(+ three-vrm for .vrm). Native VRChat .vrca AssetBundles are not loadable in
the browser — use the avatar's FBX (or UniVRM → .vrm).
"""

from __future__ import annotations

import math
import re
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

# VRM 1.0 preset expression names (VRoid Studio exports these).
VRM_PRESETS = (
    "happy",
    "angry",
    "sad",
    "relaxed",
    "surprised",
    "aa",
    "ih",
    "ou",
    "ee",
    "oh",
    "blink",
    "blinkLeft",
    "blinkRight",
    "lookUp",
    "lookDown",
    "lookLeft",
    "lookRight",
    "neutral",
)

ExpressionId = str  # happy | sad | angry | shy | surprised | thinking | neutral | sleepy | curious
ActivityId = str  # idle | listening | thinking | speaking | reacting
AnimationId = str  # idle_breathe | idle_sway | listen | think | talk | react | nod | lean_in
SpeakSource = str  # none | chat | initiative | thinking
AvatarBackend = str  # procedural | live2d | vroid | vrchat

# VRChat SDK3 viseme morph names (and common aliases without the vrc.v_ prefix).
VRCHAT_VISEMES = (
    "vrc.v_sil",
    "vrc.v_pp",
    "vrc.v_ff",
    "vrc.v_th",
    "vrc.v_dd",
    "vrc.v_kk",
    "vrc.v_ch",
    "vrc.v_ss",
    "vrc.v_nn",
    "vrc.v_rr",
    "vrc.v_aa",
    "vrc.v_e",
    "vrc.v_ih",
    "vrc.v_oh",
    "vrc.v_ou",
)

_PAUSE_RE = re.compile(r"\[pause:([0-9]*\.?[0-9]+)s\]", re.I)


@dataclass
class AvatarState:
    """Snapshot pushed over WebSocket /api/avatar."""

    expression: ExpressionId = "neutral"
    activity: ActivityId = "idle"
    animation: AnimationId = "idle_breathe"
    speak_source: SpeakSource = "none"
    speaking: bool = False
    mouth_open: float = 0.0  # 0..1
    viseme: str = "sil"
    visemes: dict[str, float] = field(default_factory=dict)
    blink: float = 1.0  # eye openness (1 = open)
    params: dict[str, float] = field(default_factory=dict)
    gesture: dict[str, float] = field(default_factory=dict)
    vrm: dict[str, float] = field(default_factory=dict)
    vrchat: dict[str, float] = field(default_factory=dict)
    label: str = "neutral"
    valence: float = 0.0
    arousal: float = 0.3
    thought_snippet: str = ""
    backend: AvatarBackend = "procedural"
    model_url: str | None = None
    model_ready: bool = False
    model_kind: str | None = None  # model3 | vrm | fbx | gltf
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_avatar_dir(settings_avatar_dir: Path | None = None) -> Path:
    return Path(settings_avatar_dir) if settings_avatar_dir else (OPHELIA_HOME / "avatar")


def _resolve_configured(root: Path, configured: str | None) -> Path | None:
    if not configured:
        return None
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = root / configured
    return candidate if candidate.is_file() else None


def find_model3(avatar_dir: Path, configured: str | None = None) -> Path | None:
    """Locate a Cubism model3.json under the avatar directory."""
    root = resolve_avatar_dir(avatar_dir)
    configured_path = _resolve_configured(root, configured)
    if configured_path and configured_path.suffix.lower() == ".json" and configured_path.name.endswith(
        ".model3.json"
    ):
        return configured_path
    if configured_path and configured_path.name.endswith(".model3.json"):
        return configured_path
    # Only honor configured when it points at model3; otherwise scan.
    if configured and configured_path is None and str(configured).lower().endswith(".model3.json"):
        return None
    if not root.is_dir():
        return None
    direct = root / "model.model3.json"
    if direct.is_file():
        return direct
    matches = sorted(root.rglob("*.model3.json"))
    return matches[0] if matches else None


def find_vrm(avatar_dir: Path, configured: str | None = None) -> Path | None:
    """Locate a VRoid / VRM model under the avatar directory."""
    root = resolve_avatar_dir(avatar_dir)
    configured_path = _resolve_configured(root, configured)
    if configured_path and configured_path.suffix.lower() == ".vrm":
        return configured_path
    if configured and str(configured).lower().endswith(".vrm") and configured_path is None:
        return None
    if not root.is_dir():
        return None
    for name in ("model.vrm", "ophelia.vrm", "avatar.vrm"):
        direct = root / name
        if direct.is_file():
            return direct
    matches = sorted(root.rglob("*.vrm"))
    return matches[0] if matches else None


def find_vrchat(avatar_dir: Path, configured: str | None = None) -> Path | None:
    """Locate a VRChat humanoid model — FBX first, then glTF/GLB.

    VRChat avatars are authored as FBX (Unity humanoid). The workstation loads
    `.fbx` via three.js FBXLoader. `.glb`/`.gltf` remain accepted as alternates.
    Native `.vrca` AssetBundles are not browser-loadable.
    """
    root = resolve_avatar_dir(avatar_dir)
    configured_path = _resolve_configured(root, configured)
    vrchat_suffixes = (".fbx", ".glb", ".gltf")
    if configured_path and configured_path.suffix.lower() in vrchat_suffixes:
        return configured_path
    if configured:
        low = str(configured).lower()
        if low.endswith(vrchat_suffixes) and configured_path is None:
            return None
    if not root.is_dir():
        return None
    for name in (
        "model.fbx",
        "ophelia.fbx",
        "avatar.fbx",
        "vrchat.fbx",
        "model.glb",
        "model.gltf",
        "ophelia.glb",
        "avatar.glb",
        "vrchat.glb",
    ):
        direct = root / name
        if direct.is_file():
            return direct
    matches = (
        sorted(root.rglob("*.fbx"))
        + sorted(root.rglob("*.glb"))
        + sorted(root.rglob("*.gltf"))
    )
    return matches[0] if matches else None


def _find_by_suffixes(root: Path, suffixes: tuple[str, ...]) -> Path | None:
    if not root.is_dir():
        return None
    preferred_names = {
        ".fbx": ("model.fbx", "ophelia.fbx", "avatar.fbx", "vrchat.fbx"),
        ".glb": ("model.glb", "ophelia.glb", "avatar.glb", "vrchat.glb"),
        ".gltf": ("model.gltf",),
    }
    for suffix in suffixes:
        for name in preferred_names.get(suffix, ()):
            direct = root / name
            if direct.is_file():
                return direct
    matches: list[Path] = []
    for suffix in suffixes:
        matches.extend(sorted(root.rglob(f"*{suffix}")))
    return matches[0] if matches else None


def resolve_model(
    avatar_dir: Path,
    configured: str | None = None,
    *,
    prefer: str = "auto",
) -> tuple[str | None, Path | None]:
    """Return (kind, path) where kind is 'vrm' | 'fbx' | 'gltf' | 'model3' | None."""
    root = resolve_avatar_dir(avatar_dir)
    prefer = (prefer or "auto").lower()
    configured_path = _resolve_configured(root, configured)
    if configured_path:
        suffix = configured_path.suffix.lower()
        name = configured_path.name.lower()
        if suffix == ".vrm":
            return "vrm", configured_path
        if suffix == ".fbx":
            return "fbx", configured_path
        if suffix in (".glb", ".gltf"):
            return "gltf", configured_path
        if name.endswith(".model3.json"):
            return "model3", configured_path

    vrm = find_vrm(root, None)
    fbx = _find_by_suffixes(root, (".fbx",))
    gltf = _find_by_suffixes(root, (".glb", ".gltf"))
    model3 = find_model3(root, None)

    def _pick(*order: str) -> tuple[str | None, Path | None]:
        for kind in order:
            if kind == "vrm" and vrm:
                return "vrm", vrm
            if kind == "fbx" and fbx:
                return "fbx", fbx
            if kind == "gltf" and gltf:
                return "gltf", gltf
            if kind == "model3" and model3:
                return "model3", model3
        return None, None

    if prefer in ("vroid", "vrm"):
        return _pick("vrm", "fbx", "gltf", "model3")
    if prefer in ("vrchat", "fbx"):
        return _pick("fbx", "gltf", "vrm", "model3")
    if prefer in ("gltf", "glb"):
        return _pick("gltf", "fbx", "vrm", "model3")
    if prefer == "live2d":
        return _pick("model3", "vrm", "fbx", "gltf")

    # auto: VRM → FBX (VRChat) → glTF → Live2D
    return _pick("vrm", "fbx", "gltf", "model3")


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _context_blob(
    *,
    label: str = "",
    feelings: list[str] | None = None,
    urges: list[str] | None = None,
    thought: str = "",
    user_text: str = "",
    reply_text: str = "",
) -> str:
    parts = [
        label or "",
        " ".join(feelings or []),
        " ".join(urges or []),
        (thought or "")[:240],
        (user_text or "")[:160],
        (reply_text or "")[:160],
    ]
    return " ".join(parts).lower()


def resolve_activity(
    *,
    speaking: bool,
    speak_text: str = "",
    speak_source: SpeakSource = "none",
    user_talking: bool = False,
    agent_thinking: bool = False,
    seconds_since_user: float | None = None,
    seconds_since_agent: float | None = None,
) -> ActivityId:
    """Map conversation + signal flags into an avatar activity mode."""
    if speaking and speak_text.strip():
        return "speaking"
    if agent_thinking or (speaking and not speak_text.strip()) or speak_source == "thinking":
        return "thinking"
    if user_talking:
        return "listening"
    # Recent autonomous line → brief reacting window
    if speak_source == "initiative" and speaking:
        return "reacting"
    if seconds_since_agent is not None and 0 <= seconds_since_agent < 2.5:
        return "reacting"
    if seconds_since_user is not None and 0 <= seconds_since_user < 8.0:
        return "listening" if seconds_since_user < 1.2 else "idle"
    return "idle"


def animation_for(
    activity: ActivityId,
    expression: ExpressionId,
    *,
    arousal: float = 0.3,
) -> AnimationId:
    """Pick a gesture/animation clip id for the current activity + emotion."""
    if activity == "speaking":
        return "talk"
    if activity == "thinking":
        return "think"
    if activity == "listening":
        return "listen"
    if activity == "reacting":
        if expression in ("surprised", "curious"):
            return "react"
        if expression in ("happy", "shy"):
            return "nod"
        if expression in ("sad", "angry"):
            return "react"
        return "nod"
    # idle
    if expression == "sleepy" or arousal < 0.22:
        return "idle_breathe"
    if arousal > 0.55 or expression in ("happy", "curious"):
        return "idle_sway"
    return "idle_breathe"


def expression_from_mood(
    *,
    label: str,
    valence: float,
    arousal: float,
    feelings: list[str] | None = None,
    boredom: float = 0.0,
    curiosity: float = 0.0,
    urges: list[str] | None = None,
    thought: str = "",
    user_text: str = "",
    reply_text: str = "",
    activity: ActivityId = "idle",
) -> ExpressionId:
    """Pick a named expression from psyche + conversation context."""
    text = _context_blob(
        label=label,
        feelings=feelings,
        urges=urges,
        thought=thought,
        user_text=user_text,
        reply_text=reply_text,
    )
    if activity == "thinking" and not any(
        k in text for k in ("angry", "sad", "happy", "surpris", "shy")
    ):
        return "thinking"
    if any(k in text for k in ("angry", "annoy", "irritat", "frustrat", "mad")):
        return "angry"
    if any(k in text for k in ("shy", "embarrass", "fluster", "blush", "awkward")):
        return "shy"
    if any(k in text for k in ("surpris", "shock", "wow", "startl", "wait what")):
        return "surprised"
    if any(k in text for k in ("think", "ponder", "wonder", "hmm", "consider")):
        return "thinking"
    if any(k in text for k in ("sleep", "tired", "drowsy", "bored", "yawn")) or boredom > 0.72:
        return "sleepy"
    if any(k in text for k in ("curios", "interes", "fascinat", "ooh")) or (
        curiosity > 0.7 and valence >= 0
    ):
        return "curious"
    if any(
        k in text for k in ("sad", "lonely", "melanch", "hurt", "down", "miss you", "missed")
    ) or valence < -0.35:
        return "sad"
    if any(
        k in text
        for k in ("happy", "joy", "excit", "warm", "fond", "play", "love", "heh", "lol")
    ) or (valence > 0.35 and arousal > 0.35):
        return "happy"
    if valence > 0.2:
        return "happy"
    if arousal < 0.2 and boredom > 0.45:
        return "sleepy"
    if activity == "listening":
        return "curious" if curiosity > 0.45 else "neutral"
    return "neutral"


def gesture_params(
    *,
    activity: ActivityId,
    animation: AnimationId,
    expression: ExpressionId,
    arousal: float,
    t: float,
) -> dict[str, float]:
    """Extra animation knobs for frontend stages (0..1 or degrees)."""
    breath_rate = 1.0 + arousal * 0.8
    if activity == "thinking":
        breath_rate *= 0.85
    if activity == "speaking":
        breath_rate *= 1.15
    sway = 0.35 + arousal * 0.45
    if animation == "idle_breathe":
        sway *= 0.55
    if animation == "listen":
        sway = 0.2
    nod = 0.0
    if animation in ("nod", "listen"):
        nod = 0.35 + 0.25 * abs(math.sin(t * 2.2))
    if animation == "think":
        nod = 0.15 * math.sin(t * 0.9)
    lean = 0.0
    if activity == "listening" or animation == "lean_in":
        lean = 0.35 + arousal * 0.2
    if activity == "speaking":
        lean = 0.15
    hand = 0.0
    if activity == "speaking" and expression in ("happy", "curious", "surprised"):
        hand = 0.25 + 0.2 * abs(math.sin(t * 3.0))
    if animation == "react":
        hand = 0.45
    return {
        "breath_rate": round(breath_rate, 3),
        "sway_amp": round(_clamp(sway, 0.0, 1.0), 3),
        "nod": round(_clamp(nod, 0.0, 1.0), 3),
        "lean_in": round(_clamp(lean, 0.0, 1.0), 3),
        "hand_emphasis": round(_clamp(hand, 0.0, 1.0), 3),
        "blink_rate": round(0.7 if activity == "thinking" else (1.3 if arousal > 0.6 else 1.0), 3),
    }


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
    urges: list[str] | None = None,
    thought: str = "",
    user_text: str = "",
    reply_text: str = "",
    activity: ActivityId = "idle",
    animation: AnimationId = "idle_breathe",
    gesture: dict[str, float] | None = None,
) -> tuple[ExpressionId, dict[str, float]]:
    """Build Live2D-style parameters from psyche + activity + speaking state."""
    now = t if t is not None else time.time()
    expr = expression_from_mood(
        label=label,
        valence=valence,
        arousal=arousal,
        feelings=feelings,
        boredom=boredom,
        curiosity=curiosity,
        urges=urges,
        thought=thought,
        user_text=user_text,
        reply_text=reply_text,
        activity=activity,
    )
    g = gesture or gesture_params(
        activity=activity, animation=animation, expression=expr, arousal=arousal, t=now
    )
    breath_rate = float(g.get("breath_rate", 1.0))
    breath = 0.5 + 0.5 * math.sin(now * (1.2 + arousal * 1.4) * breath_rate)
    sway_amp = float(g.get("sway_amp", 0.5))
    sway = math.sin(now * 0.55) * (4.0 + arousal * 6.0) * sway_amp
    nod = math.sin(now * 0.35) * (2.0 + boredom * 3.0)
    nod += float(g.get("nod", 0.0)) * 8.0
    if activity == "thinking":
        nod += 4.0
        sway *= 0.6

    mouth_form = _clamp(valence * 0.85 + (0.25 if expr == "happy" else 0.0))
    if expr == "sad":
        mouth_form = _clamp(min(mouth_form, -0.35))
    if expr == "angry":
        mouth_form = _clamp(min(mouth_form, -0.15))

    brow = 0.0
    if expr in ("angry", "thinking") or activity == "thinking":
        brow = -0.45 if expr == "angry" else 0.25
    elif expr == "surprised":
        brow = 0.55
    elif expr == "sad":
        brow = -0.25
    elif expr == "curious" or activity == "listening":
        brow = 0.2

    eye_open = 1.0
    if expr == "sleepy" or boredom > 0.65:
        eye_open = max(0.35, 1.0 - boredom * 0.55)
    if expr == "surprised":
        eye_open = 1.0
    if activity == "thinking":
        eye_open = min(eye_open, 0.85)

    angle_z = 0.0
    if expr == "shy" or activity == "thinking":
        angle_z = -8.0 if expr == "shy" else -5.0
    elif expr == "curious":
        angle_z = 6.0 * math.sin(now * 0.4)
    elif curiosity > 0.55:
        angle_z = 4.0

    lean = float(g.get("lean_in", 0.0))
    body_x = _clamp((social - 0.5) * 8.0 + lean * 4.0, -10.0, 10.0)

    open_y = _clamp(mouth_open if speaking else 0.0, 0.0, 1.0)
    if speaking and open_y < 0.08 and activity == "speaking":
        open_y = 0.15 + 0.35 * abs(math.sin(now * 12.0))

    intensity = 0.55 + 0.45 * _clamp(expressiveness, 0.0, 1.0)
    params = {
        PARAM_ANGLE_X: sway * intensity,
        PARAM_ANGLE_Y: nod * 0.6 - (3.0 if expr == "shy" else 0.0),
        PARAM_ANGLE_Z: angle_z * intensity,
        PARAM_EYE_L_OPEN: eye_open,
        PARAM_EYE_R_OPEN: eye_open,
        PARAM_EYE_BALL_X: math.sin(now * 0.25) * 0.15,
        PARAM_EYE_BALL_Y: math.cos(now * 0.2) * 0.1 - (0.12 if activity == "thinking" else 0.0),
        PARAM_BROW_L_Y: brow,
        PARAM_BROW_R_Y: brow,
        PARAM_MOUTH_OPEN_Y: open_y,
        PARAM_MOUTH_FORM: mouth_form,
        PARAM_BODY_ANGLE_X: body_x,
        PARAM_BREATH: breath,
    }
    return expr, params


def vrm_weights_from_expression(
    expression: ExpressionId,
    *,
    mouth_open: float = 0.0,
    eye_open: float = 1.0,
    params: dict[str, float] | None = None,
    speaking: bool = False,
    visemes: dict[str, float] | None = None,
) -> dict[str, float]:
    """Map Ophelia expression + lip sync onto VRM 1.0 preset weights."""
    params = params or {}
    weights = {name: 0.0 for name in VRM_PRESETS}

    # Emotion presets (mutually soft — one primary)
    emotion_map = {
        "happy": "happy",
        "angry": "angry",
        "sad": "sad",
        "surprised": "surprised",
        "sleepy": "relaxed",
        "thinking": "relaxed",
        "curious": "surprised",
        "shy": "happy",
        "neutral": "neutral",
    }
    preset = emotion_map.get(expression, "neutral")
    if preset == "neutral":
        weights["neutral"] = 1.0
    else:
        weights[preset] = 0.85 if expression != "shy" else 0.45
        if expression == "shy":
            weights["happy"] = 0.35
        if expression == "curious":
            weights["surprised"] = 0.4
            weights["relaxed"] = 0.2

    # Mouth / visemes
    if speaking or mouth_open > 0.05 or visemes:
        lip = _viseme_to_vrm(visemes or {}, mouth_open)
        for k, v in lip.items():
            if k in weights:
                weights[k] = max(weights[k], v)

    # Blink from eye openness (1 = open → blink 0)
    blink = _clamp(1.0 - eye_open, 0.0, 1.0)
    if blink > 0.05:
        weights["blink"] = blink

    # Look direction from eye ball / head
    ball_x = float(params.get(PARAM_EYE_BALL_X, 0.0))
    ball_y = float(params.get(PARAM_EYE_BALL_Y, 0.0))
    if ball_x > 0.08:
        weights["lookRight"] = min(1.0, ball_x * 2.5)
    elif ball_x < -0.08:
        weights["lookLeft"] = min(1.0, abs(ball_x) * 2.5)
    if ball_y > 0.08:
        weights["lookUp"] = min(1.0, ball_y * 2.5)
    elif ball_y < -0.08:
        weights["lookDown"] = min(1.0, abs(ball_y) * 2.5)

    return weights


def vrchat_weights_from_expression(
    expression: ExpressionId,
    *,
    mouth_open: float = 0.0,
    eye_open: float = 1.0,
    params: dict[str, float] | None = None,
    speaking: bool = False,
    visemes: dict[str, float] | None = None,
) -> dict[str, float]:
    """Map Ophelia expression + lip sync onto VRChat-style morph target names."""
    params = params or {}
    weights: dict[str, float] = {name: 0.0 for name in VRCHAT_VISEMES}

    emotion_aliases: dict[str, list[tuple[str, float]]] = {
        "happy": [("Joy", 0.85), ("happy", 0.85), ("smile", 0.7)],
        "angry": [("Angry", 0.85), ("angry", 0.85), ("anger", 0.8)],
        "sad": [("Sorrow", 0.85), ("sad", 0.85), ("sorrow", 0.8)],
        "surprised": [("surprised", 0.8), ("Surprised", 0.8), ("Fun", 0.35)],
        "sleepy": [("Sorrow", 0.25), ("relaxed", 0.5), ("blink", 0.15)],
        "thinking": [("Sorrow", 0.15), ("neutral", 0.4)],
        "curious": [("surprised", 0.45), ("Fun", 0.3)],
        "shy": [("Joy", 0.4), ("happy", 0.35), ("blush", 0.5)],
        "neutral": [("neutral", 1.0)],
    }
    for name, value in emotion_aliases.get(expression, emotion_aliases["neutral"]):
        weights[name] = max(weights.get(name, 0.0), value)

    if speaking or mouth_open > 0.05 or visemes:
        lip = _viseme_to_vrchat(visemes or {}, mouth_open)
        for k, v in lip.items():
            weights[k] = max(weights.get(k, 0.0), v)
    else:
        weights["vrc.v_sil"] = 1.0
        weights["sil"] = 1.0

    blink = _clamp(1.0 - eye_open, 0.0, 1.0)
    if blink > 0.05:
        weights["vrc.blink"] = blink
        weights["blink"] = blink
        weights["Blink"] = blink
        weights["Blink_L"] = blink
        weights["Blink_R"] = blink

    return {k: v for k, v in weights.items() if v > 0.001}


def _strip_speak_markup(text: str) -> str:
    """Remove pause/IPA markup for lip-sync timing while preserving pauses as gaps."""
    out = _PAUSE_RE.sub(" ", text or "")
    out = re.sub(r"\[[^\]]+\]\(/[^)]*/\)", " ", out)
    return out


def _char_viseme(ch: str) -> str:
    ch = ch.lower()
    if ch in "aáàâä":
        return "aa"
    if ch in "eéèêë":
        return "E"
    if ch in "iíìîïy":
        return "ih"
    if ch in "oóòôö":
        return "oh"
    if ch in "uúùûüw":
        return "ou"
    if ch in "bpm":
        return "PP"
    if ch in "fv":
        return "FF"
    if ch in "th":
        return "TH"
    if ch in "l":
        return "DD"
    if ch in "kgc":
        return "kk"
    if ch in "sz":
        return "SS"
    if ch in "jr":
        return "RR"
    if ch in "n":
        return "nn"
    if ch in "ch" or ch in "j":
        return "CH"
    if ch.isspace() or not ch.isalnum():
        return "sil"
    return "aa"


def lip_sync_frame(
    text: str, elapsed: float, *, cps: float = 14.0
) -> tuple[float, str, dict[str, float]]:
    """Return (mouth_open, primary_viseme, viseme_weights) for spoken text.

    Honors `[pause:Xs]` tags (mouth closed during pauses). Uses a simple
    character-index timeline — good enough without audio analysis.
    """
    raw = text or ""
    # Build timeline accounting for pause tags
    segments: list[tuple[str, float]] = []  # (plain_chunk, pause_after)
    pos = 0
    total_pause = 0.0
    for m in _PAUSE_RE.finditer(raw):
        chunk = raw[pos : m.start()]
        pause = float(m.group(1))
        segments.append((_strip_speak_markup(chunk), pause))
        total_pause += pause
        pos = m.end()
    segments.append((_strip_speak_markup(raw[pos:]), 0.0))

    plain = "".join(s[0] for s in segments)
    clean = "".join(ch for ch in plain if ch.isalnum() or ch.isspace())
    if not clean.strip():
        return 0.0, "sil", {"sil": 1.0}

    speak_dur = max(0.35, len(clean) / max(cps, 1.0))
    total_dur = speak_dur + total_pause
    if elapsed < 0 or elapsed > total_dur + 0.3:
        return 0.0, "sil", {"sil": 1.0}

    # Walk timeline to find whether we're in a pause and the speak progress
    t = elapsed
    spoken_chars = 0
    for chunk, pause in segments:
        chunk_clean = "".join(ch for ch in chunk if ch.isalnum() or ch.isspace())
        chunk_dur = len(chunk_clean) / max(cps, 1.0) if chunk_clean else 0.0
        if t <= chunk_dur:
            if chunk_clean:
                frac = t / max(chunk_dur, 1e-6)
                idx = min(len(chunk_clean) - 1, int(frac * len(chunk_clean)))
                ch = chunk_clean[idx]
                vis = _char_viseme(ch)
                open_map = {
                    "aa": 0.85,
                    "oh": 0.7,
                    "ou": 0.55,
                    "E": 0.45,
                    "ih": 0.35,
                    "PP": 0.05,
                    "FF": 0.2,
                    "sil": 0.0,
                }
                base = open_map.get(vis, 0.4)
                wave = 0.2 * abs(math.sin(elapsed * 16.0))
                mouth = _clamp(base + wave, 0.0, 1.0)
                weights = {vis: mouth, "sil": max(0.0, 1.0 - mouth)}
                # Map to VRM short names too
                if vis == "E":
                    weights["ee"] = mouth * 0.8
                    weights["E"] = mouth
                elif vis == "aa":
                    weights["aa"] = mouth
                elif vis == "oh":
                    weights["oh"] = mouth
                elif vis == "ou":
                    weights["ou"] = mouth
                elif vis == "ih":
                    weights["ih"] = mouth
                return mouth, vis, weights
            return 0.0, "sil", {"sil": 1.0}
        t -= chunk_dur
        spoken_chars += len(chunk_clean)
        if pause > 0:
            if t <= pause:
                return 0.0, "sil", {"sil": 1.0}
            t -= pause
    return 0.0, "sil", {"sil": 1.0}


def mouth_envelope(text: str, elapsed: float, *, cps: float = 14.0) -> float:
    """Approximate lip-sync mouth open from spoken text + elapsed seconds."""
    mouth, _vis, _weights = lip_sync_frame(text, elapsed, cps=cps)
    return mouth


def _viseme_to_vrm(visemes: dict[str, float], mouth_open: float) -> dict[str, float]:
    out = {
        "aa": visemes.get("aa", 0.0),
        "ih": visemes.get("ih", 0.0),
        "ou": visemes.get("ou", 0.0),
        "ee": visemes.get("ee", visemes.get("E", 0.0) * 0.8),
        "oh": visemes.get("oh", 0.0),
    }
    if mouth_open > 0.05 and sum(out.values()) < 0.05:
        out["aa"] = mouth_open * 0.9
        out["oh"] = mouth_open * 0.35
    return out


def _viseme_to_vrchat(visemes: dict[str, float], mouth_open: float) -> dict[str, float]:
    mapping = {
        "sil": "vrc.v_sil",
        "PP": "vrc.v_pp",
        "FF": "vrc.v_ff",
        "TH": "vrc.v_th",
        "DD": "vrc.v_dd",
        "kk": "vrc.v_kk",
        "CH": "vrc.v_ch",
        "SS": "vrc.v_ss",
        "nn": "vrc.v_nn",
        "RR": "vrc.v_rr",
        "aa": "vrc.v_aa",
        "E": "vrc.v_e",
        "ee": "vrc.v_e",
        "ih": "vrc.v_ih",
        "oh": "vrc.v_oh",
        "ou": "vrc.v_ou",
    }
    out: dict[str, float] = {name: 0.0 for name in VRCHAT_VISEMES}
    for key, value in visemes.items():
        target = mapping.get(key)
        if target:
            out[target] = max(out[target], value)
        if key in ("aa", "oh", "ih", "ou", "sil"):
            out[key] = max(out.get(key, 0.0), value)
    if mouth_open > 0.05 and out["vrc.v_aa"] < 0.05 and out["vrc.v_oh"] < 0.05:
        out["vrc.v_aa"] = mouth_open * 0.9
        out["vrc.v_oh"] = mouth_open * 0.35
        out["aa"] = out["vrc.v_aa"]
        out["oh"] = out["vrc.v_oh"]
    if mouth_open < 0.08:
        out["vrc.v_sil"] = max(out["vrc.v_sil"], 1.0 - mouth_open)
        out["sil"] = out["vrc.v_sil"]
    return out


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
        self._speak_source: SpeakSource = "none"
        self._mouth = 0.0
        self._viseme = "sil"
        self._visemes: dict[str, float] = {"sil": 1.0}
        self._user_text = ""
        self._last: AvatarState | None = None

    def model_file(self) -> Path | None:
        _kind, path = resolve_model(
            self.avatar_dir, self.model_path, prefer=self.backend_pref
        )
        return path

    def model_kind(self) -> str | None:
        kind, _path = resolve_model(
            self.avatar_dir, self.model_path, prefer=self.backend_pref
        )
        return kind

    def resolved_backend(self) -> str:
        if self.backend_pref == "procedural":
            return "procedural"
        kind = self.model_kind()
        if kind == "vrm":
            return "vroid"
        if kind in ("fbx", "gltf"):
            return "vrchat"
        if kind == "model3":
            return "live2d"
        return "procedural"

    def model_url(self) -> str | None:
        path = self.model_file()
        if not path:
            return None
        try:
            rel = path.relative_to(self.avatar_dir)
        except ValueError:
            rel = Path(path.name)
        return f"/avatar/{rel.as_posix()}"

    def note_user_text(self, text: str) -> None:
        """Remember the latest user utterance for expression context."""
        self._user_text = (text or "")[:400]

    def begin_thinking(self) -> None:
        """Enter thinking / composing face (mouth idle, thinking expression)."""
        self._speaking = True
        self._speak_text = ""
        self._speak_started = time.time()
        self._speak_source = "thinking"
        self._mouth = 0.0
        self._viseme = "sil"
        self._visemes = {"sil": 1.0}

    def begin_speak(self, text: str = "", *, source: SpeakSource = "chat") -> None:
        self._speaking = True
        self._speak_text = text or ""
        self._speak_started = time.time()
        self._speak_source = source if text else "thinking"
        self._mouth = 0.2 if text else 0.0
        if not text:
            self._viseme = "sil"
            self._visemes = {"sil": 1.0}

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def speak_source(self) -> SpeakSource:
        return self._speak_source

    @property
    def is_active(self) -> bool:
        """True when avatar should tick faster than idle (speak/think/react)."""
        if self._speaking:
            return True
        if self._last and self._last.activity in ("thinking", "listening", "reacting", "speaking"):
            return True
        return False

    def end_speak(self) -> None:
        self._speaking = False
        self._speak_text = ""
        self._speak_source = "none"
        self._mouth = 0.0
        self._viseme = "sil"
        self._visemes = {"sil": 1.0}

    def set_mouth(self, value: float) -> None:
        self._mouth = _clamp(float(value), 0.0, 1.0)

    def tick_mouth(self) -> float:
        if not self._speaking:
            self._mouth = 0.0
            self._viseme = "sil"
            self._visemes = {"sil": 1.0}
            return 0.0
        if self._speak_text:
            mouth, viseme, weights = lip_sync_frame(
                self._speak_text, time.time() - self._speak_started
            )
            self._mouth = mouth
            self._viseme = viseme
            self._visemes = weights
            if mouth <= 0.0 and (time.time() - self._speak_started) > 0.5:
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
        urges: list[str] | None = None,
        thought: str = "",
        user_talking: bool = False,
        agent_thinking: bool = False,
        seconds_since_user: float | None = None,
        seconds_since_agent: float | None = None,
    ) -> AvatarState:
        mouth = self.tick_mouth()
        activity = resolve_activity(
            speaking=self._speaking,
            speak_text=self._speak_text,
            speak_source=self._speak_source,
            user_talking=user_talking,
            agent_thinking=agent_thinking,
            seconds_since_user=seconds_since_user,
            seconds_since_agent=seconds_since_agent,
        )
        # Preliminary expression for animation choice
        pre_expr = expression_from_mood(
            label=label,
            valence=valence,
            arousal=arousal,
            feelings=feelings,
            boredom=boredom,
            curiosity=curiosity,
            urges=urges,
            thought=thought,
            user_text=self._user_text,
            reply_text=self._speak_text,
            activity=activity,
        )
        animation = animation_for(activity, pre_expr, arousal=arousal)
        now = time.time()
        gesture = gesture_params(
            activity=activity,
            animation=animation,
            expression=pre_expr,
            arousal=arousal,
            t=now,
        )
        expr, params = params_from_psyche(
            label=label,
            valence=valence,
            arousal=arousal,
            feelings=feelings,
            boredom=boredom,
            curiosity=curiosity,
            social=social,
            expressiveness=expressiveness,
            speaking=self._speaking and activity == "speaking",
            mouth_open=mouth if activity == "speaking" else 0.0,
            t=now,
            urges=urges,
            thought=thought,
            user_text=self._user_text,
            reply_text=self._speak_text,
            activity=activity,
            animation=animation,
            gesture=gesture,
        )
        backend = self.resolved_backend()
        kind = self.model_kind()
        model_url = self.model_url()
        eye_open = float(params.get(PARAM_EYE_L_OPEN, 1.0))
        visemes = dict(self._visemes) if activity == "speaking" else {"sil": 1.0}
        viseme = self._viseme if activity == "speaking" else "sil"
        vrm = vrm_weights_from_expression(
            expr,
            mouth_open=mouth if activity == "speaking" else 0.0,
            eye_open=eye_open,
            params=params,
            speaking=activity == "speaking",
            visemes=visemes,
        )
        vrchat = vrchat_weights_from_expression(
            expr,
            mouth_open=mouth if activity == "speaking" else 0.0,
            eye_open=eye_open,
            params=params,
            speaking=activity == "speaking",
            visemes=visemes,
        )
        snippet = (thought or "").strip().replace("\n", " ")
        state = AvatarState(
            expression=expr,
            activity=activity,
            animation=animation,
            speak_source=self._speak_source,
            speaking=activity == "speaking",
            mouth_open=mouth if activity == "speaking" else 0.0,
            viseme=viseme,
            visemes=visemes,
            blink=eye_open,
            params=params,
            gesture=gesture,
            vrm=vrm,
            vrchat=vrchat,
            label=label or expr,
            valence=valence,
            arousal=arousal,
            thought_snippet=snippet[:120],
            backend=backend,
            model_url=model_url,
            model_ready=bool(model_url) and backend in ("live2d", "vroid", "vrchat"),
            model_kind=kind,
            updated_at=now,
        )
        self._last = state
        return state

    def last(self) -> AvatarState | None:
        return self._last
