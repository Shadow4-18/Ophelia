"""Dream / consolidation loop — Ophelia's sleep reflection.

Runs on a long cadence (default ~4h). It replays the day's episodes,
extracts lessons, prunes stale MEMORY.md entries, and writes a dream-style
reflection to the inner monologue she can re-read on wake.

This is the "offline self-improvement" phase that complements the live
consciousness tick: instead of reacting, she consolidates.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.memory.store import MemoryStore
from ophelia.mind.inner_log import InnerMonologue
from ophelia.providers.model_gate import get_model_gate

log = structlog.get_logger()

DREAM_PROMPT = """You are dreaming — consolidating the day's experience offline.

Given a transcript of recent conversations and your recent inner thoughts,
do all of the following, output as JSON:
{
  "dream": "a short, impressionistic 'dream' narrative (2-4 sentences) weaving the day together — surreal is fine",
  "lessons": ["0-3 durable principles or corrections to remember, empty if none"],
  "mood_shift": "how this reflection should nudge your baseline mood (e.g. 'slightly more content', or 'none')",
  "memory_to_forget": "an optional snippet/topic that is no longer worth holding in active memory (or 'none')"
}
"""


class DreamLoop:
    def __init__(
        self,
        agent: AgentLoop,
        memory: MemoryStore,
        signals: Signals,
        inner: InnerMonologue | None,
        *,
        interval_hours: float = 4.0,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.agent = agent
        self.memory = memory
        self.signals = signals
        self.inner = inner
        self.interval = interval_hours * 3600
        self.notify = notify
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        log.info("dream.started", interval_hours=self.interval / 3600)
        while self._running and not self.signals.terminate:
            interval = self.interval
            if self.agent.life and self.agent.life.is_sleep_mode():
                interval = min(interval, 3600.0)
            await asyncio.sleep(interval)
            if self.signals.terminate or self.signals.autonomy_paused:
                continue
            if self.signals.user_talking or self.signals.agent_thinking:
                continue
            if get_model_gate().is_busy():
                continue
            try:
                await self._dream_cycle()
            except Exception as e:
                log.warning("dream.error", error=str(e))

    async def _dream_cycle(self) -> None:
        recent = await self.memory.recent_global(limit=60)
        if len(recent) < 4:
            log.info("dream.skip", reason="not enough recent experience")
            return
        transcript_lines: list[str] = []
        for m in recent:
            if m["role"] not in ("user", "assistant"):
                continue
            transcript_lines.append(f"[{m['channel']}] {m['role']}: {m['content'][:200]}")
        transcript = "\n".join(transcript_lines[-40:])
        inner_tail = self.inner.tail(20)[:2000] if self.inner else ""

        client = await self.agent._client("curator")
        model = self.agent._model("curator")
        gate = get_model_gate()
        provider = self.agent.stack.name("curator")  # type: ignore[attr-defined]
        dream_prompt = DREAM_PROMPT
        if self.agent.life and self.agent.life.is_sleep_mode():
            dream_prompt += (
                "\n\nOwner likely asleep — deep sleep consolidation. "
                "More abstract, dreamlike. Optional soft whisper only if meaningful."
            )
        messages = [
            {"role": "system", "content": dream_prompt},
            {"role": "user", "content": f"Recent experience:\n{transcript}\n\nRecent inner thoughts:\n{inner_tail or '(none)'}"},
        ]
        from ophelia.providers.fallback import call_with_fallback, extra_body_for

        async def _make_call(cl, mdl, prov):
            return await cl.chat.completions.create(
                model=mdl,
                messages=messages,
                extra_body=extra_body_for(self.agent.settings, prov),
            )

        try:
            resp = await call_with_fallback(
                self.agent.settings,
                self.agent.stack,  # type: ignore[attr-defined]
                role="curator",
                primary_provider=provider,
                primary_model=model,
                primary_client=client,
                make_call=_make_call,
                gate=gate,
                log_tag="dream.fallback",
            )
        except Exception as e:
            from ophelia.providers.errors import api_error_detail

            log.warning("dream.llm_failed", error=api_error_detail(e))
            return
        raw = (resp.choices[0].message.content or "").strip()

        import json
        import re

        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            log.info("dream.no_json", raw=raw[:300])
            return
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            log.info("dream.bad_json", raw=raw[:300])
            return

        dream = str(parsed.get("dream") or "").strip()
        lessons = parsed.get("lessons") or []
        mood_shift = str(parsed.get("mood_shift") or "").strip()
        forget = str(parsed.get("memory_to_forget") or "").strip()

        saved_lessons = 0
        if isinstance(lessons, list):
            for les in lessons:
                if isinstance(les, str) and les.strip():
                    await self.memory.add_lesson(les.strip(), context=f"dream:{dream[:200]}")
                    saved_lessons += 1

        if dream and self.inner:
            try:
                await self.inner.write(
                    f"Dream: {dream}\nLessons: {saved_lessons}. Mood shift: {mood_shift}.",
                    kind="dream",
                )
            except Exception:
                pass
        if dream:
            await self.memory.set_fact(f"memory:{int(time.time())}", f"[dream] {dream}")
            await self.memory.append_message(
                "consciousness", "assistant", f"[dream] {dream}",
                metadata={"type": "dream"},
            )
            # Tier B #10: record for morning continuity so she can reference
            # the dream on the next sleep->wake transition instead of it
            # disappearing into the memory stream.
            try:
                from ophelia.mind.morning import DreamContinuity

                await DreamContinuity(self.memory).record_dream(dream)
            except Exception as e:
                log.debug("dream.morning_record_failed", error=str(e))

        # Apply mood shift nudge to baseline if present.
        if mood_shift and mood_shift.lower() not in ("none", "n/a", ""):
            try:
                from ophelia.mind.psyche import PsycheState

                if isinstance(self.agent.psyche, PsycheState) and "content" in mood_shift.lower():
                    self.agent.psyche.mood.baseline_valence = min(
                        1.0, self.agent.psyche.mood.baseline_valence + 0.05
                    )
                elif "anxious" in mood_shift.lower() or "tense" in mood_shift.lower():
                    self.agent.psyche.mood.baseline_arousal = min(
                        1.0, self.agent.psyche.mood.baseline_arousal + 0.05
                    )
                elif "calm" in mood_shift.lower():
                    self.agent.psyche.mood.baseline_arousal = max(
                        0.0, self.agent.psyche.mood.baseline_arousal - 0.05
                    )
                await self.memory.save_psyche(self.agent.psyche)
            except Exception:
                pass

        # Prune: if a forget topic is given, note it (actual MEMORY.md pruning is
        # handled by the curator's dedup; here we just record the intent).
        if forget and forget.lower() not in ("none", "n/a", ""):
            await self.memory.set_fact(
                f"forget:{int(time.time())}", forget[:300]
            )

        log.info(
            "dream.cycle_done",
            lessons=saved_lessons,
            mood_shift=mood_shift or "none",
            has_dream=bool(dream),
        )
