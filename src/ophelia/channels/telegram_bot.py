from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import structlog
from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ophelia.android.factory import build_android_body
from ophelia.channels.media_reply import artifact_paths_in_text, media_kind
from ophelia.channels.session import ChannelSession
from ophelia.channels.telegram_util import ensure_polling_mode
from ophelia.media.vision_input import describe_image_file
from ophelia.config import OPHELIA_HOME, Settings
from ophelia.core.signals import Signals
from ophelia.media.voice import synthesize_speech, transcribe_audio
from ophelia.providers.router import build_provider_stack

log = structlog.get_logger()

# fcntl is POSIX-only; on Windows there's no Telegram polling in practice
# (the phone runs Termux), so the lock becomes a no-op there.
try:
    import fcntl as _fcntl  # type: ignore
except Exception:  # pragma: no cover - Windows / unsupported
    _fcntl = None  # type: ignore


def acquire_telegram_poll_lock() -> bool:
    """Try to take an exclusive lock so only one Ophelia polls the bot token.

    Returns True if this process got the lock. The lock is held until the
    process exits (the OS releases the flock automatically). Prevents the
    "terminated by other getUpdates request" spam when a second instance
    starts (e.g. an accidental `ophelia run` spawned from phone_shell).
    """
    if _fcntl is None:
        return True  # no-op on platforms without fcntl
    try:
        OPHELIA_HOME.mkdir(parents=True, exist_ok=True)
        fd = os.open(OPHELIA_HOME / "telegram.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        # Keep fd open for the lifetime of the process; never close it here.
        return True
    except Exception as e:
        log.warning("telegram.lock_failed", error=str(e))
        return True  # fail open — don't block Telegram on a lock hiccup


class _ConflictSpamFilter(logging.Filter):
    """Collapse PTB's repeated polling-conflict tracebacks into one warning.

    python-telegram-bot's networkloop retries getUpdates on a 409 Conflict
    forever, logging the full traceback every cycle. The "Conflict: ...
    getUpdates" text is in the record's exc_info, not its message (which is
    just "Exception happened while polling for updates."), so we must inspect
    the attached exception. We emit one concise warning and drop the rest,
    and set a flag a background task reads to log the culprit process.
    """

    _emitted = False
    conflict_seen = False

    @staticmethod
    def _is_conflict(record: logging.LogRecord) -> bool:
        # The useful text is on the attached exception, not the message.
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None:
            cls = type(exc).__name__
            text = f"{cls} {exc}"
            if "Conflict" in cls or "Conflict" in text or "getUpdates" in text:
                return True
        try:
            msg = record.getMessage()
        except Exception:
            msg = ""
        return "Conflict" in msg and "getUpdates" in msg

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._is_conflict(record):
            return True
        _ConflictSpamFilter.conflict_seen = True
        if not _ConflictSpamFilter._emitted:
            _ConflictSpamFilter._emitted = True
            log.error(
                "telegram.polling_conflict",
                error="409 Conflict: terminated by other getUpdates request",
                reason="another process is polling this bot token "
                "(a second ophelia run, Hermes, or a stale tmux session)",
                fix=(
                    "run: pkill -f 'ophelia run'; tmux kill-server; "
                    "then start exactly one instance. "
                    "Only one bot instance may poll a token at a time."
                ),
            )
        return False


def _install_conflict_filter() -> None:
    """Attach the spam filter so PTB's 409-conflict tracebacks don't flood."""
    filt = _ConflictSpamFilter()
    # Filter at the emitting logger (records are checked against the emitter's
    # own filters before propagation).
    logging.getLogger("telegram.ext._utils.networkloop").addFilter(filt)
    # Also filter at root handlers — propagated records pass through handler
    # filters regardless of which logger emitted them, so this catches the
    # conflict even if PTB's logger name differs across versions.
    for h in logging.getLogger().handlers:
        h.addFilter(filt)


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
        self._media_dir = settings.data_dir / "telegram_media"
        # Last text message we sent, so we can attach a "Continue" inline button
        # to it when a turn runs out of tool rounds.
        self._last_text_msg = None

    def is_configured(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    def _allowed(self, user_id: int) -> bool:
        allowed = self.settings.allowed_telegram_users()
        if allowed is None:
            return True
        return user_id in allowed

    async def _reply_text(self, update: Update, text: str) -> None:
        if update.message:
            self._last_text_msg = await update.message.reply_text(text[:4000])

    def _remember_user(self, user_id: int) -> None:
        self.signals.last_telegram_user_id = user_id

    async def _reject_user(self, update: Update) -> None:
        user = update.effective_user
        if not user or not update.message:
            return
        allowed = self.settings.allowed_telegram_users()
        hint = f"Your Telegram id is {user.id}."
        if allowed:
            hint += f" Allowed: {', '.join(str(x) for x in sorted(allowed))}."
        else:
            hint += " Set TELEGRAM_ALLOWED_USER_IDS in ~/.ophelia/.env"
        await update.message.reply_text(f"Unauthorized. {hint}")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user:
            return
        if not self._allowed(update.effective_user.id):
            await self._reject_user(update)
            return
        self._remember_user(update.effective_user.id)
        log.info("telegram.command", command="start", user=update.effective_user.id)
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

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        channel = f"telegram:{update.effective_user.id}"
        await self.session.cmd_status(
            channel,
            lambda t: self._reply_text(update, t),
            default_voice=self.settings.voice_reply_default,
        )

    async def cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        await self.session.cmd_models(lambda t: self._reply_text(update, t))

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        await self.session.cmd_help(lambda t: self._reply_text(update, t))

    async def cmd_continue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resume an unfinished tool chain — same as tapping the Continue button."""
        if not update.effective_user or not self._allowed(update.effective_user.id):
            return
        user = update.effective_user
        channel = f"telegram:{user.id}"
        self._remember_user(user.id)
        pending = getattr(self.session.agent, "_pending_resume", {})
        if channel not in pending:
            await self._reply_text(update, "Nothing to continue — I'm not mid-task.")
            return
        await update.message.chat.send_action("typing")
        await self.session.handle_chat(
            channel,
            "continue",
            lambda t: self._send_reply(update, context, channel, t),
            media_reply=lambda p, c: self._send_media_to_chat(update, p, c),
        )
        await self._maybe_attach_continue(update, context, channel)

    async def _bearer(self) -> str | None:
        xai = build_provider_stack(self.settings).xai_backend()
        if not xai:
            return None
        try:
            return await xai.bearer_fresh()
        except Exception:
            return xai.bearer()

    async def _describe_saved_image(self, path: Path, caption: str) -> str:
        question = caption.strip() or (
            "The user sent this image on Telegram. Describe it and respond to what they likely want."
        )
        return await describe_image_file(self.settings, path, question=question)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.photo:
            return
        user = update.effective_user
        if not user or not self._allowed(user.id):
            await self._reject_user(update)
            return
        self._remember_user(user.id)
        channel = f"telegram:{user.id}"
        caption = (update.message.caption or "").strip()
        log.info("telegram.photo", user=user.id, caption=caption[:80] if caption else "")
        await update.message.chat.send_action("typing")
        try:
            self._media_dir.mkdir(parents=True, exist_ok=True)
            photo = update.message.photo[-1]
            file = await photo.get_file()
            path = self._media_dir / f"in_{update.message.message_id}.jpg"
            await file.download_to_drive(str(path))
            description = await self._describe_saved_image(path, caption)
            prompt = (
                f"[User sent a photo — saved {path.name}]\n"
                f"Caption: {caption or '(none)'}\n\n"
                f"Vision analysis:\n{description}"
            )
            await self.session.handle_chat(
                channel,
                prompt,
                lambda t: self._send_reply(update, context, channel, t),
                media_reply=lambda p, c: self._send_media_to_chat(update, p, c),
            )
        except Exception as e:
            log.exception("telegram.photo_error")
            await update.message.reply_text(f"Photo error: {e}")
        await self._maybe_attach_continue(update, context, channel)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.document:
            return
        doc = update.message.document
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        if not mime.startswith("image/") and not name.endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ):
            await update.message.reply_text(
                "I can read photos/images sent as pictures or image files — not other document types yet."
            )
            return
        user = update.effective_user
        if not user or not self._allowed(user.id):
            await self._reject_user(update)
            return
        self._remember_user(user.id)
        channel = f"telegram:{user.id}"
        caption = (update.message.caption or "").strip()
        log.info("telegram.document_image", user=user.id, mime=mime)
        await update.message.chat.send_action("typing")
        try:
            self._media_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(name).suffix or ".jpg"
            path = self._media_dir / f"in_{update.message.message_id}{ext}"
            file = await doc.get_file()
            await file.download_to_drive(str(path))
            description = await self._describe_saved_image(path, caption)
            prompt = (
                f"[User sent an image file — saved {path.name}]\n"
                f"Caption: {caption or '(none)'}\n\n"
                f"Vision analysis:\n{description}"
            )
            await self.session.handle_chat(
                channel,
                prompt,
                lambda t: self._send_reply(update, context, channel, t),
                media_reply=lambda p, c: self._send_media_to_chat(update, p, c),
            )
        except Exception as e:
            log.exception("telegram.document_error")
            await update.message.reply_text(f"Image error: {e}")
        await self._maybe_attach_continue(update, context, channel)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        user = update.effective_user
        if not user or not self._allowed(user.id):
            await self._reject_user(update)
            return
        self._remember_user(user.id)
        channel = f"telegram:{user.id}"
        log.info("telegram.message", user=user.id, preview=update.message.text.strip()[:80])
        await update.message.chat.send_action("typing")
        await self.session.handle_chat(
            channel,
            update.message.text.strip(),
            lambda t: self._send_reply(update, context, channel, t),
            media_reply=lambda p, c: self._send_media_to_chat(update, p, c),
        )
        await self._maybe_attach_continue(update, context, channel)

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
                media_reply=lambda p, c: self._send_media_to_chat(update, p, c),
            )
        except Exception as e:
            log.exception("telegram.voice_error")
            await update.message.reply_text(f"Voice error: {e}")
        finally:
            await self.signals.set_user_talking(False)
        await self._maybe_attach_continue(update, context, channel)

    async def _send_media_artifacts(
        self,
        update: Update,
        reply: str,
        *,
        extra_paths: list[Path] | None = None,
    ) -> None:
        if not update.message:
            return
        seen: set[Path] = set()
        paths = artifact_paths_in_text(reply)
        if extra_paths:
            paths.extend(extra_paths)
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            kind = media_kind(path)
            try:
                if kind == "photo":
                    with path.open("rb") as f:
                        await update.message.reply_photo(photo=InputFile(f))
                    log.info("telegram.sent_photo", path=str(path))
                elif kind == "video":
                    with path.open("rb") as f:
                        await update.message.reply_video(video=InputFile(f))
                    log.info("telegram.sent_video", path=str(path))
                elif kind == "audio":
                    with path.open("rb") as f:
                        await update.message.reply_audio(audio=InputFile(f))
                    log.info("telegram.sent_audio", path=str(path))
            except Exception as e:
                log.warning("telegram.send_media_failed", path=str(path), error=str(e))

    async def _send_media_to_chat(self, update: Update, path: Path, caption: str) -> bool:
        """Send a single file to the current chat as the appropriate media type.

        Used by the send_file tool to deliver audio/video/images/documents
        mid-turn. Falls back to sending as a document for unknown types.
        Returns True on success.
        """
        if not update.message:
            return False
        kind = media_kind(path)
        cap = caption[:1024] if caption else None
        try:
            with path.open("rb") as f:
                if kind == "photo":
                    await update.message.reply_photo(photo=InputFile(f), caption=cap)
                elif kind == "video":
                    await update.message.reply_video(video=InputFile(f), caption=cap)
                elif kind == "audio":
                    await update.message.reply_audio(audio=InputFile(f), caption=cap)
                else:
                    await update.message.reply_document(document=InputFile(f), caption=cap)
            log.info("telegram.send_file", path=str(path), kind=kind or "document")
            return True
        except Exception as e:
            log.warning("telegram.send_file_failed", path=str(path), error=str(e))
            return False

    async def _send_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        channel: str,
        reply: str,
    ) -> None:
        extra_paths: list[Path] = []
        tools = getattr(self.session.agent, "tools", None)
        if tools and hasattr(tools, "consume_pending_artifacts"):
            try:
                extra_paths = tools.consume_pending_artifacts()
            except Exception:
                extra_paths = []
        await self._send_media_artifacts(update, reply, extra_paths=extra_paths)
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
                    # Voice went out, not text — no message to edit a button onto.
                    self._last_text_msg = None
                    return
            except Exception as e:
                log.warning("telegram.tts_fallback", error=str(e))
        await self._reply_text(update, reply)

    async def _on_error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        log.exception("telegram.handler_error", error=str(context.error))

    def _continue_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶ Continue", callback_data="ophelia:continue")]]
        )

    async def _maybe_attach_continue(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        channel: str,
    ) -> None:
        """If the last turn queued a resume, show a Continue button on its reply."""
        pending = getattr(self.session.agent, "_pending_resume", {})
        if channel not in pending:
            return
        kb = self._continue_keyboard()
        last = self._last_text_msg
        if last is not None and last.chat is not None:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=last.chat_id,
                    message_id=last.message_id,
                    reply_markup=kb,
                )
                return
            except Exception as e:
                log.debug("telegram.continue_edit_failed", error=str(e))
        # Fallback: send a small standalone message with the button (e.g. when
        # the last reply went out as voice/media instead of text).
        try:
            chat = update.effective_chat
            if chat is not None:
                self._last_text_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text="Ran out of steps on that one — tap to keep going.",
                    reply_markup=kb,
                )
        except Exception as e:
            log.warning("telegram.continue_button_failed", error=str(e))

    async def on_continue_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the inline ▶ Continue button — resume the unfinished chain."""
        q = update.callback_query
        if q is None:
            return
        await q.answer()
        user = q.from_user
        if user is None or not self._allowed(user.id):
            return
        channel = f"telegram:{user.id}"
        self._remember_user(user.id)
        pending = getattr(self.session.agent, "_pending_resume", {})
        if channel not in pending:
            # Already resumed or finished — clear the stale button.
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        chat = q.message.chat if q.message and q.message.chat else None
        if chat is None:
            return
        await chat.send_action("typing")
        # Reuse the gateway's reply plumbing by routing through handle_chat with
        # callbacks that target the callback-query's chat.
        async def _reply(text: str) -> None:
            self._last_text_msg = await context.bot.send_message(
                chat_id=chat.id, text=text[:4000]
            )

        async def _media(path, caption) -> bool:
            try:
                kind = media_kind(path)
                cap = caption[:1024] if caption else None
                with path.open("rb") as f:
                    if kind == "photo":
                        await context.bot.send_photo(chat_id=chat.id, photo=InputFile(f), caption=cap)
                    elif kind == "video":
                        await context.bot.send_video(chat_id=chat.id, video=InputFile(f), caption=cap)
                    elif kind == "audio":
                        await context.bot.send_audio(chat_id=chat.id, audio=InputFile(f), caption=cap)
                    else:
                        await context.bot.send_document(chat_id=chat.id, document=InputFile(f), caption=cap)
                return True
            except Exception as e:
                log.warning("telegram.continue_media_failed", error=str(e))
                return False

        await self.session.handle_chat(
            channel,
            "continue",
            _reply,
            media_reply=_media,
        )
        await self._maybe_attach_continue(update, context, channel)

    async def _conflict_diagnostic(self) -> None:
        """Once a polling conflict is seen, log the likely culprit process."""
        while not self.signals.terminate:
            await asyncio.sleep(5)
            if _ConflictSpamFilter.conflict_seen:
                await self._log_polling_processes()
                return

    async def _log_polling_processes(self) -> None:
        """List ophelia/hermes/tmux processes so the user can kill the dup."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-ef",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            text = out.decode(errors="replace")
        except Exception as e:
            log.warning("telegram.conflict_diag_failed", error=str(e))
            return
        candidates = []
        for line in text.splitlines():
            low = line.lower()
            if "ophelia" in low or "hermes" in low or "tmux" in low:
                candidates.append(line.strip())
        log.error(
            "telegram.polling_conflict_processes",
            candidates="\n".join(candidates[:40]) or "(none matched by name)",
            fix=(
                "kill every line above that's polling this token, then keep one. "
                "Common: pkill -f 'ophelia run'; tmux kill-server; pkill -f hermes"
            ),
        )

    def build_app(self) -> Application:
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN in ~/.ophelia/.env")

        app = Application.builder().token(token).build()
        app.add_error_handler(self._on_error)
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("models", self.cmd_models))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("continue", self.cmd_continue))
        app.add_handler(CommandHandler("voice", self.cmd_voice))
        app.add_handler(CommandHandler("listen", self.cmd_listen))
        app.add_handler(CommandHandler("inner", self.cmd_inner))
        app.add_handler(CommandHandler("game", self.cmd_game))
        app.add_handler(CallbackQueryHandler(self.on_continue_callback, pattern="^ophelia:continue$"))
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.Document.IMAGE, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self._app = app
        return app

    async def prepare(self) -> None:
        """Initialize bot API before consciousness can send proactive messages."""
        if self._app is not None:
            return
        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN in ~/.ophelia/.env")
        try:
            cleared = await ensure_polling_mode(token)
            if cleared:
                log.info(
                    "telegram.polling_restored",
                    note="removed Hermes/webhook URL so Ophelia can poll",
                )
        except Exception as e:
            log.warning("telegram.webhook_check_failed", error=str(e))
        app = self.build_app()
        await app.initialize()
        await app.start()
        # Overwrite BotFather's command menu. Whatever Hermes (or a previous
        # bot on this token) registered still shows in the "/" picker until we
        # replace it, so set Ophelia's own commands explicitly.
        try:
            from telegram import BotCommand

            await app.bot.set_my_commands(
                [
                    BotCommand("status", "what's on / running / pending"),
                    BotCommand("pause", "pause autonomous outreach"),
                    BotCommand("resume", "resume autonomous outreach"),
                    BotCommand("continue", "resume an unfinished task"),
                    BotCommand("voice", "voice replies on/off"),
                    BotCommand("listen", "local mic listening on/off"),
                    BotCommand("inner", "inner-monologue mirror on/off/tail"),
                    BotCommand("game", "game list / play / stop / look"),
                    BotCommand("models", "per-role provider/model routing"),
                    BotCommand("help", "list commands"),
                ]
            )
            log.info("telegram.commands_registered")
        except Exception as e:
            log.warning("telegram.set_commands_failed", error=str(e))
        log.info("telegram.ready")

    async def run(self) -> None:
        await self.prepare()
        app = self._app
        if app is None:
            raise RuntimeError("Telegram app failed to initialize")
        # Collapse PTB's repeated 409-conflict tracebacks into one clear warning.
        _install_conflict_filter()
        # If a conflict does appear, log the culprit process once (the second
        # poller is usually an external leftover our lock can't see).
        asyncio.create_task(self._conflict_diagnostic())
        # Refuse to start a second poller on the same bot token — that's the
        # cause of the "terminated by other getUpdates request" spam. Let the
        # already-running instance keep Telegram; this one skips polling.
        if not acquire_telegram_poll_lock():
            log.error(
                "telegram.poll_lock_held",
                reason="another Ophelia instance is already polling this bot token",
                fix="pkill -f 'ophelia run'  then start a single instance",
            )
            return
        try:
            await app.updater.start_polling(drop_pending_updates=True)
        except Exception as e:
            err = str(e)
            if "409" in err or "webhook" in err.lower() or "Conflict" in err:
                log.error(
                    "telegram.polling_conflict",
                    error=err,
                    reason="another process is polling this bot token "
                    "(second ophelia run, Hermes, or a stale tmux session)",
                    fix="pkill -f 'ophelia run'; ensure no other bot uses this token; "
                    "only one poller per token",
                )
            raise
        log.info("telegram.polling")
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

    async def send_proactive_media(self, path) -> None:
        """Send a generated media file (image/video/audio) to all recipients."""
        if not self._app:
            return
        from pathlib import Path as _P

        p = _P(path)
        if not p.is_file():
            return
        kind = media_kind(p)
        if not kind:
            return
        recipients = self._proactive_recipients()
        for uid in recipients:
            try:
                with p.open("rb") as f:
                    if kind == "photo":
                        await self._app.bot.send_photo(chat_id=uid, photo=InputFile(f))
                    elif kind == "video":
                        await self._app.bot.send_video(chat_id=uid, video=InputFile(f))
                    elif kind == "audio":
                        await self._app.bot.send_audio(chat_id=uid, audio=InputFile(f))
                log.info("telegram.notify_media_sent", user=uid, kind=kind, path=str(p))
            except Exception as e:
                log.warning("telegram.notify_media_failed", user=uid, error=str(e))
