"""Personality alarms — wake the owner with voice + character, not a buzzer."""

from __future__ import annotations

import asyncio
import time

import structlog

from ophelia.config import Settings
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.mind.life_context import LifeContext

log = structlog.get_logger()


class AlarmLoop:
    def __init__(
        self,
        settings: Settings,
        agent: AgentLoop,
        signals: Signals,
        life: LifeContext,
        *,
        notify_text,
        notify_voice=None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.signals = signals
        self.life = life
        self.notify_text = notify_text
        self.notify_voice = notify_voice
        self._running = False
        self._fired_today: set[str] = set()
        self._last_day = ""

    def _parse_alarms(self) -> list[tuple[int, int]]:
        raw = (self.settings.alarms or "").strip()
        if not raw:
            return []
        out: list[tuple[int, int]] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                h_s, m_s = part.split(":", 1)
            else:
                h_s, m_s = part, "0"
            try:
                out.append((int(h_s.strip()), int(m_s.strip())))
            except ValueError:
                continue
        return out

    async def run(self) -> None:
        if not self._parse_alarms():
            return
        self._running = True
        log.info("alarms.started", times=self.settings.alarms)
        while self._running and not self.signals.terminate:
            await self._tick()
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        now = self.life.now()
        day_key = now.strftime("%Y-%m-%d")
        if day_key != self._last_day:
            self._fired_today.clear()
            self._last_day = day_key
        hm = (now.hour, now.minute)
        for alarm in self._parse_alarms():
            key = f"{day_key}:{alarm[0]:02d}:{alarm[1]:02d}"
            if alarm != hm or key in self._fired_today:
                continue
            self._fired_today.add(key)
            await self._fire_alarm()

    async def _fire_alarm(self) -> None:
        channel = self.settings.primary_user_channel() or "consciousness"
        try:
            text = await self.agent.run_turn(
                channel,
                "[Alarm] Wake-up time. Greet the owner in your voice — warm, "
                "personality-forward, one short spoken paragraph. No task lists.",
                system_extra=self.life.to_context_block(),
            )
        except Exception as e:
            log.warning("alarms.generate_failed", error=str(e))
            text = "Hey. Alarm. I'm up if you are."
        try:
            if self.settings.spontaneous_voice_enabled and self.notify_voice:
                await self.notify_voice(text)
            else:
                await self.notify_text(text)
            log.info("alarms.fired")
        except Exception as e:
            log.warning("alarms.send_failed", error=str(e))

    def stop(self) -> None:
        self._running = False
