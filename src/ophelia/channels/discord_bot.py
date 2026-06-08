from __future__ import annotations

import asyncio

import structlog

from ophelia.channels.session import ChannelSession
from ophelia.config import Settings
from ophelia.core.signals import Signals

log = structlog.get_logger()


class DiscordGateway:
    platform = "discord"

    def __init__(
        self,
        settings: Settings,
        session: ChannelSession,
        signals: Signals,
    ) -> None:
        self.settings = settings
        self.session = session
        self.signals = signals
        self._bot = None
        self._task: asyncio.Task | None = None

    def is_configured(self) -> bool:
        return bool(self.settings.discord_bot_token)

    def _allowed(self, user_id: int) -> bool:
        allowed = self.settings.allowed_discord_users()
        if allowed is None:
            return True
        return user_id in allowed

    def _build_bot(self):
        import discord
        from discord.ext import commands

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
        gw = self

        @bot.event
        async def on_ready() -> None:
            log.info("discord.ready", user=str(bot.user))

        async def _check(ctx) -> bool:
            if ctx.author.bot:
                return False
            if not gw._allowed(ctx.author.id):
                await ctx.send("Unauthorized.")
                return False
            return True

        @bot.command(name="start")
        async def cmd_start(ctx) -> None:
            if not await _check(ctx):
                return
            await ctx.send(gw.session.WELCOME[:2000])

        @bot.command(name="pause")
        async def cmd_pause(ctx) -> None:
            if not await _check(ctx):
                return
            await gw.session.cmd_pause(lambda t: ctx.send(t[:2000]))

        @bot.command(name="resume")
        async def cmd_resume(ctx) -> None:
            if not await _check(ctx):
                return
            await gw.session.cmd_resume(lambda t: ctx.send(t[:2000]))

        @bot.command(name="listen")
        async def cmd_listen(ctx, arg: str = "status") -> None:
            if not await _check(ctx):
                return
            await gw.session.cmd_listen(arg, lambda t: ctx.send(t[:2000]))

        @bot.command(name="inner")
        async def cmd_inner(ctx, arg: str = "status") -> None:
            if not await _check(ctx):
                return
            await gw.session.cmd_inner(arg, lambda t: ctx.send(t[:2000]))

        @bot.command(name="voice")
        async def cmd_voice(ctx, arg: str = "status") -> None:
            if not await _check(ctx):
                return
            channel = f"discord:{ctx.author.id}"
            await gw.session.cmd_voice(
                channel,
                arg,
                lambda t: ctx.send(t[:2000]),
                default=gw.settings.voice_reply_default,
            )

        @bot.command(name="game")
        async def cmd_game(ctx, arg: str = "status", *rest: str) -> None:
            if not await _check(ctx):
                return
            from ophelia.android.factory import build_android_body

            def _android():
                if gw.settings.android_enabled:
                    return build_android_body(gw.settings)
                return None

            await gw.session.cmd_game(
                arg,
                list(rest),
                lambda t: ctx.send(t[:2000]),
                primary_channel=gw.settings.primary_user_channel(),
                android_factory=_android,
            )

        @bot.event
        async def on_message(message) -> None:
            if message.author.bot:
                return
            if not gw._allowed(message.author.id):
                return
            if message.content.startswith("!"):
                await bot.process_commands(message)
                return
            channel = f"discord:{message.author.id}"
            async with message.channel.typing():
                await gw.session.handle_chat(
                    channel,
                    message.content.strip(),
                    lambda t: message.channel.send(t[:2000]),
                )

        self._bot = bot
        return bot

    async def run(self) -> None:
        token = self.settings.discord_bot_token
        if not token:
            raise RuntimeError("Set DISCORD_BOT_TOKEN in ~/.ophelia/.env")
        bot = self._build_bot()
        await bot.start(token)

    async def stop(self) -> None:
        if self._bot and not self._bot.is_closed():
            await self._bot.close()

    async def send_proactive(self, text: str) -> None:
        if not self._bot:
            return
        allowed = self.settings.allowed_discord_users()
        if not allowed:
            log.warning("discord.notify_skipped", reason="no DISCORD_ALLOWED_USER_IDS")
            return
        for uid in allowed:
            try:
                user = await self._bot.fetch_user(uid)
                await user.send(text[:2000])
            except Exception as e:
                log.warning("discord.notify_failed", user=uid, error=str(e))
