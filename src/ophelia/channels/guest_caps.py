"""Daily caps on what plain guests can have her make.

Images, videos and voice notes cost real credits (Grok, Civitai, TTS), and
without a cap anyone the owner let in could burn them as fast as she'd make
things. Each plain guest gets a per-day allowance of each kind; friends and
the owner have none. Only things that were actually made count (a refusal
or a failure is free), and the day turns over at midnight in her timezone.

0 turns a kind off for guests; a negative number means no cap.
Counts live in data/guest_usage.json (today's only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from ophelia.fileio import atomic_write_json

log = structlog.get_logger()

# tool → (setting, what to call it)
CAPPED: dict[str, tuple[str, str]] = {
    "generate_image": ("guest_daily_images", "images"),
    "generate_video": ("guest_daily_videos", "videos"),
    "text_to_speech": ("guest_daily_voice", "voice notes"),
}


def _path(settings: Any) -> Path:
    return Path(settings.data_dir) / "guest_usage.json"


def _today(settings: Any) -> str:
    from ophelia.mind import day_log

    return day_log._now(settings).strftime("%Y-%m-%d")


def _load(settings: Any) -> dict:
    try:
        data = json.loads(_path(settings).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) and data.get("day") == _today(settings) else {}


def limit(settings: Any, tool: str) -> int | None:
    """The day's allowance for ``tool`` (None = uncapped)."""
    if tool not in CAPPED:
        return None
    try:
        n = int(getattr(settings, CAPPED[tool][0]))
    except (TypeError, ValueError):
        return None
    return None if n < 0 else n


def used(settings: Any, channel: str, tool: str) -> int:
    return int(_load(settings).get("counts", {}).get(channel, {}).get(tool, 0))


def check(settings: Any, channel: str, tool: str) -> str:
    """'' when the guest may have another; otherwise what to tell her."""
    cap = limit(settings, tool)
    if cap is None or not channel:
        return ""
    have = used(settings, channel, tool)
    if have < cap:
        return ""
    what = CAPPED[tool][1]
    log.info("guest_caps.reached", channel=channel, tool=tool, cap=cap)
    if cap == 0:
        return (f"Not available: guests can't have {what} (your owner's setting). "
                "Tell them in your own words; don't pretend you made it.")
    return (f"Daily limit reached: this guest has had their {cap} {what} for today. "
            "It resets at midnight. Tell them in your own words (tease them about "
            "it if you like); don't pretend you made it and don't try another tool "
            "to get around it.")


def made(result: str) -> int:
    """How many things a tool result says were actually made."""
    text = str(result or "")
    low = text.lower()
    n = low.count("saved to")
    if n:
        return n
    return 1 if low.startswith("pending:") else 0


def record(settings: Any, channel: str, tool: str, result: str) -> None:
    n = made(result)
    if not n or not channel or limit(settings, tool) is None:
        return
    data = _load(settings) or {"day": _today(settings), "counts": {}}
    counts = data.setdefault("counts", {}).setdefault(channel, {})
    counts[tool] = int(counts.get(tool, 0)) + n
    try:
        path = _path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except Exception as e:
        log.debug("guest_caps.save_failed", error=str(e))
