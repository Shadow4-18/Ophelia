"""/stop: cancel whatever she's in the middle of.

Chat turns (one long tool chain can run for minutes, e.g. playing a game)
and autonomous thinking turns register their asyncio task here while they
run. ``stop_all()`` cancels them; each one cleans up in its own finally
blocks (flags reset, model gate released) and the loops keep running.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger()

_running: dict[asyncio.Task, str] = {}
_yields: set[asyncio.Task] = set()  # her own turns that give way when he writes


def track(task: asyncio.Task, label: str, *, yields_to_owner: bool = False) -> asyncio.Task:
    _running[task] = label
    if yields_to_owner:
        _yields.add(task)

    def done(t: asyncio.Task) -> None:
        _running.pop(t, None)
        _yields.discard(t)

    task.add_done_callback(done)
    return task


def yield_to_owner() -> list[str]:
    """He just wrote: stop what she started on her own (a thought, a voice
    note nobody asked for) so his conversation isn't run over by it."""
    stopped = []
    for task in list(_yields):
        if not task.done():
            task.cancel()
            stopped.append(_running.get(task, "own turn"))
    if stopped:
        log.info("interrupts.yielded_to_owner", what=stopped)
    return stopped


def running() -> list[str]:
    return [label for t, label in _running.items() if not t.done()]


def stop_all(*, except_task: asyncio.Task | None = None) -> list[str]:
    """Cancel every tracked task. Returns what was stopped."""
    stopped = []
    for task, label in list(_running.items()):
        if task is except_task or task.done():
            continue
        task.cancel()
        stopped.append(label)
    if stopped:
        log.info("interrupts.stopped", what=stopped)
    return stopped
