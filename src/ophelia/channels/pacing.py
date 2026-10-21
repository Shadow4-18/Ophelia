"""Human timing for her replies.

She used to answer within seconds whatever the hour or the length. A person
shows "typing..." for about as long as the message takes to write, sends a
long thought as a couple of messages with a beat between them, and takes a
moment when they've just been woken up. The thinking she already did counts
toward the first message, so short replies don't get slower than they were.
"""

from __future__ import annotations

import random
import time
from typing import Any

_CHARS_PER_SECOND = 30.0  # brisk phone typing; long replies are capped below
_MAX_TYPING_S = 7.0
_BETWEEN_MESSAGES_S = (0.8, 2.2)


def typing_seconds(text: str, *, first: bool, elapsed: float) -> float:
    """How long to show 'typing...' before sending ``text``."""
    t = min(_MAX_TYPING_S, 0.6 + len(text or "") / _CHARS_PER_SECOND)
    if first:
        return max(0.0, t - elapsed)
    return t + random.uniform(*_BETWEEN_MESSAGES_S)


def sleepy_seconds(settings: Any) -> float:
    """Extra wait when his message woke her or she's barely awake."""
    try:
        from ophelia.mind import body_clock

        clock = body_clock.get(settings)
    except Exception:
        clock = None
    if clock is None:
        return 0.0
    st = clock.state
    if st.woken_by_owner and st.woke_at and time.time() - st.woke_at < 180:
        return random.uniform(15, 75) if random.random() < 0.7 else 0.0
    if clock.phase() == "drowsy":
        return random.uniform(3, 15)
    return 0.0


def enabled(settings: Any) -> bool:
    return getattr(settings, "human_pacing", False) is True
