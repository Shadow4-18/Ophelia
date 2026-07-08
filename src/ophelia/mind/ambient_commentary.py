"""Ambient screen commentary — glance at the phone and say something, with anti-spam."""

from __future__ import annotations

import asyncio
import time

import structlog

from ophelia.config import Settings
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.mind.initiative import InitiativeGovernor
from ophelia.mind.life_context import LifeContext

log = structlog.get_logger()


class AmbientCommentaryLoop:
    def __init__(
        self,
        settings: Settings,
        agent: AgentLoop,
        signals: Signals,
        life: LifeContext,
        governor: InitiativeGovernor,
        vision,
        *,
        notify,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.signals = signals
        self.life = life
        self.governor = governor
        self.vision = vision
        self.notify = notify
        self._running = False
        self._last_at = 0.0

    async def run(self) -> None:
        if not self.vision or not self.settings.ambient_commentary_enabled:
            return
        self._running = True
        log.info("ambient_commentary.started")
        while self._running and not self.signals.terminate:
            await asyncio.sleep(60)
            if not self.life.commentary_allowed(last_commentary_at=self._last_at):
                continue
            if self.signals.user_talking or self.signals.agent_thinking:
                continue
            allowed, reason = self.governor.allow_outreach()
            if not allowed:
                continue
            await self._maybe_comment()

    async def _maybe_comment(self) -> None:
        try:
            seen = await self.vision.explore_cycle("ambient glance — what's on screen?")
        except Exception as e:
            log.debug("ambient_commentary.vision_skip", error=str(e))
            return
        if not seen or len(seen) < 20:
            return
        channel = self.settings.primary_user_channel() or "consciousness"
        try:
            reply = await self.agent.run_turn(
                channel,
                f"You just glanced at your phone screen:\n{seen[:2000]}\n\n"
                "If something catches your eye and you'd actually nudge a friend "
                "about it, say one short line. Otherwise just let it go — no "
                "need to narrate or acknowledge boring UI. Silence is fine.",
                system_extra=(
                    self.life.to_context_block()
                    + "\n\nDon't describe what's on screen. Only speak if it's "
                    "the kind of thing you'd actually text someone about."
                ),
            )
        except Exception as e:
            log.debug("ambient_commentary.turn_skip", error=str(e))
            return
        text = (reply or "").strip()
        # Drop empty, SKIP-literal, or suspiciously long responses (the model
        # sometimes ignores "silence is fine" and writes a paragraph). Short
        # genuine asides pass through.
        if not text or text.upper() == "SKIP" or len(text) > 200:
            return
        allowed, reason = self.governor.allow_outreach()
        if not allowed:
            return
        try:
            await self.notify(text)
            self.governor.record_outreach("ambient_commentary", 0.5, text)
            self._last_at = time.time()
            log.info("ambient_commentary.sent", preview=text[:80])
        except Exception as e:
            log.warning("ambient_commentary.send_failed", error=str(e))

    def stop(self) -> None:
        self._running = False
