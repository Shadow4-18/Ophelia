"""Messages that arrive while she's in the middle of something.

A long chat turn (playing a game, a chain of tool calls) used to hold every
new owner message until it finished, so a quick "how's it going?" got its
answer minutes later, after the whole task. Now the running turn opens a
mailbox for its channel and keeps a short status of what it's doing.

parallel mode: the message is answered at once by a separate tool-less
reply that knows the status (her mouth), while the task keeps going (her
hands). The exchange is posted to the mailbox as a heads-up, so the task
acts on it only if he asked for something.

pause mode: the message itself is posted; between steps the task picks it
up, she answers, then carries on where she left off.

If the turn ends before it gets to a posted message, the message is handed
back and handled as a normal turn, so nothing is lost.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# How often a waiting sender checks whether its message was picked up or the
# turn ended. The turn itself checks the mailbox once per step.
_POLL_S = 0.25


@dataclass
class Note:
    text: str
    reply: str = ""  # set when she already answered it (parallel mode)
    taken: bool = False
    withdrawn: bool = False
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def take(self) -> bool:
        """Claim for the running turn. Fails if the sender already gave up."""
        if self.withdrawn or self.taken:
            return False
        self.taken = True
        self._event.set()
        return True

    def withdraw(self) -> bool:
        """Sender takes it back to run as its own turn. Fails if taken."""
        if self.taken:
            return False
        self.withdrawn = True
        return True


_open: dict[str, int] = {}  # channel -> running turns accepting messages
_mail: dict[str, list[Note]] = {}
_task: dict[str, str] = {}  # channel -> what she was asked to do
_steps: dict[str, deque] = {}  # channel -> her latest steps, newest last


def open_for(channel: str) -> None:
    _open[channel] = _open.get(channel, 0) + 1


def close_for(channel: str) -> None:
    n = _open.get(channel, 0) - 1
    if n <= 0:
        _open.pop(channel, None)
        # Anything still here was never taken; its sender sees the turn
        # closed and runs it normally.
        _mail.pop(channel, None)
        _task.pop(channel, None)
        _steps.pop(channel, None)
    else:
        _open[channel] = n


def accepting(channel: str) -> bool:
    return _open.get(channel, 0) > 0


def take_all(channel: str) -> list[Note]:
    """Messages waiting for the running turn on ``channel`` (claimed)."""
    notes = _mail.pop(channel, [])
    return [n for n in notes if n.take()]


def set_task(channel: str, request: str) -> None:
    _task[channel] = " ".join((request or "").split())[:300]


def note_step(channel: str, step: str) -> None:
    if accepting(channel):
        _steps.setdefault(channel, deque(maxlen=6)).append(" ".join(step.split())[:240])


def status(channel: str) -> str:
    """What she's in the middle of, for a reply written while it runs."""
    lines = []
    if _task.get(channel):
        lines.append(f"You were asked: {_task[channel]}")
    if _steps.get(channel):
        lines.append("Your latest steps (newest last):")
        lines += [f"- {s}" for s in _steps[channel]]
    return "\n".join(lines) or "(just getting started)"


def post_answered(channel: str, text: str, reply: str) -> bool:
    """Tell the running task about an exchange she already had (parallel
    mode). Fire-and-forget; False when no task is running any more."""
    if not accepting(channel):
        return False
    _mail.setdefault(channel, []).append(Note(text, reply=reply))
    return True


async def offer(channel: str, text: str) -> bool:
    """Post ``text`` to the turn running on ``channel`` and wait until it's
    picked up (True) or that turn ends without it (False: send it as a
    normal turn)."""
    if not accepting(channel):
        return False
    note = Note(text)
    _mail.setdefault(channel, []).append(note)
    log.info("interjection.posted", channel=channel, preview=text[:60])
    while accepting(channel) and not note.taken:
        try:
            await asyncio.wait_for(note._event.wait(), timeout=_POLL_S)
        except asyncio.TimeoutError:
            pass
    if note.taken:
        return True
    note.withdraw()
    return False


def reset() -> None:
    """For tests."""
    _open.clear()
    _mail.clear()
    _task.clear()
    _steps.clear()
