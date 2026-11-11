"""Match the art style of a reference image without copying its content.

img2img (image=) keeps the reference's composition and subject, so "draw
me in the style of this" came back as a redrawn copy, or a new picture that
ignored the style. Here the vision model reads only the style (medium,
linework, shading, palette, lighting, texture, era) and those words go into
the prompt. The medium also steers auto mode to the right model family
(an anime reference gets an anime checkpoint, a photo a photoreal one).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from ophelia.fileio import atomic_write_json
import structlog

log = structlog.get_logger()

STYLE_QUESTION = (
    "Look only at the ART STYLE of this image, not what it shows. Reply with JSON only, "
    "no other text:\n"
    '{"medium": one of "anime", "photo", "3d", "painting", "comic", "pixel", "other",\n'
    ' "tags": 8 to 15 short style tags an image model understands: rendering, linework, '
    "shading, color palette, lighting, texture, camera/film look, era or genre "
    '(e.g. "thick lineart", "cel shading", "flat colors", "pastel palette", "soft '
    'airbrush shading", "film grain", "35mm photo", "oil painting", "1990s anime style", '
    '"high contrast", "neon lighting"),\n'
    ' "sentence": one sentence describing the style for an image model}\n'
    "Never mention the subject, people, clothing, body, pose, names or text in the image."
)

# Medium → the word that steers auto mode (see civitai prompting.STYLE_WORDS).
_MEDIUM_WORD = {
    "anime": "anime style", "photo": "photo", "3d": "3d render", "painting": "painting",
    "comic": "comic style", "pixel": "pixel art",
}
_SUBJECTISH = re.compile(
    r"\b(girl|boy|woman|man|women|men|person|people|character|hair|eyes|dress|outfit|"
    r"nude|naked|breasts?|pose|face|body|wearing)\b", re.I)


@dataclass
class StyleRef:
    medium: str = "other"
    tags: list[str] = field(default_factory=list)
    sentence: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.tags or self.sentence)


def parse_style(text: str) -> StyleRef:
    """StyleRef from the vision reply (tolerates code fences and chatter)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return StyleRef()
    try:
        data: Any = json.loads(m.group(0))
    except json.JSONDecodeError:
        return StyleRef()
    if not isinstance(data, dict):
        return StyleRef()
    medium = str(data.get("medium") or "other").strip().lower()
    if medium not in _MEDIUM_WORD:
        medium = "other"
    raw = data.get("tags") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    tags: list[str] = []
    for t in raw:
        t = re.sub(r"\s+", " ", str(t)).strip(" .,'\"").lower()
        # Keep style only: the subject comes from her prompt, never the reference.
        if t and len(t) <= 40 and not _SUBJECTISH.search(t) and t not in tags:
            tags.append(t)
    sentence = str(data.get("sentence") or "").strip()
    if _SUBJECTISH.search(sentence):
        sentence = ""
    return StyleRef(medium=medium, tags=tags[:15], sentence=sentence[:300])


def apply_style(prompt: str, ref: StyleRef) -> str:
    """The prompt with the reference's style added: its medium word first when
    the prompt names no style, then the style tags."""
    from ophelia.providers.civitai.prompting import _has_word, detect_style

    text = (prompt or "").strip()
    low = text.lower()
    word = _MEDIUM_WORD.get(ref.medium, "")
    if word and detect_style(text) == "sdxl" and not _has_word(low, word):
        text = f"{word}, {text}" if text else word
    extra = [t for t in ref.tags if t not in low]
    if extra:
        text = f"{text}, {', '.join(extra)}"
    return text


def _cache_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "style_refs.json"


def _load_cache(settings: Any) -> dict[str, dict]:
    try:
        data = json.loads(_cache_path(settings).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(settings: Any, cache: dict[str, dict]) -> None:
    try:
        path = _cache_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        keep = dict(list(cache.items())[-200:])
        atomic_write_json(path, keep)
    except Exception as e:
        log.debug("style_ref.cache_save_failed", error=str(e))


async def local_copy(source: str, dest_dir: Path) -> Path:
    """A local file for ``source`` (a path, or an http(s) URL downloaded)."""
    raw = (source or "").strip()
    if raw.startswith(("http://", "https://")):
        dest_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            r = await http.get(raw)
            r.raise_for_status()
        ext = ".png"
        ctype = r.headers.get("content-type", "")
        if "jpeg" in ctype or "jpg" in ctype:
            ext = ".jpg"
        elif "webp" in ctype:
            ext = ".webp"
        path = dest_dir / (hashlib.sha1(r.content).hexdigest()[:16] + ext)
        path.write_bytes(r.content)
        return path
    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"style reference not found: {raw}")
    return path


async def analyze_style(settings: Any, path: Path, *, stack=None) -> StyleRef:
    """Read a reference image's style (cached per image, so retries are free)."""
    from ophelia.media.vision_input import describe_image_file

    key = hashlib.sha1(path.read_bytes()).hexdigest()
    cache = _load_cache(settings)
    if key in cache:
        c = cache[key]
        return StyleRef(medium=c.get("medium", "other"), tags=list(c.get("tags") or []),
                        sentence=c.get("sentence", ""))
    text = await describe_image_file(settings, path, question=STYLE_QUESTION, stack=stack)
    ref = parse_style(text)
    log.info("style_ref.analyzed", path=str(path), medium=ref.medium, tags=len(ref.tags),
             ok=ref.ok)
    if ref.ok:
        cache[key] = {"medium": ref.medium, "tags": ref.tags, "sentence": ref.sentence}
        _save_cache(settings, cache)
    return ref
