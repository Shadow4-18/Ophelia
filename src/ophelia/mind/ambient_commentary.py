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
                f"[Ambient screen glance]\n{seen[:2000]}\n\n"
                "If something is genuinely worth a brief aside to the owner, "
                "say ONE short line (under 120 chars). If nothing interesting or "
                "it would be annoying, reply exactly: SKIP",
                system_extra=(
                    self.life.to_context_block()
                    + "\n\nDo NOT narrate boring UI. Only speak if you'd nudge a friend."
                ),
            )
        except Exception as e:
            log.debug("ambient_commentary.turn_skip", error=str(e))
            return
        text = (reply or "").strip()
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
