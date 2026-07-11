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
        # Give the session a back-reference so /tell, /suggest, and the
        # send_message_to_guest tool can route cross-platform DMs.
        self.session.hub = self
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

    async def send_to_user(self, platform: str, user_id: int, message: str) -> bool:
        """Send a DM to a specific user on the given platform.

        Routes to the correct gateway by platform name. Returns True on
        success, False if the platform isn't configured or the send failed.
        Used by /tell, /suggest, and the send_message_to_guest tool so the
        owner can message guests on any platform from any platform.
        """
        for gw in self._gateways:
            if gw.platform == platform and gw.is_configured():
                sender = getattr(gw, "send_to_user", None)
                if callable(sender):
                    return await sender(user_id, message)
        log.warning("hub.send_to_user_no_gateway", platform=platform)
        return False

    async def send_file_to_user(
        self,
        platform: str,
        user_id: int,
        path,
        *,
        caption: str = "",
    ) -> bool:
        """Send an image/video/audio file to a specific user on a platform."""
        for gw in self._gateways:
            if gw.platform == platform and gw.is_configured():
                sender = getattr(gw, "send_file_to_user", None)
                if callable(sender):
                    try:
                        return bool(
                            await sender(user_id, path, caption=caption)
                        )
                    except TypeError:
                        return bool(await sender(user_id, path, caption))
        log.warning("hub.send_file_to_user_no_gateway", platform=platform)
        return False

    def require_any(self) -> None:
        active = [g for g in self._gateways if g.is_configured()]
        if not active:
            raise RuntimeError(
                "No chat channels configured. Set at least one:\n"
                "  TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS\n"
                "  DISCORD_BOT_TOKEN + DISCORD_ALLOWED_USER_IDS\n"
                "Or use: ophelia ui / ophelia chat (no bot needed)"
            )

    async def broadcast_proactive(self, text: str, *, owners_only: bool = True) -> int:
        """Send spontaneous text to chat platforms.

        By default owners_only=True — consciousness ticks, ambient asides,
        alarms, and inner-mirror previews are for the owner, not a broadcast
        to every guest on the allowlist. Intentional guest outreach uses
        send_to_user / send_message_to_guest instead (Neuro-style DMs).
        Pass owners_only=False only when you truly mean every allowlisted user.

        Returns the number of (gateway, chunk) deliveries that succeeded.
        """
        from ophelia.channels.proactive_filter import is_outreach_junk, proactive_chunks

        if is_outreach_junk(text):
            log.debug("hub.proactive_suppressed", reason="junk", preview=(text or "")[:80])
            return 0
        chunks = proactive_chunks(text)
        if not chunks:
            return 0
        delivered = 0
        for gw in self._gateways:
            if not gw.is_configured():
                continue
            for i, chunk in enumerate(chunks):
                try:
                    if i:
                        await asyncio.sleep(1.2)
                    sender = getattr(gw, "send_proactive", None)
                    if not callable(sender):
                        continue
                    try:
                        result = await sender(chunk, owners_only=owners_only)
                    except TypeError:
                        # Older gateway signature without owners_only.
                        result = await sender(chunk)
                    # Gateways may return an int count, a bool, or None (legacy).
                    if isinstance(result, bool):
                        n = 1 if result else 0
                    elif isinstance(result, (int, float)):
                        n = int(result)
                    else:
                        n = 1
                    delivered += max(0, n)
                    mirror = getattr(gw, "mirror_consciousness", None)
                    if callable(mirror):
                        try:
                            await mirror(chunk)
                        except Exception as e:
                            log.warning("hub.consciousness_mirror_failed", platform=gw.platform, error=str(e))
                except Exception as e:
                    log.warning("hub.proactive_failed", platform=gw.platform, error=str(e))
        if delivered == 0:
            log.warning(
                "hub.proactive_zero_delivery",
                owners_only=owners_only,
                preview=(text or "")[:80],
                gateways=[g.platform for g in self._gateways if g.is_configured()],
            )
        return delivered

    async def broadcast_proactive_media(
        self, paths: list, *, caption: str = "", owners_only: bool = True
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
                    try:
                        await sender(p, caption=caption, owners_only=owners_only)
                    except TypeError:
                        try:
                            await sender(p, caption=caption)
                        except TypeError:
                            await sender(p)
                except Exception as e:
                    log.warning("hub.proactive_media_failed", platform=gw.platform, error=str(e))

    async def broadcast_proactive_voice(self, text: str, *, owners_only: bool = True) -> None:
        """Synthesize and send a spontaneous voice note to the owner.

        Tier C #11: send to ALL configured gateways (Telegram + Discord),
        not just the first one that succeeds — true parity means Discord
        users get voice notes too, even when Telegram is also configured.
        Default owners_only=True so guests don't get spontaneous voice.
        """
        sent_any = False
        for gw in self._gateways:
            if not gw.is_configured():
                continue
            sender = getattr(gw, "send_proactive_voice", None)
            if not callable(sender):
                continue
            try:
                try:
                    await sender(text, owners_only=owners_only)
                except TypeError:
                    await sender(text)
                sent_any = True
            except Exception as e:
                log.warning("hub.proactive_voice_failed", platform=gw.platform, error=str(e))
        if not sent_any:
            # No gateway could send voice — fall back to text so the message
            # isn't lost entirely.
            await self.broadcast_proactive(text, owners_only=owners_only)

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
        from ophelia.channels.proactive_filter import is_outreach_junk

        if is_outreach_junk(text):
            return
        for gw in self._gateways:
            mirror = getattr(gw, "mirror_inner_thought", None)
            if callable(mirror):
                try:
                    await mirror(text)
                except Exception as e:
                    log.warning("hub.inner_mirror_failed", platform=gw.platform, error=str(e))
