"""AIR URNs (Civitai resource ids) and model-family compatibility.

``urn:air:<ecosystem>:<type>:civitai:<modelId>@<versionId>``. The ecosystem
says which engine runs it (sdxl, sd1, flux1, anima, zImage, qwen, flux2);
the *family* (Illustrious, Pony, plain SDXL, ...) only shows in the site
API's baseModel, which is why LoRA checks look that up.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

log = structlog.get_logger()

_AIR_RE = re.compile(
    r"^urn:air:(?P<eco>[^:]+):(?P<kind>[^:]+):(?P<source>[^:]+):(?P<id>.+)$",
    re.IGNORECASE,
)

# Site-API baseModel strings → (orchestration ecosystem, prompt style).
_BASE_MODEL_MAP: list[tuple[tuple[str, ...], str, str]] = [
    (("illustrious", "noobai", "noob"), "sdxl", "danbooru"),
    (("pony",), "sdxl", "danbooru"),
    (("sdxl", "sd xl", "sd_xl"), "sdxl", "mixed"),
    (("sd 1.5", "sd1.5", "sd 1", "sd1", "sd15"), "sd1", "danbooru"),
    (("flux.2", "flux2", "flux 2"), "flux2", "natural"),
    (("flux", "flux.1", "flux1"), "flux1", "natural"),
    (("anima",), "anima", "danbooru"),
    (("z-image", "zimage", "z image"), "zimage", "natural"),
    (("sd 3", "sd3", "stable diffusion 3"), "sd3", "natural"),
    (("qwen",), "qwen", "natural"),
]


def sanitize_air(raw: str) -> str:
    """Normalize AIR URNs. Fixes ?version= / # typos that 404 on orchestration."""
    air = (raw or "").strip()
    if not air:
        return ""
    # urn:air:…:civitai:ID?version=VID  →  …:ID@VID
    if "?version=" in air.lower():
        base, _, ver = air.partition("?")
        vid = ver.split("=", 1)[-1].split("&")[0].strip()
        if "@" not in base and vid.isdigit():
            air = f"{base}@{vid}"
        else:
            air = base
    if "#" in air:
        air = air.split("#", 1)[0]
    return air.strip()


def is_air(raw: str) -> bool:
    return bool(_AIR_RE.match(sanitize_air(raw)))


def air_kind(air: str) -> str:
    """AIR kind (checkpoint|lora|diffuser|…) or '' if not a parseable URN."""
    m = _AIR_RE.match(sanitize_air(air))
    return (m.group("kind") or "").lower() if m else ""


def version_id(air: str) -> int | None:
    """The @versionId of an AIR, if present."""
    clean = sanitize_air(air)
    if "@" not in clean:
        return None
    vid = clean.rsplit("@", 1)[-1].split("+")[0]
    return int(vid) if vid.isdigit() else None


def model_id(air: str) -> int | None:
    m = _AIR_RE.match(sanitize_air(air))
    if not m:
        return None
    mid = m.group("id").split("@", 1)[0]
    return int(mid) if mid.isdigit() else None


def _norm_eco(eco: str) -> str:
    eco = (eco or "").lower()
    if eco in ("sd15", "sd1.5"):
        return "sd1"
    if eco in ("flux", "flux.1"):
        return "flux1"
    if eco.startswith("flux2"):
        return "flux2"
    return eco


def ecosystem_from_air_or_base(air: str = "", base_model: str = "") -> str:
    """Normalized ecosystem (sdxl, sd1, flux1, flux2, anima, zimage, qwen, …)."""
    m = _AIR_RE.match((air or "").strip())
    if m:
        return _norm_eco(m.group("eco"))
    low = (base_model or "").strip().lower()
    for keys, eco, _style in _BASE_MODEL_MAP:
        if any(k in low for k in keys):
            return eco
    return "sdxl"


def prompt_style_for(ecosystem: str, base_model: str = "") -> str:
    low = (base_model or "").strip().lower()
    for keys, _eco, style in _BASE_MODEL_MAP:
        if any(k in low for k in keys):
            return style
    eco = (ecosystem or "").lower()
    if eco in ("sd1", "sd15", "anima"):
        return "danbooru"
    if eco in ("flux1", "flux", "flux2", "sd3", "qwen", "zimage"):
        return "natural"
    return "mixed"


def base_family(base_model: str = "", air: str = "") -> str:
    """Coarse family for compatibility: illustrious|pony|sdxl|sd1|flux|anima|
    zimage|qwen|other."""
    low = f"{base_model} {air}".lower()
    if "illustrious" in low or "noobai" in low or "noob" in low:
        return "illustrious"
    if "pony" in low:
        return "pony"
    if "anima" in low:
        return "anima"
    if "zimage" in low or "z-image" in low:
        return "zimage"
    if "qwen" in low:
        return "qwen"
    if "flux" in low:
        return "flux"
    if any(x in low for x in ("sd 1", "sd1", "sd15", ":sd1:", ":sd15:")):
        return "sd1"
    if "sdxl" in low or ":sdxl:" in low:
        return "sdxl"
    return "other"


def lora_compatible_with_checkpoint(
    checkpoint_air: str,
    checkpoint_base: str,
    lora_air: str,
    lora_base: str,
) -> bool:
    """True when LoRA family matches the checkpoint (no Pony-on-SDXL, etc.)."""
    ck = base_family(checkpoint_base, checkpoint_air)
    lr = base_family(lora_base, lora_air)
    if ck == "flux" or lr == "flux":
        return ck == lr
    if ck == "other" or lr == "other":
        ck_eco = ecosystem_from_air_or_base(checkpoint_air, checkpoint_base)
        lr_eco = ecosystem_from_air_or_base(lora_air, lora_base)
        return ck_eco == lr_eco and ck_eco in ("sdxl", "sd1")
    return ck == lr


def lora_family_error(
    checkpoint_air: str,
    checkpoint_base: str,
    lora_air: str,
    lora_base: str,
    *,
    ecosystem: str = "",
) -> str:
    """Why this LoRA can't run on this model, or '' if it can (or we can't
    tell). Only verified mismatches are errors: differing AIR ecosystems
    (sd1 vs sdxl vs anima ...), or both baseModels known from the site API
    and from incompatible families (Pony LoRA on Illustrious, ...).

    ``ecosystem`` covers models without a checkpoint AIR (anima, zimage...).
    """
    lr_m = _AIR_RE.match(sanitize_air(lora_air))
    ck_eco = (
        ecosystem_from_air_or_base(checkpoint_air)
        if _AIR_RE.match(sanitize_air(checkpoint_air))
        else _norm_eco(ecosystem)
    )
    if lr_m and ck_eco:
        lr_eco = ecosystem_from_air_or_base(lora_air)
        if ck_eco != lr_eco:
            target = checkpoint_air or ck_eco
            return f"LoRA {lora_air} is {lr_eco} but the model {target} is {ck_eco}"
    if checkpoint_base and lora_base and not lora_compatible_with_checkpoint(
        checkpoint_air, checkpoint_base, lora_air, lora_base
    ):
        return (
            f"LoRA {lora_air} is {lora_base} ({base_family(lora_base)}) but checkpoint "
            f"{checkpoint_air} is {checkpoint_base} ({base_family(checkpoint_base)})"
        )
    return ""


def filter_loras_for_checkpoint(
    lora_map: dict[str, float],
    *,
    checkpoint_air: str,
    checkpoint_base: str = "",
    lora_meta: dict[str, str] | None = None,
) -> dict[str, float]:
    """Drop LoRAs whose family doesn't match the checkpoint; sanitize AIRs."""
    out: dict[str, float] = {}
    meta = lora_meta or {}
    for air, strength in (lora_map or {}).items():
        clean = sanitize_air(air)
        if not clean:
            continue
        lora_base = meta.get(clean) or meta.get(air) or ""
        if lora_compatible_with_checkpoint(checkpoint_air, checkpoint_base, clean, lora_base):
            out[clean] = strength
        else:
            log.info("civitai.lora_dropped_incompatible", lora=clean, checkpoint=checkpoint_air)
    return out


def parse_loras(raw: Any) -> dict[str, float]:
    """Accept dict, JSON string, or 'urn|strength,urn' / 'urn=strength'."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        out: dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                out[str(k)] = 0.8
        return out
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return parse_loras(json.loads(text))
        except json.JSONDecodeError:
            pass
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            urn, _, strength = part.partition("|")
        elif "=" in part:
            urn, _, strength = part.partition("=")
        else:
            urn, strength = part, "0.8"
        try:
            out[urn.strip()] = float(strength.strip())
        except ValueError:
            out[urn.strip()] = 0.8
    return out
