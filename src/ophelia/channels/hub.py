"""Run multiple chat gateways (Telegram, Discord, …) together."""

from __future__ import annotations

import asyncio

import structlog

from ophelia.channels.base import ChatGateway
from ophelia.channels.discord_bot import DiscordGateway
from ophelia.channels.session import ChannelSession
from ophelia.channels.telegram_bot import TelegramGateway
from ophelia.config import Settings
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.memory.store import MemoryStore
from ophelia.mind.drives import DriveState

log = structlog.get_logger()


class ChannelHub:
    def __init__(
        self,
        settings: Settings,
        agent: AgentLoop,
        signals: Signals,
        memory: MemoryStore,
        drives: DriveState,
        *,
        games=None,
        vision=None,
    ) -> None:
        self.settings = settings
        self.signals = signals
        self.session = ChannelSession(
            agent, signals, memory, drives, games=games, vision=vision
        )
        self._gateways: list[ChatGateway] = []

        if settings.telegram_enabled:
            self._gateways.append(
                TelegramGateway(
                    settings,
                    self.session,
                    signals,
                    games=games,
                    vision=vision,
                )
            )
        if settings.discord_enabled:
            dc = DiscordGateway(
                settings,
                self.session,
                signals,
            )
            dc.register_log_hooks(self.session)
            self._gateways.append(dc)

    def gateways(self) -> list[ChatGateway]:
        return list(self._gateways)

    def configured_names(self) -> list[str]:
        return [g.platform for g in self._gateways if g.is_configured()]

    def require_any(self) -> None:
        active = [g for g in self._gateways if g.is_configured()]
        if not active:
            raise RuntimeError(
                "No chat channels configured. Set at least one:\n"
                "  TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS\n"
                "  DISCORD_BOT_TOKEN + DISCORD_ALLOWED_USER_IDS\n"
                "Or use: ophelia ui / ophelia chat (no bot needed)"
            )

    async def broadcast_proactive(self, text: str) -> None:
        from ophelia.channels.message_split import split_messages

        chunks = split_messages(text)
        for gw in self._gateways:
            if not gw.is_configured():
                continue
            for i, chunk in enumerate(chunks):
                try:
                    if i:
                        await asyncio.sleep(1.2)
                    await gw.send_proactive(chunk)
                    mirror = getattr(gw, "mirror_consciousness", None)
                    if callable(mirror):
                        try:
                            await mirror(chunk)
                        except Exception as e:
                            log.warning("hub.consciousness_mirror_failed", platform=gw.platform, error=str(e))
                except Exception as e:
                    log.warning("hub.proactive_failed", platform=gw.platform, error=str(e))

    async def broadcast_proactive_media(
        self, paths: list, *, caption: str = ""
    ) -> None:
        """Forward media artifacts produced during an autonomous turn."""
        if not paths:
            return
        for gw in self._gateways:
            if not gw.is_configured():
                continue
            sender = getattr(gw, "send_proactive_media", None)
            if not callable(sender):
                continue
            for p in paths:
                try:
                    await sender(p, caption=caption)
                except TypeError:
                    await sender(p)
                except Exception as e:
                    log.warning("hub.proactive_media_failed", platform=gw.platform, error=str(e))

    async def broadcast_proactive_voice(self, text: str) -> None:
        """Synthesize and send a spontaneous voice note to the owner.

        Tier C #11: send to ALL configured gateways (Telegram + Discord),
        not just the first one that succeeds — true parity means Discord
        users get voice notes too, even when Telegram is also configured.
        """
        sent_any = False
        for gw in self._gateways:
            if not gw.is_configured():
                continue
            sender = getattr(gw, "send_proactive_voice", None)
            if not callable(sender):
                continue
            try:
                await sender(text)
                sent_any = True
            except Exception as e:
                log.warning("hub.proactive_voice_failed", platform=gw.platform, error=str(e))
        if not sent_any:
            # No gateway could send voice — fall back to text so the message
            # isn't lost entirely.
            await self.broadcast_proactive(text)

    async def prepare(self) -> None:
        """Start gateway APIs (e.g. Telegram bot) before consciousness outreach."""
        self.require_any()
        for gw in self._gateways:
            if not gw.is_configured():
                continue
            prepare = getattr(gw, "prepare", None)
            if callable(prepare):
                await prepare()

    async def run(self) -> None:
        self.require_any()
        active = [g for g in self._gateways if g.is_configured()]
        log.info("hub.starting", platforms=[g.platform for g in active])
        tasks = [asyncio.create_task(g.run()) for g in active]
        try:
            while not self.signals.terminate:
                await asyncio.sleep(1)
        finally:
            for gw in active:
                try:
                    await gw.stop()
                except Exception as e:
                    log.warning("hub.stop_failed", platform=gw.platform, error=str(e))
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def mirror_inner_thought(self, text: str) -> None:
        for gw in self._gateways:
            mirror = getattr(gw, "mirror_inner_thought", None)
            if callable(mirror):
                try:
                    await mirror(text)
                except Exception as e:
                    log.warning("hub.inner_mirror_failed", platform=gw.platform, error=str(e))
