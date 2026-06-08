from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog
from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ophelia.android.factory import build_android_body
from ophelia.channels.session import ChannelSession
from ophelia.config import Settings
from ophelia.core.signals import Signals
from ophelia.media.voice import synthesize_speech, transcribe_audio
from ophelia.providers.router import build_provider_stack

log = structlog.get_logger()


class TelegramGateway:
    platform = "telegram"

    def __init__(
        self,
        settings: Settings,
        session: ChannelSession,
        signals: Signals,
        *,
        games=None,
        vision=None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.signals = signals
        self.games = games
        self.vision = vision
        self._app: Application | None = None
        self._voice_dir = settings.data_dir / "voice"

    def is_configured(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    def _allowed(self, user_id: int) -> bool:
        allowed = self.settings.allowed_telegram_users()
        if allowed is None:
            return True
        return user_id in allowed

    async def _reply_text(self, update: Update, text: str) -> None:
        if update.message:
            await update.message.reply_text(text[:4000])

    def _remember_user(self, user_id: int) -> None:
        self.signals.last_telegram_user_id = user_id

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        self._remember_user(update.effective_user.id)
        await self._reply_text(update, self.session.WELCOME)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        await self.session.cmd_pause(lambda t: self._reply_text(update, t))

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        await self.session.cmd_resume(lambda t: self._reply_text(update, t))

    async def cmd_listen(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        arg = context.args[0] if context.args else "status"
        await self.session.cmd_listen(arg, lambda t: self._reply_text(update, t))

    async def cmd_inner(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        arg = context.args[0] if context.args else "status"
        await self.session.cmd_inner(arg, lambda t: self._reply_text(update, t))

    async def cmd_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        arg = context.args[0] if context.args else "status"
        rest = context.args[1:] if len(context.args) > 1 else []

        def _android():
            if self.settings.android_enabled:
                return build_android_body(self.settings)
            return None

        await self.session.cmd_game(
            arg,
            rest,
            lambda t: self._reply_text(update, t),
            primary_channel=self.settings.primary_user_channel(),
            android_factory=_android,
        )

    async def cmd_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        user = update.effective_user
        channel = f"telegram:{user.id}"
        arg = context.args[0] if context.args else "status"
        await self.session.cmd_voice(
            channel,
            arg,
            lambda t: self._reply_text(update, t),
            default=self.settings.voice_reply_default,
        )

    async def _bearer(self) -> str | None:
        xai = build_provider_stack(self.settings).xai_backend()
        if not xai:
            return None
        try:
            return await xai.bearer_fresh()
        except Exception:
            return xai.bearer()

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if not user or not self._allowed(user.id):
            await update.message.reply_text("Unauthorized.")
            return
        self._remember_user(user.id)
        channel = f"telegram:{user.id}"
        await update.message.chat.send_action("typing")
        await self.session.handle_chat(
            channel,
            update.message.text.strip(),
            lambda t: self._send_reply(update, context, channel, t),
        )

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.voice:
            return
        user = update.effective_user
        if not user or not self._allowed(user.id):
            return

        self._remember_user(user.id)
        channel = f"telegram:{user.id}"
        self.signals.last_user_message_at = time.time()
        await self.signals.set_user_talking(True)
        await update.message.chat.send_action("typing")

        try:
            bearer = await self._bearer()
            if not bearer:
                await update.message.reply_text("No xAI auth for voice.")
                return

            self._voice_dir.mkdir(parents=True, exist_ok=True)
            file = await update.message.voice.get_file()
            ogg_path = self._voice_dir / f"in_{update.message.message_id}.ogg"
            await file.download_to_drive(str(ogg_path))

            text = await transcribe_audio(
                ogg_path,
                bearer=bearer,
                base_url=self.settings.xai_base_url,
            )
            if not text:
                await update.message.reply_text("Couldn't hear that — try again?")
                return

            await update.message.reply_text(f"Heard: {text[:500]}")
            await self.session.handle_chat(
                channel,
                text,
                lambda t: self._send_reply(update, context, channel, t),
            )
        except Exception as e:
            log.exception("telegram.voice_error")
            await update.message.reply_text(f"Voice error: {e}")
        finally:
            await self.signals.set_user_talking(False)

    async def _send_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        channel: str,
        reply: str,
    ) -> None:
        voice_on = self.session.voice_enabled(
            channel, self.settings.voice_reply_default
        )
        if voice_on and len(reply) < 800 and update.message:
            try:
                bearer = await self._bearer()
                if bearer:
                    mp3 = self._voice_dir / f"out_{update.message.message_id}.mp3"
                    await synthesize_speech(
                        reply[:1000],
                        mp3,
                        bearer=bearer,
                        base_url=self.settings.xai_base_url,
                        voice_id=self.settings.tts_voice_id,
                    )
                    with mp3.open("rb") as audio:
                        await update.message.reply_voice(voice=InputFile(audio))
                    return
            except Exception as e:
                log.warning("telegram.tts_fallback", error=str(e))
        await self._reply_text(update, reply)

    def build_app(self) -> Application:
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN in ~/.ophelia/.env")

        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("voice", self.cmd_voice))
        app.add_handler(CommandHandler("listen", self.cmd_listen))
        app.add_handler(CommandHandler("inner", self.cmd_inner))
        app.add_handler(CommandHandler("game", self.cmd_game))
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self._app = app
        return app

    async def prepare(self) -> None:
        """Initialize bot API before consciousness can send proactive messages."""
        if self._app is not None:
            return
        app = self.build_app()
        await app.initialize()
        await app.start()
        log.info("telegram.ready")

    async def run(self) -> None:
        await self.prepare()
        app = self._app
        if app is None:
            raise RuntimeError("Telegram app failed to initialize")
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while not self.signals.terminate:
                await asyncio.sleep(1)
        finally:
            if app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()

    async def stop(self) -> None:
        pass

    def _proactive_recipients(self) -> list[int]:
        allowed = self.settings.allowed_telegram_users()
        if allowed:
            return sorted(allowed)
        if self.signals.last_telegram_user_id is not None:
            return [self.signals.last_telegram_user_id]
        return []

    async def send_proactive(self, text: str) -> None:
        if not self._app:
            log.warning(
                "telegram.notify_skipped",
                reason="bot not ready — Telegram still starting",
            )
            return
        recipients = self._proactive_recipients()
        if not recipients:
            log.warning(
                "telegram.notify_skipped",
                reason="no TELEGRAM_ALLOWED_USER_IDS and no user has /start yet",
                hint="message @userinfobot for your ID, add to ~/.ophelia/.env, send /start to your bot",
            )
            return
        for uid in recipients:
            try:
                await self._app.bot.send_message(chat_id=uid, text=text[:4000])
                log.info("telegram.notify_sent", user=uid, chars=len(text))
            except Exception as e:
                err = str(e)
                hint = ""
                if "can't initiate conversation" in err.lower() or "forbidden" in err.lower():
                    hint = "send /start to your bot in Telegram first"
                log.warning(
                    "telegram.notify_failed",
                    user=uid,
                    error=err,
                    hint=hint or None,
                )
