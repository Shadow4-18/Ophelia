from __future__ import annotations

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

from ophelia.android.games import GameStore
from ophelia.android.vision import ScreenVision
from ophelia.config import Settings
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.memory.store import MemoryStore
from ophelia.mind.drives import DriveState
from ophelia.media.voice import synthesize_speech, transcribe_audio
from ophelia.providers.router import XAIBackend, build_provider_stack

log = structlog.get_logger()


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        agent: AgentLoop,
        signals: Signals,
        memory: MemoryStore,
        drives: DriveState,
        *,
        games: GameStore | None = None,
        vision: ScreenVision | None = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.signals = signals
        self.memory = memory
        self.drives = drives
        self.games = games
        self.vision = vision
        self._app: Application | None = None
        self._voice_dir = settings.data_dir / "voice"

    def _allowed(self, user_id: int) -> bool:
        allowed = self.settings.allowed_telegram_users()
        if allowed is None:
            return True
        return user_id in allowed

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "Ophelia online.\n"
            "/pause /resume — consciousness outreach\n"
            "/voice on|off — TTS replies\n"
            "/listen on|off — local mic loop (Termux)\n"
            "/inner on|off — mirror thoughts to Telegram\n"
            "/game list|play|stop|look|status — mobile games (Shizuku)"
        )

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        self.signals.autonomy_paused = True
        await update.message.reply_text("Consciousness outreach paused.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        self.signals.autonomy_paused = False
        await update.message.reply_text("Consciousness outreach resumed.")

    async def cmd_listen(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        arg = (context.args[0] if context.args else "status").lower()
        if arg == "on":
            self.signals.listen_enabled = True
            await update.message.reply_text(
                "Local listen on — phone mic → Ophelia → speaker (Termux:API required)."
            )
        elif arg == "off":
            self.signals.listen_enabled = False
            await update.message.reply_text("Local listen off.")
        else:
            await update.message.reply_text(
                f"Local listen: {'on' if self.signals.listen_enabled else 'off'}"
            )

    async def cmd_inner(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        arg = (context.args[0] if context.args else "status").lower()
        if arg == "on":
            self.signals.inner_mirror = True
            await update.message.reply_text("Inner thought mirror to Telegram: on (💭 prefix).")
        elif arg == "off":
            self.signals.inner_mirror = False
            await update.message.reply_text("Inner mirror off (still logged to file).")
        elif arg == "tail":
            from ophelia.mind.inner_log import InnerMonologue

            tail = InnerMonologue().tail(30)
            await update.message.reply_text(tail[:4000] or "(empty)")
        else:
            await update.message.reply_text(
                f"Inner mirror: {'on' if self.signals.inner_mirror else 'off'} — /inner tail for log"
            )

    async def cmd_game(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        if not self.games:
            await update.message.reply_text("Games layer disabled (OPHELIA_GAMES=false).")
            return

        arg = (context.args[0] if context.args else "status").lower()
        rest = context.args[1:] if len(context.args) > 1 else []

        if arg == "list":
            await update.message.reply_text(self.games.format_list()[:4000])
            return

        if arg == "stop":
            await update.message.reply_text(self.games.stop_session())
            return

        if arg == "status":
            await update.message.reply_text(self.games.format_list()[:4000])
            return

        if arg == "look":
            if not self.vision:
                await update.message.reply_text("Vision unavailable — set up Shizuku.")
                return
            profile = self.games.active_profile()
            intent = ""
            if rest:
                named = self.games.get(rest[0])
                if named:
                    profile = named
                    intent = " ".join(rest[1:])
                else:
                    intent = " ".join(rest)
            if not profile:
                await update.message.reply_text(
                    "No active session. /game play <id> first.\n" + self.games.format_list()
                )
                return
            text = await self.vision.see_for_game(profile, intent)
            self.games.record_turn()
            await update.message.reply_text(text[:4000])
            return

        if arg == "play":
            if not rest:
                await update.message.reply_text("Usage: /game play <game_id> [minutes]")
                return
            game_id = rest[0]
            minutes = float(rest[1]) if len(rest) > 1 else None
            from ophelia.android.shizuku import AndroidBody

            android = None
            if self.settings.android_enabled:
                android = AndroidBody(
                    Path(str(self.settings.phone_control_path)).expanduser()
                )
            msg = await self.games.start_session(game_id, minutes=minutes, android=android)
            await update.message.reply_text(msg[:4000])
            channel = self.settings.primary_user_channel()
            if channel:
                await self.memory.append_message(
                    channel,
                    "assistant",
                    f"[game session started: {game_id}] {msg[:500]}",
                    metadata={"type": "game"},
                )
            return

        await update.message.reply_text(
            "Usage: /game list | play <id> [min] | stop | look | status"
        )

    async def cmd_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        arg = (context.args[0] if context.args else "status").lower()
        if arg == "on":
            context.application.bot_data["voice_reply"] = True
            await update.message.reply_text("Voice replies enabled.")
        elif arg == "off":
            context.application.bot_data["voice_reply"] = False
            await update.message.reply_text("Voice replies disabled.")
        else:
            on = context.application.bot_data.get("voice_reply", self.settings.voice_reply_default)
            await update.message.reply_text(f"Voice replies: {'on' if on else 'off'} (use /voice on|off)")

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

        channel = f"telegram:{user.id}"
        text = update.message.text.strip()
        await self._handle_user_input(update, channel, text, context)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.voice:
            return
        user = update.effective_user
        if not user or not self._allowed(user.id):
            return

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

            await update.message.reply_text(f"🎤 Heard: {text[:500]}")
            reply = await self.agent.run_turn(channel, text)
            await self._reply(update, context, reply)
        except Exception as e:
            log.exception("telegram.voice_error")
            await update.message.reply_text(f"Voice error: {e}")
        finally:
            await self.signals.set_user_talking(False)

    async def _handle_user_input(
        self,
        update: Update,
        channel: str,
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        self.signals.last_user_message_at = time.time()
        await self.signals.set_user_talking(True)
        await self.signals.set_agent_thinking(True)
        try:
            await update.message.chat.send_action("typing")
            reply = await self.agent.run_turn(channel, text)
            self.drives.on_user_message()
            await self.memory.save_drives(self.drives)
            self.signals.last_agent_message_at = time.time()
            await self._reply(update, context, reply)
        except Exception as e:
            log.exception("telegram.error")
            await update.message.reply_text(f"Error: {e}")
        finally:
            await self.signals.set_user_talking(False)
            await self.signals.set_agent_thinking(False)

    async def _reply(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, reply: str
    ) -> None:
        voice_on = context.application.bot_data.get(
            "voice_reply", self.settings.voice_reply_default
        )
        if voice_on and len(reply) < 800:
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
        await update.message.reply_text(reply[:4000])

    def build_app(self) -> Application:
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN in ~/.ophelia/.env")

        app = Application.builder().token(token).build()
        app.bot_data["voice_reply"] = self.settings.voice_reply_default
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

    async def send_proactive(self, text: str) -> None:
        if not self._app:
            return
        allowed = self.settings.allowed_telegram_users()
        if not allowed:
            log.warning("consciousness.notify_skipped", reason="no TELEGRAM_ALLOWED_USER_IDS")
            return
        for uid in allowed:
            try:
                await self._app.bot.send_message(chat_id=uid, text=text[:4000])
            except Exception as e:
                log.warning("consciousness.notify_failed", user=uid, error=str(e))
