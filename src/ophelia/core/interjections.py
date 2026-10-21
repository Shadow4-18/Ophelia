"""Messages that arrive while she's in the middle of something.

A long chat turn (playing a game, a chain of tool calls) used to hold every
new owner message until it finished, so a quick "how's it going?" got its
answer minutes later, after the whole task. Now the running turn opens a
mailbox for its channel; a new owner message is posted there instead of
queueing. Between steps the turn picks it up, she answers right away, and
then carries on where she left off.

If the turn ends before it gets to a posted message, the message is handed
back and handled as a normal turn, so nothing is lost.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# How often a waiting sender checks whether its message was picked up or the
# turn ended. The turn itself checks the mailbox once per step.
_POLL_S = 0.25


@dataclass
class Note:
    text: str
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


def open_for(channel: str) -> None:
    _open[channel] = _open.get(channel, 0) + 1


def close_for(channel: str) -> None:
    n = _open.get(channel, 0) - 1
    if n <= 0:
        _open.pop(channel, None)
        # Anything still here was never taken; its sender sees the turn
        # closed and runs it normally.
        _mail.pop(channel, None)
    else:
        _open[channel] = n


def accepting(channel: str) -> bool:
    return _open.get(channel, 0) > 0


def take_all(channel: str) -> list[str]:
    """Messages waiting for the running turn on ``channel`` (claimed)."""
    notes = _mail.pop(channel, [])
    return [n.text for n in notes if n.take()]


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
