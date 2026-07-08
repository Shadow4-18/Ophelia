"""Inner monologue stream — watch her think (file + optional Telegram mirror)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import structlog

from ophelia.config import OPHELIA_HOME

log = structlog.get_logger()


class InnerMonologue:
    def __init__(
        self,
        log_path: Path | None = None,
        *,
        mirror_telegram: bool = False,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.log_path = log_path or (OPHELIA_HOME / "data" / "inner_monologue.md")
        self.mirror_telegram = mirror_telegram
        self.notify = notify
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.is_file():
            self.log_path.write_text("# Ophelia inner monologue\n\n", encoding="utf-8")

    async def write(
        self,
        thought: str,
        *,
        kind: str = "inner",
        mood: str = "",
        pressure: float | None = None,
    ) -> None:
        thought = thought.strip()
        if not thought:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meta = f" [{kind}]"
        if mood:
            meta += f" mood={mood}"
        if pressure is not None:
            meta += f" pressure={pressure:.2f}"
        line = f"\n## {ts}{meta}\n{thought}\n"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

        if self.notify:
            from ophelia.channels.proactive_filter import is_outreach_junk

            if not is_outreach_junk(thought):
                try:
                    await self.notify(thought)
                except Exception as e:
                    log.warning("inner.notify_failed", error=str(e))

    def tail(self, lines: int = 40) -> str:
        if not self.log_path.is_file():
            return "(empty)"
        text = self.log_path.read_text(encoding="utf-8")
        parts = text.strip().split("\n")
        return "\n".join(parts[-lines:])
