from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog

from ophelia.channels.guest_approval import (
    GuestApprovals,
    append_user_to_allowlist,
)
from ophelia.channels.discord_log_channels import DiscordLogChannels
from ophelia.channels.media_reply import artifact_paths_in_text, media_kind
from ophelia.channels.session import ChannelSession
from ophelia.config import Settings
from ophelia.core.signals import Signals
from ophelia.providers.router import build_provider_stack

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
        self._guest_approvals = GuestApprovals()
        self._log_channels = DiscordLogChannels(settings)
        self._media_dir = settings.data_dir / "discord_media"

    def register_log_hooks(self, session: ChannelSession) -> None:
        if not self._log_channels.enabled():
            return

        async def _mirror(entry: dict) -> None:
            if self._bot:
                await self._log_channels.mirror_chat_entry(self._bot, entry)

        session.add_log_hook(_mirror)

    def _log_context(self, message) -> dict:
        author = message.author
        return {
            "platform": "discord",
            "display_name": str(author),
            "is_dm": message.guild is None,
            "guild_id": message.guild.id if message.guild else None,
            "guild_name": message.guild.name if message.guild else None,
        }

    async def mirror_consciousness(self, text: str) -> None:
        if self._bot and self._log_channels.enabled():
            await self._log_channels.log_consciousness(self._bot, text)

    async def mirror_inner_thought(self, text: str) -> None:
        if self._bot and self._log_channels.enabled():
            await self._log_channels.log_inner_thought(self._bot, text)

    def is_configured(self) -> bool:
        return bool(self.settings.discord_bot_token)

    def _allowed(self, user_id: int) -> bool:
        """Owner-only gate for control commands."""
        return self.settings.is_owner_channel(f"discord:{user_id}")

    def _owner_discord_ids(self) -> list[int]:
        ids: list[int] = []
        for c in self.settings.owner_channels():
            if c.startswith("discord:"):
                raw = c.split(":", 1)[1]
                if raw.isdigit():
                    ids.append(int(raw))
        allowed = self.settings.allowed_discord_users() or set()
        return ids or sorted(allowed)

    async def _send_owner_dm(self, text: str) -> None:
        if not self._bot:
            return
        for uid in self._owner_discord_ids():
            try:
                user = await self._bot.fetch_user(uid)
                await user.send(text[:2000])
            except Exception as e:
                log.warning("discord.owner_dm_failed", user=uid, error=str(e))

    async def _admit_discord(self, message) -> str:
        """Decide what to do with an inbound Discord chat message.
        Returns 'ok' | 'held' | 'rejected' (see TelegramGateway._admit_chat)."""
        author = message.author
        if author.bot:
            return "rejected"
        uid = author.id
        if self.settings.is_owner_channel(f"discord:{uid}"):
            return "ok"
        allowed = self.settings.allowed_discord_users()
        if allowed is not None and uid in allowed:
            return "ok"
        mode = (self.settings.guest_admission or "approve").lower()
        if mode == "open":
            return "ok"
        if mode == "reject":
            await message.channel.send("Unauthorized.")
            return "rejected"
        # approve mode
        if self._guest_approvals.is_denied("discord", uid):
            await message.channel.send("Sorry — the owner hasn't approved this chat.")
            return "rejected"
        if self._guest_approvals.is_pending("discord", uid):
            await message.channel.send(
                "I've asked my owner to OK our chat — still waiting. Hang tight 💙"
            )
            return "held"
        name = str(author)
        preview = message.content.strip()[:300] or "(media)"
        added = self._guest_approvals.add_pending("discord", uid, name, preview)
        if added:
            await self._send_owner_dm(
                f"🌙 Someone new wants to talk to me on Discord:\n\n"
                f"Name: {name}\nDiscord ID: {uid}\nFirst message: {preview}\n\n"
                f"Reply with `!approve {uid}` to add them as a sandboxed guest, "
                f"or `!deny {uid}` to decline."
            )
        await message.channel.send(
            "Hi! I'm Ophelia 🌙 I've asked my owner to OK our chat — once they "
            "say yes I'll be able to talk with you. Give me a moment."
        )
        return "held"

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
            await gw._log_channels.setup(bot)

        @bot.event
        async def on_guild_join(guild) -> None:
            await gw._log_channels.on_guild_join(bot, guild)

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

        @bot.command(name="approve")
        async def cmd_approve(ctx, user_id: str = "") -> None:
            if not gw._allowed(ctx.author.id):
                await ctx.send("Owner only.")
                return
            uid_s = user_id.strip()
            if not uid_s.isdigit():
                await ctx.send("Usage: !approve <discord user id>")
                return
            uid = int(uid_s)
            append_user_to_allowlist(gw.settings, "discord", uid)
            rec = gw._guest_approvals.set_status("discord", uid, "approved") or {}
            name = rec.get("display_name") or str(uid)
            await ctx.send(f"✅ Approved {name} (discord:{uid}) — added as a guest.")
            # Notify the guest and replay their first held message.
            try:
                user = await bot.fetch_user(uid)
                await user.send(
                    "Good news — my owner said yes 💙 I'm around now. Say hi any time!"
                )
                first_msg = rec.get("first_message") or ""
                if first_msg:

                    async def _reply(t: str) -> None:
                        await user.send(t[:2000])

                    async def _media(path: Path, caption: str) -> bool:
                        return await gw._send_discord_file_to_user(user, path, caption)

                    asyncio.create_task(
                        gw.session.handle_chat(
                            f"discord:{uid}",
                            first_msg,
                            _reply,
                            media_reply=_media,
                            log_context={
                                "platform": "discord",
                                "display_name": name,
                                "is_dm": True,
                            },
                        )
                    )
            except Exception as e:
                log.warning("discord.approve_notify_failed", error=str(e))

        @bot.command(name="deny")
        async def cmd_deny(ctx, user_id: str = "") -> None:
            if not gw._allowed(ctx.author.id):
                await ctx.send("Owner only.")
                return
            uid_s = user_id.strip()
            if not uid_s.isdigit():
                await ctx.send("Usage: !deny <discord user id>")
                return
            uid = int(uid_s)
            rec = gw._guest_approvals.set_status("discord", uid, "denied") or {}
            name = rec.get("display_name") or str(uid)
            await ctx.send(f"❌ Declined {name} (discord:{uid}).")

        @bot.event
        async def on_message(message) -> None:
            if message.author.bot:
                return
            if message.content.startswith("!"):
                await bot.process_commands(message)
                return
            admission = await gw._admit_discord(message)
            if admission != "ok":
                return
            channel = f"discord:{message.author.id}"
            ctx = gw._log_context(message)

            async def _reply(text: str) -> None:
                # Send any media artifacts referenced in the reply text first.
                await gw._send_discord_media(message, text)
                # Then the text itself (split to Discord's 2000-char limit).
                if not text:
                    return
                for i in range(0, len(text), 2000):
                    await message.channel.send(text[i : i + 2000])

            async def _media_reply(path: Path, caption: str) -> bool:
                return await gw._send_discord_file(message, path, caption)

            # Capture image attachments: download, run vision, and fold the
            # saved absolute paths into the prompt so the agent can use them
            # (e.g. pass to generate_video for image-to-video).
            image_prompt = await gw._save_and_describe_image_attachments(message)
            prompt_text = message.content.strip()
            if image_prompt:
                prompt_text = f"{image_prompt}\n\n{prompt_text}" if prompt_text else image_prompt

            async with message.channel.typing():
                await gw.session.handle_chat(
                    channel,
                    prompt_text,
                    _reply,
                    media_reply=_media_reply,
                    log_context=ctx,
                )

        self._bot = bot
        return bot

    async def _save_and_describe_image_attachments(self, message) -> str | None:
        """Download any image attachments on a Discord message, run vision on
        each, and return a prompt fragment listing the saved absolute paths
        (so the agent can pass them to media tools like generate_video).

        Non-image attachments are ignored. Returns None if there are no image
        attachments.
        """
        image_attachments = [
            a for a in message.attachments
            if (a.content_type or "").startswith("image/")
            or Path(a.filename).suffix.lower()
            in (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ]
        if not image_attachments:
            return None

        from ophelia.media.vision_input import describe_image_file

        self._media_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []
        for att in image_attachments:
            ext = Path(att.filename).suffix.lower() or ".png"
            path = self._media_dir / f"in_{message.id}_{att.id}{ext}"
            try:
                await att.save(str(path))
            except Exception as e:
                log.warning("discord.attachment_save_failed", att=att.id, error=str(e))
                continue
            caption = (message.content or "").strip()
            description = await describe_image_file(self.settings, path, question=caption or (
                "The user sent this image on Discord. Describe it and respond to what they likely want."
            ))
            parts.append(
                f"[User sent an image — saved to {path}]\n"
                f"Caption: {caption or '(none)'}\n\n"
                f"Vision analysis:\n{description}"
            )
        return "\n\n".join(parts) if parts else None

    async def _send_discord_media(self, message, text: str) -> None:
        """Send media artifacts detected in a reply blob (Image/Video/TTS saved to ...)."""
        tools = getattr(self.session.agent, "tools", None)
        for path in artifact_paths_in_text(text):
            if tools is not None and tools.is_artifact_delivered(path):
                continue
            await self._send_discord_file(message, path, "")

    async def _send_discord_file(self, message, path: Path, caption: str) -> bool:
        """Send a file to the originating Discord channel as an attachment."""
        import discord

        tools = getattr(self.session.agent, "tools", None)
        try:
            p = Path(path).expanduser()
            if not p.is_file():
                return False
            if tools is not None and tools.is_artifact_delivered(p):
                return True
            await message.channel.send(
                content=caption[:2000] if caption else None,
                file=discord.File(str(p)),
            )
            if tools is not None:
                tools._mark_artifact_delivered(p)
            log.info("discord.send_file", path=str(p))
            return True
        except Exception as e:
            log.warning("discord.send_file_failed", path=str(path), error=str(e))
            return False

    async def _send_discord_file_to_user(self, user, path: Path, caption: str) -> bool:
        """Send a file to a specific Discord user's DM (used when replaying a
        newly-approved guest's first message)."""
        import discord

        try:
            p = Path(path).expanduser()
            if not p.is_file():
                return False
            await user.send(
                content=caption[:2000] if caption else None,
                file=discord.File(str(p)),
            )
            return True
        except Exception as e:
            log.warning("discord.send_file_to_user_failed", path=str(path), error=str(e))
            return False

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

    async def send_proactive_media(self, path, *, caption: str = "") -> None:
        """Send a generated media file (image/video/audio/doc) to all recipients.

        Tier C #11: Discord parity — captioned media like Telegram. Discord
        attaches the file with the caption as the message content.
        """
        if not self._bot:
            return
        import discord

        p = Path(path).expanduser()
        if not p.is_file():
            return
        allowed = self.settings.allowed_discord_users()
        if not allowed:
            return
        cap = (caption or "")[:2000] or None
        for uid in allowed:
            try:
                user = await self._bot.fetch_user(uid)
                await user.send(content=cap, file=discord.File(str(p)))
                log.info("discord.notify_media_sent", user=uid, path=str(p))
            except Exception as e:
                log.warning("discord.notify_media_failed", user=uid, error=str(e))

    async def send_proactive_voice(self, text: str) -> None:
        """Tier C #11: Discord parity — spontaneous voice notes.

        Synthesizes the text via the configured TTS backend (Kokoro / ElevenLabs
        / OpenAI / xAI) and DMs the audio file to each recipient. Mirrors
        TelegramGateway.send_proactive_voice so spontaneous consciousness
        messages reach Discord users as voice, not just text.
        """
        if not self._bot or not text.strip():
            return
        from ophelia.media.voice import resolve_tts_provider, synthesize
        from ophelia.mind.mood_behavior import mood_knobs

        allowed = self.settings.allowed_discord_users()
        if not allowed:
            return

        # Voice mind rewrite (Tier A #4) + mood-derived speed, same as Telegram.
        spoken = text
        voice_mind = getattr(self.session.agent, "voice_mind", None)
        if voice_mind is not None and voice_mind.enabled:
            try:
                spoken = await voice_mind.rewrite_for_speech(
                    text[:800],
                    psyche=self.session.agent.psyche,
                    agent=self.session.agent,
                )
            except Exception as e:
                log.debug("discord.voice_mind_failed", error=str(e))

        bearer = None
        if resolve_tts_provider(self.settings) == "xai":
            xai = build_provider_stack(self.settings).xai_backend()
            if xai:
                try:
                    bearer = await xai.bearer_fresh()
                except Exception as e:
                    log.warning("discord.voice_auth_failed", error=str(e))
                    await self.send_proactive(text)
                    return
        out = self.settings.data_dir / "discord_media" / f"spontaneous_{int(time.time())}.mp3"
        speed = None
        if hasattr(self.session.agent, "life") and self.session.agent.life:
            psyche = getattr(self.session.agent, "psyche", None)
            speed = self.session.agent.life.voice_speed(psyche=psyche)
        try:
            audio_path = await synthesize(
                spoken[:1000],
                out,
                settings=self.settings,
                xai_bearer=bearer,
                speed=speed,
            )
        except Exception as e:
            log.warning("discord.proactive_voice_tts_failed", error=str(e))
            await self.send_proactive(text)
            return

        import discord

        for uid in allowed:
            try:
                user = await self._bot.fetch_user(uid)
                await user.send(file=discord.File(str(audio_path)))
                log.info("discord.notify_voice_sent", user=uid, path=str(audio_path))
            except Exception as e:
                log.warning("discord.notify_voice_failed", user=uid, error=str(e))
