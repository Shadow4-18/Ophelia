"""Ask the owner yes/no before something dangerous, and wait for the answer.

The Telegram gateway registers how to ask (a message with Allow / Deny
buttons) and calls answer() when a button is pressed. A tool that needs
permission awaits ask(); with nobody reachable to ask, the answer is no.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Awaitable, Callable

import structlog

log = structlog.get_logger()

# (approval id, question) -> whether the question reached the owner.
Asker = Callable[[str, str], Awaitable[bool]]

_asker: Asker | None = None
_pending: dict[str, asyncio.Future[bool]] = {}
_ids = itertools.count(1)

DEFAULT_TIMEOUT_S = 300.0


def set_asker(fn: Asker | None) -> None:
    global _asker
    _asker = fn


def available() -> bool:
    return _asker is not None


async def ask(question: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """'allowed' | 'denied' | 'timeout' | 'unavailable'."""
    if _asker is None:
        return "unavailable"
    approval_id = f"{next(_ids)}"
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    _pending[approval_id] = fut
    try:
        try:
            sent = await _asker(approval_id, question)
        except Exception as e:
            log.warning("owner_approval.ask_failed", error=str(e))
            sent = False
        if not sent:
            return "unavailable"
        log.info("owner_approval.asked", id=approval_id, question=question[:160])
        try:
            allowed = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.info("owner_approval.timeout", id=approval_id)
            return "timeout"
        log.info("owner_approval.answered", id=approval_id, allowed=allowed)
        return "allowed" if allowed else "denied"
    finally:
        _pending.pop(approval_id, None)


def answer(approval_id: str, allowed: bool) -> bool:
    """A button was pressed. False when that question is no longer waiting."""
    fut = _pending.get(approval_id)
    if fut is None or fut.done():
        return False
    fut.set_result(bool(allowed))
    return True


async def permit(what: str, why: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """'' when the owner allowed ``what``; otherwise the tool result explaining
    why it didn't run."""
    outcome = await ask(f"⚠️ Ophelia wants to run this, which {why}:\n\n{what[:1500]}\n\n"
                        "Allow it?", timeout=timeout)
    if outcome == "allowed":
        return ""
    if outcome == "denied":
        return (f"Not run: Okia said no. (It {why}.) Don't try it another way; ask him "
                "about it if it matters.")
    if outcome == "timeout":
        return (f"Not run: Okia didn't answer the permission request in time. (It {why}.) "
                "Ask him in chat if it still matters.")
    return (f"Not run: it {why}, and that needs Okia's OK, but there's no way to ask him "
            "right now (Telegram isn't connected).")
