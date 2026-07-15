"""Classify and name inbound chat attachments (images, videos, files)."""

from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"})
# Archives + other uploadables she may want on the site or to unpack later
FILE_EXTS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".pdf", ".json", ".txt", ".md"})

_INBOUND_EXTS = IMAGE_EXTS | VIDEO_EXTS | FILE_EXTS


def classify_attachment(
    *,
    filename: str = "",
    mime: str = "",
) -> str | None:
    """Return 'image' | 'video' | 'file' | None (unsupported)."""
    name = (filename or "").lower()
    mime_l = (mime or "").lower()
    ext = Path(name).suffix.lower()

    if mime_l.startswith("image/") or ext in IMAGE_EXTS:
        return "image"
    if mime_l.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if (
        mime_l
        in (
            "application/zip",
            "application/x-zip-compressed",
            "application/x-rar-compressed",
            "application/x-7z-compressed",
            "application/gzip",
            "application/x-tar",
            "application/pdf",
            "application/json",
            "text/plain",
            "text/markdown",
        )
        or ext in FILE_EXTS
    ):
        return "file"
    # Generic binary with a known inbound extension already covered;
    # allow any non-empty extension that's in our allowlist only.
    if ext in _INBOUND_EXTS:
        if ext in IMAGE_EXTS:
            return "image"
        if ext in VIDEO_EXTS:
            return "video"
        return "file"
    return None


def safe_inbound_ext(filename: str, *, kind: str, default: str = "") -> str:
    """Pick a filesystem extension for an inbound save."""
    ext = Path(filename or "").suffix.lower()
    if ext and len(ext) <= 8 and ext.startswith("."):
        return ext
    if default:
        return default
    return { "image": ".jpg", "video": ".mp4", "file": ".bin" }.get(kind, ".bin")


def inbound_prompt_label(kind: str) -> str:
    return {
        "image": "image",
        "video": "video",
        "file": "file",
    }.get(kind, "file")
