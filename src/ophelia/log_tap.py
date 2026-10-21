"""A copy of her terminal output that can be read from elsewhere.

She runs on a phone at home; the owner can't see the terminal from work. A
structlog processor keeps the recent log lines (plain text, secrets
redacted) in a ring buffer with sequence numbers, so the Discord bot can
stream them into a #terminal channel and Telegram's /logs can show the tail.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from typing import Any

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40,
           "exception": 40, "critical": 50}

_lines: deque[tuple[int, int, str]] = deque(maxlen=1500)  # (seq, level, text)
_seq = 0
_lock = threading.Lock()
_secrets: set[str] = set()

# Log events that would echo the mirror's own work back into it.
_SKIP_PREFIXES = ("terminal_mirror.", "discord_log.post")

# "key=VALUE" style: keep the name, blank the value.
_NAMED_SECRET = re.compile(
    r"(?i)\b((?:bearer|token|api[_-]?key|authorization|secret)(?:\s*[=:]\s*|\s+))"
    r"[^\s,'\"]{8,}"
)
# Values that are secrets on their own.
_BARE_SECRETS = [
    re.compile(r"\b(?:sk|xai)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
]


def register_secrets(values: list[str | None]) -> None:
    """Exact secret values (API keys, bot tokens) to blank out of every line."""
    for v in values:
        v = (v or "").strip()
        if len(v) >= 8:
            _secrets.add(v)


def register_settings_secrets(settings: Any) -> None:
    """Every configured key/token/secret value in Settings."""
    vals = []
    for name in getattr(type(settings), "model_fields", {}) or {}:
        if any(w in name for w in ("key", "token", "secret", "password")):
            v = getattr(settings, name, None)
            if isinstance(v, str):
                vals.append(v)
    register_secrets(vals)


def redact(text: str) -> str:
    for s in _secrets:
        if s in text:
            text = text.replace(s, "[redacted]")
    text = _NAMED_SECRET.sub(r"\1[redacted]", text)
    for pat in _BARE_SECRETS:
        text = pat.sub("[redacted]", text)
    return text


def _render(event_dict: dict[str, Any]) -> str:
    ts = str(event_dict.get("timestamp", ""))[11:19]
    level = str(event_dict.get("level", "info"))
    event = str(event_dict.get("event", ""))
    extras = " ".join(
        f"{k}={str(v)[:200]}" for k, v in event_dict.items()
        if k not in ("timestamp", "level", "event") and not k.startswith("_")
    )
    return f"{ts} {level[:4]:<4} {event}{(' ' + extras) if extras else ''}"


def add(level: int, text: str) -> None:
    global _seq
    with _lock:
        _seq += 1
        _lines.append((_seq, level, redact(text)[:1800]))


def tap(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: copy the line, pass the event on unchanged."""
    try:
        event = str(event_dict.get("event", ""))
        if not event.startswith(_SKIP_PREFIXES):
            level = _LEVELS.get(str(event_dict.get("level", method_name)).lower(), 20)
            add(level, _render(event_dict))
    except Exception:
        pass
    return event_dict


class _StdlibTap(logging.Handler):
    """Library warnings (python-telegram-bot, httpx, discord) too."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            add(record.levelno, f"{record.levelname[:4].lower():<4} [{record.name}] "
                                f"{record.getMessage()}")
        except Exception:
            pass


def install_stdlib_tap(level: int = logging.WARNING) -> None:
    root = logging.getLogger()
    if not any(isinstance(h, _StdlibTap) for h in root.handlers):
        h = _StdlibTap(level)
        root.addHandler(h)


def since(seq: int, *, min_level: int = 20) -> tuple[int, list[str]]:
    """Lines after ``seq`` at or above ``min_level``, and the newest seq."""
    with _lock:
        latest = _seq
        out = [t for s, lvl, t in _lines if s > seq and lvl >= min_level]
    return latest, out


def tail(n: int = 30, *, min_level: int = 20) -> list[str]:
    with _lock:
        out = [t for _, lvl, t in _lines if lvl >= min_level]
    return out[-n:]


def level_number(name: str) -> int:
    return _LEVELS.get((name or "info").strip().lower(), 20)
