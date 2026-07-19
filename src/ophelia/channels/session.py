"""Shared user message handling for all chat gateways."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

from ophelia.android.games import GameStore
from ophelia.channels.chat_log import ChatLogger
from ophelia.channels.media_reply import artifact_paths_in_text
from ophelia.channels.message_split import split_messages
from ophelia.android.vision import ScreenVision
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.memory.store import MemoryStore
from ophelia.media.tts_context import tts_turn_extra

log = structlog.get_logger()

ReplyFn = Callable[[str], Awaitable[None]]
MediaReplyFn = Callable[[Path, str], Awaitable[bool]]
LogHookFn = Callable[[dict], Awaitable[None]]
SendToGuestFn = Callable[[str, int, str], Awaitable[bool]]


def _sender_id(channel: str) -> str:
    """Extract the platform id from a 'platform:id' channel string."""
    return channel.split(":", 1)[1] if ":" in channel else channel


def guest_welcome_message() -> str:
    """The first-visit welcome shown to a newly-approved guest.

    Prepended to the guest's first real reply (not replacing it) so they get
    expectations set without losing the answer to the thing they actually
    asked. Mentioned capabilities match what guest mode actually allows:
    chat, web search, sharing images and short videos (1:1, lower res), and
    occasional voice notes. Excluded: owner's personal info, identity-shaping
    tools, phone control.
    """
    return (
        "Hey — you're talking to Ophelia. Quick heads-up on how this works:\n"
        "• I'll chat with you, search the web, and share what I know.\n"
        "• I can make images and short videos for you (square, standard quality).\n"
        "• Sometimes I'll send a voice note if it fits.\n"
        "• I won't share my owner's personal info, and you can't change who I am "
        "or poke at my phone.\n"
        "Otherwise — I'm around. What's up?"
    )


class ChannelSession:
    """Run agent turns and shared slash/bang commands."""

    WELCOME = (
        "Ophelia online.\n"
        "Commands: /status /pause /resume /update /voice /listen /inner /game /models /help\n"
        "(Telegram: /command — Discord: !command)"
    )

    def __init__(
        self,
        agent: AgentLoop,
        signals: Signals,
        memory: MemoryStore,
        drives: DriveState,
        *,
        games: GameStore | None = None,
        vision: ScreenVision | None = None,
    ) -> None:
        self.agent = agent
        self.signals = signals
        self.memory = memory
        self.drives = drives
        self.games = games
        self.vision = vision
        # Set by ChannelHub after construction so /tell, /suggest, and the
        # send_message_to_guest tool can route cross-platform DMs through the
        # correct gateway. None when running without a hub (e.g. CLI mode).
        self.hub: Any = None
        self._voice_reply: dict[str, bool] = {}
        self._chat_logger: ChatLogger | None = None
        self._log_hooks: list[LogHookFn] = []
        self._current_log_context: dict | None = None

    def add_log_hook(self, hook: LogHookFn) -> None:
        self._log_hooks.append(hook)

    async def _emit_log_hook(self, entry: dict) -> None:
        for hook in self._log_hooks:
            try:
                await hook(entry)
            except Exception as e:
                log.warning("channel.log_hook_failed", error=str(e))

    def _logger(self) -> ChatLogger | None:
        if not self.agent.settings.chat_log_enabled:
            return None
        if self._chat_logger is None:
            self._chat_logger = ChatLogger.from_settings(self.agent.settings)
        return self._chat_logger

    async def log_outgoing(
        self,
        *,
        channel: str,
        text: str = "",
        media_path: Path | str | None = None,
        media_kind: str | None = None,
        role: str = "assistant",
    ) -> None:
        """Log an outbound message that DIDN'T come from a handle_chat reply.

        Proactive messages (consciousness ticks), guest DMs sent via
        send_message_to_guest / /tell, spontaneous voice notes, and
        proactive media all go through the gateway's send_proactive /
        send_to_user paths — which bypass handle_chat entirely. Without
        this, those sends are invisible to the chat log (the logging
        server), so the owner can't see what Ophelia said on her own
        initiative or to guests. This closes that gap.
        """
        logger = self._logger()
        if not logger:
            return
        settings = self.agent.settings
        is_owner = settings.is_owner_channel(channel)
        sender_id = _sender_id(channel)
        entry = {
            "channel": channel,
            "direction": "out",
            "text": text or None,
            "media_path": media_path,
            "media_kind": media_kind,
            "sender_id": sender_id,
            "is_owner": is_owner,
            "role": role,
            "log_context": None,
        }
        await logger.log(
            **{k: v for k, v in entry.items() if k != "log_context"}
        )
        await self._emit_log_hook(entry)

    def voice_enabled(self, channel: str, default: bool = False) -> bool:
        return self._voice_reply.get(channel, default)

    def set_voice(self, channel: str, enabled: bool) -> None:
        self._voice_reply[channel] = enabled

    async def handle_chat(
        self,
        channel: str,
        text: str,
        reply: ReplyFn,
        *,
        media_reply: MediaReplyFn | None = None,
        log_context: dict | None = None,
    ) -> None:
        settings = self.agent.settings
        is_owner = settings.is_owner_channel(channel)
        sender_id = _sender_id(channel)
        logger = self._logger()
        self._current_log_context = log_context

        self.signals.last_user_message_at = time.time()
        await self.signals.set_user_talking(True)
        await self.signals.set_agent_thinking(True)
        self.agent.tools.begin_turn_artifacts()
        self.agent.tools.set_owner(is_owner)
        # Record who's speaking so guest-self-only tools (e.g. set_guest_name)
        # can verify the sender matches the target.
        self.agent.tools._current_sender_channel = channel

        # Tier B #6: log owner activity so the schedule learner can infer
        # quiet/active windows from observed patterns, not just .env schedule.
        if is_owner and getattr(self.agent, "life", None) is not None:
            learner = getattr(self.agent.life, "schedule_learner", None)
            if learner is not None:
                try:
                    await learner.record_owner_activity()
                except Exception as e:
                    log.debug("schedule_learner.record_failed", error=str(e))

        # Tier A #1: director decides urgency + pacing for this reply. The
        # owner is active by definition here, so the director won't defer —
        # it just shapes HOW she responds, not whether.
        director_decision = None
        director = getattr(self.agent, "director", None)
        if director is not None and director.available():
            try:
                director_decision = await director.decide(
                    trigger="user_message",
                    context_summary=text[:300],
                    owner_active=True,
                )
            except Exception as e:
                log.debug("director.chat_decide_error", error=str(e))

        # Log the inbound message (text + any referenced photo path) — universal,
        # for owner oversight. The "[User sent a photo — saved <path>]" prompt
        # text carries the inbound media filename; we capture it explicitly too.
        if logger:
            inbound_media = self._extract_inbound_media(text, settings)
            media_kind = None
            if inbound_media:
                from ophelia.channels.inbound_media import classify_attachment

                media_kind = classify_attachment(filename=inbound_media) or "file"
                if media_kind == "image":
                    media_kind = "photo"  # keep chat-log convention
            inbound_entry = {
                "channel": channel,
                "direction": "in",
                "text": text,
                "media_path": inbound_media,
                "media_kind": media_kind,
                "sender_id": sender_id,
                "is_owner": is_owner,
                "role": "user",
                "log_context": log_context,
            }
            await logger.log(**{k: v for k, v in inbound_entry.items() if k != "log_context"})
            await self._emit_log_hook(inbound_entry)

        # Wrap the reply/media senders so every outbound chunk + media file is
        # logged too — including mid-turn send_message / preamble delivery.
        # Previously set_message_sender(reply) used the raw gateway reply, so
        # mid-turn text reached Discord/Telegram but never the chat log / dm-*
        # mirror channels.
        async def _logged_reply(chunk: str) -> None:
            # Send first, then log — so the dm-* mirror reflects what actually
            # reached the user, not what we hoped to send.
            tools = self.agent.tools
            already_delivered: set[Path] = set()
            if tools is not None:
                already_delivered = set(tools._delivered_artifacts)
            await reply(chunk)
            if logger:
                out_entry = {
                    "channel": channel,
                    "direction": "out",
                    "text": chunk,
                    "sender_id": sender_id,
                    "is_owner": is_owner,
                    "role": "assistant",
                    "log_context": log_context,
                }
                await logger.log(
                    **{k: v for k, v in out_entry.items() if k != "log_context"}
                )
                await self._emit_log_hook(out_entry)
                # Media auto-delivered mid-turn is already mirrored via
                # _logged_media. Only log paths newly delivered by the
                # gateway's text-scan backup (_send_discord_media / Telegram
                # equivalent) during this reply call.
                for p in artifact_paths_in_text(chunk):
                    if tools is None or not tools.is_artifact_delivered(p):
                        continue
                    try:
                        resolved = p.expanduser().resolve()
                    except (OSError, ValueError):
                        continue
                    if resolved in already_delivered:
                        continue
                    media_entry = {
                        "channel": channel,
                        "direction": "out",
                        "text": f"[media sent: {p.name}]",
                        "media_path": p,
                        "media_kind": "generated",
                        "sender_id": sender_id,
                        "is_owner": is_owner,
                        "role": "media",
                        "log_context": log_context,
                    }
                    await logger.log(
                        **{k: v for k, v in media_entry.items() if k != "log_context"}
                    )
                    await self._emit_log_hook(media_entry)

        logged_media_reply: MediaReplyFn | None = None
        if media_reply is not None:
            async def _logged_media(path: Path, caption: str) -> bool:
                ok = await media_reply(path, caption)
                # Only mirror successful uploads — logging on failure made the
                # dm-* channel show images the user never received.
                if ok and logger:
                    media_entry = {
                        "channel": channel,
                        "direction": "out",
                        "text": caption or f"[media sent: {path.name}]",
                        "media_path": path,
                        "media_kind": "file",
                        "sender_id": sender_id,
                        "is_owner": is_owner,
                        "role": "media",
                        "log_context": log_context,
                    }
                    await logger.log(
                        **{k: v for k, v in media_entry.items() if k != "log_context"}
                    )
                    await self._emit_log_hook(media_entry)
                return ok
            logged_media_reply = _logged_media

        # Wire mid-turn senders AFTER the logged wrappers exist so send_message
        # and preamble delivery hit the chat log / Discord mirror too.
        self.agent.tools.set_message_sender(_logged_reply)
        if logged_media_reply is not None:
            self.agent.tools.set_media_sender(logged_media_reply)

        try:
            voice_on = self.voice_enabled(channel, settings.voice_reply_default)
            turn_extra = tts_turn_extra(settings, voice_reply=voice_on)
            # Tier A #1: director pace hint composes with the TTS turn extra.
            if director_decision is not None and director_decision.pace_hint:
                turn_extra = (
                    (turn_extra + "\n" if turn_extra else "")
                    + f"# Director pacing (urgency={director_decision.urgency})\n"
                    + director_decision.pace_hint
                )
            if is_owner and self.agent.humor:
                await self.agent.humor.score_inbound_reply(text)
            out = await self.agent.run_turn(
                channel, text, is_owner=is_owner, system_extra=turn_extra
            )
            from ophelia.channels.proactive_filter import (
                is_outreach_junk,
                strip_consciousness_tick_leak,
            )

            # Models sometimes echo consciousness-tick JSON into chat replies
            # after seeing raw ticks in history. Strip before the owner sees it.
            out = strip_consciousness_tick_leak(out or "")
            if not out or is_outreach_junk(out):
                out = ""
            # Only the owner's messages shape her drives/will. Guests don't.
            if is_owner:
                self.drives.on_user_message()
                await self.memory.save_drives(self.drives)
            self.signals.last_agent_message_at = time.time()
            for i, chunk in enumerate(split_messages(out)):
                cleaned = strip_consciousness_tick_leak(chunk)
                if not cleaned or is_outreach_junk(cleaned):
                    continue
                if i:
                    await asyncio.sleep(1.2)
                await _logged_reply(cleaned)
            # Tier B #8: track jokes/quips in her normal chat replies (owner
            # only) so her humor can be calibrated from everyday conversation,
            # not just spontaneous outreach.
            if is_owner and self.agent.humor:
                try:
                    await self.agent.humor.note_chat_reply(out)
                except Exception as e:
                    log.debug("humor.note_chat_reply_failed", error=str(e))
        except Exception as e:
            log.exception("channel.chat_error", channel=channel)
            await _logged_reply(f"Error: {e}")
        finally:
            if logged_media_reply is not None:
                for path in self.agent.tools.consume_pending_artifacts():
                    try:
                        ok = await logged_media_reply(path, "")
                        if ok:
                            self.agent.tools._mark_artifact_delivered(path)
                    except Exception as e:
                        log.warning("channel.flush_media_failed", path=str(path), error=str(e))
            self.agent.tools.clear_message_sender()
            self.agent.tools.clear_media_sender()
            self.agent.tools.clear_owner()
            self.agent.tools._current_sender_channel = None
            await self.signals.set_user_talking(False)
            await self.signals.set_agent_thinking(False)
            self._current_log_context = None
            # Event-driven will: after a chat turn, stir consciousness soon so
            # she can react/impulse without waiting for the full clock interval.
            asyncio.create_task(self._wake_after_chat())

    async def _wake_after_chat(self) -> None:
        """Delayed consciousness wake so we don't interrupt our own send."""
        try:
            await asyncio.sleep(2.0)
            if self.signals.autonomy_paused or self.signals.terminate:
                return
            if self.signals.user_talking or self.signals.agent_thinking:
                # Another turn started — they'll wake us when they finish.
                return
            self.signals.request_wake("chat_ended")
        except Exception as e:
            log.debug("channel.wake_after_chat_failed", error=str(e))

    @staticmethod
    def _extract_inbound_media(text: str, settings) -> str | None:
        """Pull the saved-photo path out of the gateway's photo prompt text.

        Gateways write either "saved to /abs/path/in_123.jpg" (preferred —
        the agent can pass it straight to media tools) or the legacy
        "saved in_123.jpg" form (filename only, resolved under telegram_media).
        """
        marker = "saved "
        idx = text.find(marker)
        if idx < 0:
            return None
        rest = text[idx + len(marker) :]
        # Skip an optional "to " between "saved" and the path.
        if rest.startswith("to "):
            rest = rest[3:]
        end = rest.find("]")
        token = rest[:end] if end >= 0 else rest.split()[0]
        token = token.strip().strip(".")
        if not token:
            return None
        # Absolute path: use it directly (gateways now surface the full path).
        p = Path(token)
        if p.is_absolute():
            return str(p) if p.is_file() else token
        # Legacy filename-only form: resolve under the gateway media dir.
        resolved = settings.data_dir / "telegram_media" / token
        return str(resolved) if resolved.is_file() else token

    async def cmd_pause(self, reply: ReplyFn) -> None:
        self.signals.autonomy_paused = True
        await reply("Consciousness outreach paused.")

    async def cmd_resume(self, reply: ReplyFn) -> None:
        self.signals.autonomy_paused = False
        await reply("Consciousness outreach resumed.")

    async def cmd_listen(self, arg: str, reply: ReplyFn) -> None:
        arg = (arg or "status").lower()
        if arg == "on":
            self.signals.listen_enabled = True
            await reply(
                "Local listen on — phone mic (Termux:API required)."
            )
        elif arg == "off":
            self.signals.listen_enabled = False
            await reply("Local listen off.")
        else:
            await reply(
                f"Local listen: {'on' if self.signals.listen_enabled else 'off'}"
            )

    async def cmd_inner(self, arg: str, reply: ReplyFn) -> None:
        from ophelia.mind.inner_log import InnerMonologue

        arg = (arg or "status").lower()
        if arg == "on":
            self.signals.inner_mirror = True
            await reply("Inner thought mirror: on.")
        elif arg == "off":
            self.signals.inner_mirror = False
            await reply("Inner mirror off (still logged to file).")
        elif arg == "tail":
            await reply(InnerMonologue().tail(30)[:4000] or "(empty)")
        else:
            await reply(
                f"Inner mirror: {'on' if self.signals.inner_mirror else 'off'}"
            )

    async def cmd_voice(self, channel: str, arg: str, reply: ReplyFn, *, default: bool) -> None:
        arg = (arg or "status").lower()
        if arg == "on":
            self.set_voice(channel, True)
            await reply("Voice replies enabled.")
        elif arg == "off":
            self.set_voice(channel, False)
            await reply("Voice replies disabled.")
        else:
            on = self.voice_enabled(channel, default)
            await reply(f"Voice replies: {'on' if on else 'off'}")

    async def cmd_game(
        self,
        arg: str,
        rest: list[str],
        reply: ReplyFn,
        *,
        primary_channel: str | None,
        android_factory,
    ) -> None:
        if not self.games:
            await reply("Games layer disabled (OPHELIA_GAMES=false).")
            return

        arg = (arg or "status").lower()

        if arg == "list" or arg == "status":
            await reply(self.games.format_list()[:4000])
            return
        if arg == "stop":
            await reply(self.games.stop_session())
            return
        if arg == "look":
            if not self.vision:
                await reply("Vision unavailable — set up Shizuku/ADB.")
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
                await reply(
                    "No active session. game play <id> first.\n"
                    + self.games.format_list()
                )
                return
            text = await self.vision.see_for_game(profile, intent)
            self.games.record_turn()
            await reply(text[:4000])
            return
        if arg == "play":
            if not rest:
                await reply("Usage: game play <game_id> [minutes]")
                return
            game_id = rest[0]
            minutes = float(rest[1]) if len(rest) > 1 else None
            android = android_factory() if android_factory else None
            msg = await self.games.start_session(
                game_id, minutes=minutes, android=android
            )
            await reply(msg[:4000])
            if primary_channel:
                await self.memory.append_message(
                    primary_channel,
                    "assistant",
                    f"[game session started: {game_id}] {msg[:500]}",
                    metadata={"type": "game"},
                )
            return
        await reply("Usage: game list | play <id> [min] | stop | look | status")

    async def cmd_status(self, channel: str, reply: ReplyFn, *, default_voice: bool = False) -> None:
        """Remote-control snapshot: what's on, what's running, anything pending."""
        s = self.signals
        lines = [
            f"autonomy: {'PAUSED' if s.autonomy_paused else 'active'}",
            f"thinking: {s.agent_thinking} | listening(mic): {s.listen_enabled} | inner-mirror: {s.inner_mirror}",
            f"voice replies: {'on' if self.voice_enabled(channel, default_voice) else 'off'}",
            f"max_tool_rounds: {self.agent.settings.max_tool_rounds} (resume: {self.agent.settings.tool_loop_resume})",
        ]
        pending = list(getattr(self.agent, "_pending_resume", {}).keys())
        lines.append(f"resume pending: {','.join(pending) if pending else 'none'}")
        try:
            from ophelia.providers.router import build_provider_stack
            stack = build_provider_stack(self.agent.settings)
            lines.append(f"chat provider: {stack.name('chat')} / {stack.model('chat')}")
        except Exception as e:
            lines.append(f"chat provider: ? ({e})")
        try:
            from ophelia.providers.router import _ollama_reachable
        except Exception:
            _ollama_reachable = None  # type: ignore
        if _ollama_reachable is not None:
            try:
                lines.append(f"ollama reachable: {_ollama_reachable(self.agent.settings)}")
            except Exception:
                pass
        await reply("\n".join(lines)[:4000])

    async def cmd_models(self, reply: ReplyFn) -> None:
        """Show the per-role provider/model routing — handy when away from the terminal."""
        from ophelia.providers.router import build_provider_stack
        try:
            stack = build_provider_stack(self.agent.settings)
        except Exception as e:
            await reply(f"models: ? ({e})")
            return
        roles = ["chat", "consciousness", "curator", "vision", "image", "video"]
        lines = []
        for role in roles:
            try:
                name = stack.name(role)  # type: ignore[arg-type]
                model = stack.model(role)  # type: ignore[arg-type]
                lines.append(f"{role}: {name} / {model}")
            except Exception as e:
                lines.append(f"{role}: ? ({e})")
        await reply("\n".join(lines)[:4000])

    async def cmd_help(self, reply: ReplyFn) -> None:
        await reply(
            "Commands:\n"
            "/status — what's on / running / pending\n"
            "/pause — pause autonomous outreach\n"
            "/resume — resume autonomous outreach\n"
            "/update [branch] — pull + reinstall (+ restart). Owner-only.\n"
            "         add `dirty` to allow local uncommitted changes\n"
            "         add `norestart` to skip restart\n"
            "/voice on|off — voice replies\n"
            "/listen on|off — local mic listening (Termux:API)\n"
            "/inner on|off|tail — inner-monologue mirror\n"
            "/game list|play <id>|stop|look\n"
            "/models — per-role provider/model routing\n"
            "/continue — resume an unfinished tool chain\n"
            "/tell <guest> <message> — relay an exact message to a guest\n"
            "/suggest <guest> <topic> — nudge her to reach out to a guest in her own words\n"
            "/revoke <guest> — instantly block a guest from messaging you\n"
            "/help — this list"
        )

    async def cmd_update(self, args: list[str], reply: ReplyFn) -> None:
        """Owner-only: git pull + pip install -e ., then schedule restart."""
        import asyncio

        from ophelia.setup.update import request_process_exit_soon, run_update

        branch: str | None = None
        allow_dirty = False
        restart = True
        for tok in args:
            low = tok.lower()
            if low in ("dirty", "--allow-dirty", "allow-dirty"):
                allow_dirty = True
            elif low in ("norestart", "no-restart", "--no-restart"):
                restart = False
            elif low in ("restart", "--restart"):
                restart = True
            elif not tok.startswith("-"):
                branch = tok

        await reply(
            "Updating"
            + (f" (branch `{branch}`)" if branch else "")
            + " — git pull + reinstall"
            + (" + restart" if restart else "")
            + ". Hang on…"
        )

        result = await asyncio.to_thread(
            run_update,
            branch=branch,
            restart=restart,
            allow_dirty=allow_dirty,
        )
        await reply(result.summary())
        if result.ok and result.restart_scheduled:
            request_process_exit_soon(2.5)

    async def _send_to_user(self, platform: str, user_id: int, message: str,
                            gateway_sender: SendToGuestFn | None = None) -> bool:
        """Send a DM to a user on any platform. Uses the hub (which knows all
        gateways) when available; falls back to the single-platform
        gateway_sender passed by the calling command handler."""
        if self.hub is not None:
            return await self.hub.send_to_user(platform, user_id, message)
        if gateway_sender is not None:
            return await gateway_sender(platform, user_id, message)
        log.warning("session.send_to_user_no_route", platform=platform, user=user_id)
        return False

    async def cmd_tell(
        self, args: list[str], reply: ReplyFn, *, send_to_guest: SendToGuestFn
    ) -> None:
        """Relay an exact message from the owner to a specific guest.

        No agent turn, no model — pure relay. Routes through the hub when
        available so the owner can message guests on any platform; falls back
        to the calling gateway's sender otherwise.
        """
        from ophelia.memory.guests import resolve_guest_target

        if len(args) < 2:
            await reply("Usage: /tell <guest> <message>")
            return
        target_query = args[0]
        message = " ".join(args[1:]).strip()
        if not message:
            await reply("Usage: /tell <guest> <message>")
            return
        resolved = await resolve_guest_target(self.agent.settings, self.memory, target_query)
        if not resolved:
            await reply(
                f"Couldn't resolve '{target_query}' to a known guest. "
                "Use a channel like 'telegram:111', a numeric id, or the "
                "guest's name (as you've named them or as they introduced themselves)."
            )
            return
        platform, user_id = resolved
        ok = await self._send_to_user(platform, user_id, message, send_to_guest)
        if ok:
            await reply(f"Sent to {platform}:{user_id}.")
        else:
            await reply(
                f"Failed to send to {platform}:{user_id}. "
                + (
                    "The guest may not have messaged the bot yet "
                    "(they need to /start it first on Telegram)."
                    if platform == "telegram"
                    else "Discord couldn't DM that user."
                )
            )

    async def cmd_suggest(
        self, args: list[str], reply: ReplyFn, *, send_to_guest: SendToGuestFn
    ) -> None:
        """Nudge Ophelia to reach out to a guest about a topic, in her own words.

        Spawns a real agent turn with a system nudge; the resulting message is
        sent to the guest AND cc'd to the owner (per user preference)."""
        from ophelia.memory.guests import (
            get_guest_name,
            resolve_guest_target,
        )

        if len(args) < 2:
            await reply("Usage: /suggest <guest> <topic>")
            return
        target_query = args[0]
        topic = " ".join(args[1:]).strip()
        if not topic:
            await reply("Usage: /suggest <guest> <topic>")
            return
        resolved = await resolve_guest_target(self.agent.settings, self.memory, target_query)
        if not resolved:
            await reply(
                f"Couldn't resolve '{target_query}' to a known guest. "
                "Use a channel like 'telegram:111', a numeric id, or the "
                "guest's name (as you've named them or as they introduced themselves)."
            )
            return
        platform, user_id = resolved
        name = await get_guest_name(
            self.memory, platform, user_id, data_dir=self.agent.settings.data_dir
        ) or f"{platform}:{user_id}"
        # Compose a one-shot agent turn that produces a short outbound message.
        prompt = (
            f"The owner suggests you reach out to {name} ({platform}:{user_id}) "
            f"about: {topic}. Compose a short, warm message to {name} — in your "
            "own voice, as if texting them. Just the message body, no preamble, "
            "no quotes, no 'hey this is ophelia'. One or two sentences."
        )
        try:
            outbound = await self.agent.compose_message(
                channel=f"{platform}:{user_id}",
                user_text=prompt,
                is_owner=True,  # the owner issued this; full context
            )
        except Exception as e:
            log.warning("session.suggest_compose_failed", error=str(e))
            await reply(f"Couldn't compose a message: {e}")
            return
        outbound = (outbound or "").strip()
        if not outbound:
            await reply("She didn't produce a message — try rephrasing the topic.")
            return
        ok = await self._send_to_user(platform, user_id, outbound, send_to_guest)
        if ok:
            # CC the owner a copy (per user preference).
            await reply(f"To {name} ({platform}:{user_id}):\n\n{outbound}")
        else:
            await reply(
                f"Failed to send to {platform}:{user_id}. "
                + (
                    "The guest may not have messaged the bot yet "
                    "(they need to /start it first on Telegram)."
                    if platform == "telegram"
                    else "Discord couldn't DM that user."
                )
                + f" Draft was:\n\n{outbound}"
            )

    async def cmd_revoke(
        self, args: list[str], reply: ReplyFn, *, guest_approvals
    ) -> None:
        """Instantly revoke a guest's access: remove from allowlist + mark denied.

        The owner's kill switch — no need to edit .env and restart. The guest
        is immediately locked out on their next message. `guest_approvals` is
        the GuestApprovals instance from the gateway.
        """
        from ophelia.channels.guest_approval import remove_user_from_allowlist
        from ophelia.memory.guests import resolve_guest_target

        if not args:
            await reply("Usage: /revoke <guest>")
            return
        target_query = args[0].strip()
        resolved = await resolve_guest_target(
            self.agent.settings, self.memory, target_query
        )
        if not resolved:
            # Even if they're not currently in the allowlist (already revoked?),
            # allow revoking by raw channel form so the owner can be sure.
            if ":" in target_query:
                platform, _, id_s = target_query.partition(":")
                if id_s.isdigit():
                    resolved = (platform.lower(), int(id_s))
            if not resolved:
                await reply(
                    f"Couldn't resolve '{target_query}'. Use a channel like "
                    "'telegram:111', a numeric id, or the guest's name."
                )
                return
        platform, user_id = resolved
        # Don't let the owner revoke themselves.
        if self.agent.settings.is_owner_channel(f"{platform}:{user_id}"):
            await reply("That's you — can't revoke the owner.")
            return
        removed = remove_user_from_allowlist(self.agent.settings, platform, user_id)
        guest_approvals.set_status(platform, user_id, "denied")
        action = "removed from allowlist and blocked" if removed else "blocked (wasn't in allowlist)"
        await reply(f"{platform}:{user_id} — {action}. They can't message you anymore.")
