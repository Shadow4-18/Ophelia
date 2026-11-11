"""One image request and its outcome, as objects instead of loose arguments
and strings.

Before, every option (prompt, model, LoRAs, pose, style, ...) was passed by
hand through four layers (tool → generate_image → dispatcher → backend), and
the outcome was a sentence that half a dozen places re-read with regexes
("Image saved to ..."). An ImageJob carries the request end to end; an
ImageResult says what happened (saved / pending / refused / failed), which
files, from which backend, and still renders to the same text she reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SAVED = "saved"
PENDING = "pending"
REFUSED = "refused"
FAILED = "failed"

_SAVED_TO = re.compile(r"(?:Image|Video|Audio) saved to (\S+)")


@dataclass
class ImageJob:
    prompt: str
    aspect_ratio: str = "1:1"
    nsfw: bool = False
    model: str = ""
    loras: Any = None
    negative_prompt: str | None = None
    image: str | None = None
    strength: float = 0.7
    auto_pick: bool = False
    quality: str = ""
    count: int = 1
    attempts: int = 1
    deliver_to: str = ""
    fallback_mature: bool = False
    pose_image: str | None = None
    style_terms: list[str] = field(default_factory=list)

    @classmethod
    def from_kwargs(cls, prompt: str, **kw: Any) -> "ImageJob":
        """From generate_image's keyword arguments (unknown ones ignored)."""
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in kw.items() if k in names}
        data["model"] = (data.get("model") or "").strip()
        data["style_terms"] = list(data.get("style_terms") or [])
        return cls(prompt=prompt, **data)

    def but(self, **changes: Any) -> "ImageJob":
        return replace(self, **changes)


@dataclass
class ImageResult:
    status: str  # saved | pending | refused | failed
    text: str  # what she reads (unchanged format)
    paths: list[Path] = field(default_factory=list)
    provider: str = ""
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.status == SAVED and bool(self.paths)

    @property
    def error(self) -> str:
        return self.text if self.status in (FAILED, REFUSED) else ""

    def __str__(self) -> str:
        return self.text

    @classmethod
    def from_text(cls, text: str, *, provider: str = "", model: str = "") -> "ImageResult":
        """Read a backend's reply (they still return text)."""
        text = text or ""
        paths = paths_in(text)
        if text.startswith("Pending:"):
            status = PENDING
        elif text.startswith("Refused:"):
            status = REFUSED
        elif text.startswith("Image saved to") and paths:
            status = SAVED
        elif text.startswith("Image saved to"):
            status = SAVED  # file already moved/delivered; still a success
        else:
            status = FAILED
        return cls(status=status, text=text, paths=paths, provider=provider, model=model)

    def but(self, **changes: Any) -> "ImageResult":
        return replace(self, **changes)


def paths_in(text: str) -> list[Path]:
    """Existing files named by "Image saved to <path>" lines, in order."""
    out: list[Path] = []
    for m in _SAVED_TO.finditer(text or ""):
        p = Path(m.group(1).rstrip(").,;")).expanduser()
        if p.is_file() and p not in out:
            out.append(p)
    return out
