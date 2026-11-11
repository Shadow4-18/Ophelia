"""Background work that doesn't vanish.

Two problems with bare asyncio.create_task():

- A task nobody keeps a reference to can be garbage-collected before it
  finishes (Python's docs warn about this), so a delayed follow-through or
  wake-up could silently never happen. spawn() keeps it referenced until it
  ends and logs it if it crashes.
- A long-running loop (consciousness, dreams, backups...) that raised was
  gone for good, without a word, while everything else carried on.
  supervise() logs the crash and starts the loop again after a pause, with
  a cap so a loop that crashes on start can't spin.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import structlog

log = structlog.get_logger()

_TASKS: set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("background.task_failed", task=task.get_name(),
                    error=f"{type(exc).__name__}: {exc}"[:300])


def spawn(coro: Coroutine[Any, Any, Any], *, name: str = "") -> asyncio.Task:
    """create_task that keeps the task alive until done and logs a crash."""
    task = asyncio.create_task(coro, name=name or None)
    _TASKS.add(task)
    task.add_done_callback(_finished)
    return task


def pending() -> int:
    return len(_TASKS)


async def _run_supervised(name: str, factory: Callable[[], Awaitable[Any]], *,
                          backoff: float, max_restarts: int, window: float) -> None:
    crashes: list[float] = []
    while True:
        try:
            await factory()
            return  # the loop ended on its own (stopped): done
        except asyncio.CancelledError:
            raise
        except Exception as e:
            now = time.monotonic()
            crashes = [t for t in crashes if now - t < window] + [now]
            if len(crashes) > max_restarts:
                log.error("background.loop_gave_up", loop=name, crashes=len(crashes),
                          error=f"{type(e).__name__}: {e}"[:300])
                return
            delay = backoff * len(crashes)
            log.error("background.loop_crashed", loop=name, restart_in=delay,
                      error=f"{type(e).__name__}: {e}"[:300], exc_info=True)
            await asyncio.sleep(delay)


def supervise(name: str, factory: Callable[[], Awaitable[Any]], *, backoff: float = 15.0,
              max_restarts: int = 5, window: float = 3600.0) -> asyncio.Task:
    """Run ``factory()`` (e.g. ``lambda: self.dream.run()``) and restart it
    when it raises: after ``backoff`` seconds, longer each time, at most
    ``max_restarts`` times an hour."""
    return asyncio.create_task(
        _run_supervised(name, factory, backoff=backoff, max_restarts=max_restarts,
                        window=window), name=f"loop:{name}")
