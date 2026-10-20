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


def track(task: asyncio.Task, label: str) -> asyncio.Task:
    _running[task] = label
    task.add_done_callback(lambda t: _running.pop(t, None))
    return task


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
