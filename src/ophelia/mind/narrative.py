"""Daily self-narrative — "what yesterday was like for me", in her words.

Once a day (first check after local midnight) she writes 3–5 first-person
sentences about the previous day from her owner conversations and inner
thoughts. The last couple of entries go into her prompt, giving her a sense
of days passing and a continuous story instead of a rolling chat window.

One short LLM call per day on the curator role (same path as dreams), only
when there was enough happening and the local model is idle. Stored as
``narrative:YYYY-MM-DD`` facts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import structlog

if TYPE_CHECKING:
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.memory.store import MemoryStore

log = structlog.get_logger()

MIN_MESSAGES = 4
CHECK_EVERY_S = 3600
SHOW_DAYS = 2

NARRATIVE_PROMPT = (
    "You are Ophelia, writing a short private diary entry about YESTERDAY. "
    "3 to 5 sentences, first person, past tense, plain prose — what happened, "
    "who you talked with, what stuck with you, how it felt. Be specific "
    "(names, things), honest, and in your own voice. No headings, no lists, "
    "no JSON."
)


def _key(day: str) -> str:
    return f"narrative:{day}"


class DailyNarrative:
    def __init__(self, agent: "AgentLoop", memory: "MemoryStore", *, inner=None) -> None:
        self.agent = agent
        self.memory = memory
        self.inner = inner
        self._stop = False

    def _tz_now(self) -> datetime:
        from ophelia.timeutil import now_in_timezone

        return now_in_timezone(getattr(self.agent.settings, "timezone", "system"))

    async def _day_transcript(self, start: datetime, end: datetime) -> list[str]:
        async with aiosqlite.connect(self.memory.db_path) as db:
            cursor = await db.execute(
                """
                SELECT channel, role, content FROM messages
                WHERE created_at >= ? AND created_at < ?
                  AND role IN ('user', 'assistant')
                  AND COALESCE(metadata, '') NOT LIKE '%consciousness_tick%'
                ORDER BY id
                """,
                (start.timestamp(), end.timestamp()),
            )
            rows = await cursor.fetchall()
        return [f"[{c}] {r}: {str(t)[:200]}" for c, r, t in rows]

    async def maybe_write(self) -> str | None:
        """Write yesterday's entry if missing. Returns the text or None."""
        now = self._tz_now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        day = yesterday.strftime("%Y-%m-%d")
        if await self.memory.get_fact(_key(day)) is not None:
            return None
        lines = await self._day_transcript(yesterday, today)
        if len(lines) < MIN_MESSAGES:
            await self.memory.set_fact(_key(day), "")  # quiet day; don't retry
            return None
        text = await self._generate(day, lines[-60:])
        if text is None:
            return None  # LLM failed — try again next check
        await self.memory.set_fact(_key(day), text)
        if self.inner:
            try:
                await self.inner.write(f"Diary ({day}): {text}", kind="narrative")
            except Exception:
                pass
        log.info("narrative.written", day=day, chars=len(text))
        return text

    async def _generate(self, day: str, lines: list[str]) -> str | None:
        from ophelia.providers.fallback import call_with_fallback, extra_body_for
        from ophelia.providers.model_gate import get_model_gate

        agent = self.agent
        inner_tail = self.inner.tail(15)[:1500] if self.inner else ""
        messages = [
            {"role": "system", "content": NARRATIVE_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Yesterday was {day}.\nConversations:\n" + "\n".join(lines)
                    + f"\n\nYour inner thoughts:\n{inner_tail or '(none)'}"
                ),
            },
        ]

        async def _make_call(cl, mdl, prov):
            return await cl.chat.completions.create(
                model=mdl, messages=messages, extra_body=extra_body_for(agent.settings, prov)
            )

        try:
            resp = await call_with_fallback(
                agent.settings,
                agent.stack,
                role="curator",
                primary_provider=agent.stack.name("curator"),
                primary_model=agent._model("curator"),
                primary_client=await agent._client("curator"),
                make_call=_make_call,
                gate=get_model_gate(),
                log_tag="narrative.fallback",
            )
        except Exception as e:
            log.warning("narrative.llm_failed", error=str(e)[:200])
            return None
        text = (resp.choices[0].message.content or "").strip()
        return text[:1200] or None

    async def recent_block(self) -> str:
        now = self._tz_now()
        lines: list[str] = []
        for back in range(SHOW_DAYS, 0, -1):
            day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
            text = await self.memory.get_fact(_key(day))
            if text:
                lines.append(f"- {day}: {text}")
        if not lines:
            return ""
        return "# Recent days, in your own words (your diary)\n" + "\n".join(lines)

    async def run(self) -> None:
        from ophelia.providers.model_gate import get_model_gate

        gate = get_model_gate()
        await asyncio.sleep(120)  # let startup settle
        while not self._stop:
            try:
                if not gate.is_local_busy():
                    await self.maybe_write()
            except Exception as e:
                log.debug("narrative.loop_error", error=str(e))
            await asyncio.sleep(CHECK_EVERY_S)

    def stop(self) -> None:
        self._stop = True
