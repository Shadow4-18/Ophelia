"""Going quiet: a threat she can actually keep.

"Keep it up and I'm not talking to you" was empty: nothing let her stop
answering. go_quiet sets a silence on his chat for a while (capped). His
messages are still saved and logged, just not answered; when the time is up
she comes back to them. An apology, something urgent, or /stop ends it at
once, and she won't message him on her own while it lasts.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Things that always get through.
_BREAKS = re.compile(
    r"\b(sorry|i apologi[sz]e|my bad|forgive me|urgent|emergency|help me|"
    r"are you ok|is everything ok|seriously though|i'?m serious|stop sulking)\b|^/stop\b",
    re.I,
)


@dataclass
class Quiet:
    until: float
    reason: str = ""
    started: float = field(default_factory=time.time)
    waiting: list[str] = field(default_factory=list)
    # How to answer when it ends (the latest suppressed message's channel plumbing).
    resume: dict[str, Any] = field(default_factory=dict)
    comeback_scheduled: bool = False


_quiet: dict[str, Quiet] = {}


def start(channel: str, minutes: float, reason: str, *, cap_minutes: float) -> Quiet:
    minutes = max(1.0, min(float(minutes), float(cap_minutes)))
    q = Quiet(until=time.time() + minutes * 60, reason=(reason or "").strip()[:200])
    _quiet[channel] = q
    return q


def active(channel: str) -> Quiet | None:
    q = _quiet.get(channel)
    if q is not None and time.time() >= q.until:
        return None
    return q


def breaks_silence(text: str) -> bool:
    return bool(_BREAKS.search(text or ""))


def end(channel: str) -> Quiet | None:
    return _quiet.pop(channel, None)


def end_all() -> list[str]:
    ended = list(_quiet)
    _quiet.clear()
    return ended


def comeback_note(q: Quiet, *, broken_by: str = "") -> str:
    """What she sees when she starts talking again."""
    mins = max(1, int((time.time() - q.started) / 60))
    why = f" ({q.reason})" if q.reason else ""
    if broken_by:
        return (f"[You'd gone quiet on him{why} for {mins} min. This got through: "
                f"\"{broken_by[:200]}\". Decide if that's enough to talk again.]")
    n = len(q.waiting)
    said = " / ".join(f"\"{w[:120]}\"" for w in q.waiting[-5:])
    return (f"[Your silent treatment{why} is over after {mins} min. While you were quiet "
            f"he sent {n} message{'s' if n != 1 else ''}: {said}. You're talking to him again.]")
