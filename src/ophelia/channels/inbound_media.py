"""Classify and name inbound chat attachments (images, videos, files)."""

from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"})
# Music and recordings sent as files (voice notes have their own path).
AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".aac",
                        ".aif", ".aiff", ".wma", ".mid", ".midi"})
# Archives + other uploadables she may want on the site or to unpack later
FILE_EXTS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".pdf", ".json", ".txt", ".md"})

_INBOUND_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | FILE_EXTS


def classify_attachment(
    *,
    filename: str = "",
    mime: str = "",
) -> str | None:
    """Return 'image' | 'video' | 'audio' | 'file' | None (unsupported)."""
    name = (filename or "").lower()
    mime_l = (mime or "").lower()
    ext = Path(name).suffix.lower()

    if mime_l.startswith("image/") or ext in IMAGE_EXTS:
        return "image"
    if mime_l.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if mime_l.startswith("audio/") or ext in AUDIO_EXTS:
        return "audio"
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
        if ext in AUDIO_EXTS:
            return "audio"
        return "file"
    return None


def safe_inbound_ext(filename: str, *, kind: str, default: str = "") -> str:
    """Pick a filesystem extension for an inbound save."""
    ext = Path(filename or "").suffix.lower()
    if ext and len(ext) <= 8 and ext.startswith("."):
        return ext
    if default:
        return default
    return {"image": ".jpg", "video": ".mp4", "audio": ".mp3",
            "file": ".bin"}.get(kind, ".bin")


def inbound_prompt_label(kind: str) -> str:
    return {
        "image": "image",
        "video": "video",
        "audio": "audio file",
        "file": "file",
    }.get(kind, "file")


# Telegram's Bot API won't hand a bot any file bigger than this.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def too_big_for_telegram(size: int | None) -> str:
    """The message to send when a file is over the bot download limit."""
    if not size or size <= TELEGRAM_DOWNLOAD_LIMIT:
        return ""
    return (f"That one's {size / 1024 / 1024:.1f} MB, and Telegram only lets bots "
            "download files up to 20 MB, so it never reaches me. Send it as an mp3 "
            "(a 4-minute song is about 5-10 MB), or zip it, or drop a link.")


def audio_prompt(path, *, name: str = "", caption: str = "", title: str = "",
                 seconds: int | None = None) -> str:
    """What she's told when he sends a song or recording."""
    length = f"{seconds // 60}:{seconds % 60:02d}" if seconds else "unknown length"
    return (
        f"[User sent an audio file — saved to {path}]\n"
        f"Filename: {name or getattr(path, 'name', path)}"
        + (f"\nTrack: {title}" if title else "")
        + f"\nLength: {length}\nCaption: {caption or '(none)'}\n"
        "You can't listen to music, so don't describe how it sounds. You can pass it "
        f"on: send_message_to_guest(file=\"{path}\") or send_file(path=\"{path}\"), "
        "or put it on your site with site_add_asset."
    )
