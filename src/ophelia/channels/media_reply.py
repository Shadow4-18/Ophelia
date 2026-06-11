"""Detect generated artifacts in agent replies for Telegram media upload."""

from __future__ import annotations

import re
from pathlib import Path

PHOTO_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm"})
AUDIO_SUFFIXES = frozenset({".mp3", ".ogg", ".opus", ".m4a"})

_SAVED_TO = re.compile(
    r"(?:saved to|Saved to|Image saved to|Video saved to|TTS saved to)\s+"
    r"([^\s\n\"']+\.(?:mp4|mov|webm|png|jpe?g|webp|gif|mp3|ogg|opus|m4a))",
    re.IGNORECASE,
)


def artifact_paths_in_text(text: str) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for match in _SAVED_TO.finditer(text):
        p = Path(match.group(1)).expanduser()
        if p.is_file() and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in PHOTO_SUFFIXES:
        return "photo"
    if ext in VIDEO_SUFFIXES:
        return "video"
    if ext in AUDIO_SUFFIXES:
        return "audio"
    return None
