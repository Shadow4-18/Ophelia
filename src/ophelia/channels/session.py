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
from ophelia.mind.drives import DriveState

log = structlog.get_logger()

ReplyFn = Callable[[str], Awaitable[None]]
MediaReplyFn = Callable[[Path, str], Awaitable[bool]]


def _sender_id(channel: str) -> str:
    """Extract the platform id from a 'platform:id' channel string."""
    return channel.split(":", 1)[1] if ":" in channel else channel


class ChannelSession:
    """Run agent turns and shared slash/bang commands."""

    WELCOME = (
        "Ophelia online.\n"
        "Commands: /status /pause /resume /voice /listen /inner /game /models /help\n"
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
        self._voice_reply: dict[str, bool] = {}
        self._chat_logger: ChatLogger | None = None

    def _logger(self) -> ChatLogger | None:
        if not self.agent.settings.chat_log_enabled:
            return None
        if self._chat_logger is None:
            self._chat_logger = ChatLogger.from_settings(self.agent.settings)
        return self._chat_logger

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
    ) -> None:
        settings = self.agent.settings
        is_owner = settings.is_owner_channel(channel)
        sender_id = _sender_id(channel)
        logger = self._logger()

        self.signals.last_user_message_at = time.time()
        await self.signals.set_user_talking(True)
        await self.signals.set_agent_thinking(True)
        # Let send_message tool push follow-ups mid-turn through this channel.
        self.agent.tools.set_message_sender(reply)
        self.agent.tools.set_owner(is_owner)
        if media_reply is not None:
            self.agent.tools.set_media_sender(media_reply)

        # Log the inbound message (text + any referenced photo path) — universal,
        # for owner oversight. The "[User sent a photo — saved <path>]" prompt
        # text carries the inbound media filename; we capture it explicitly too.
        if logger:
            inbound_media = self._extract_inbound_media(text, settings)
            await logger.log(
                channel=channel,
                direction="in",
                text=text,
                media_path=inbound_media,
                media_kind="photo" if inbound_media else None,
                sender_id=sender_id,
                is_owner=is_owner,
                role="user",
            )

        # Wrap the reply/media senders so every outbound chunk + media file is
        # logged too. Generated images appear as "saved to <path>" in the reply
        # text — extract and log those as outbound media as well.
        async def _logged_reply(chunk: str) -> None:
            if logger:
                await logger.log(
                    channel=channel,
                    direction="out",
                    text=chunk,
                    sender_id=sender_id,
                    is_owner=is_owner,
                    role="assistant",
                )
                for p in artifact_paths_in_text(chunk):
                    await logger.log(
                        channel=channel,
                        direction="out",
                        text=f"[media sent: {p.name}]",
                        media_path=p,
                        media_kind="generated",
                        sender_id=sender_id,
                        is_owner=is_owner,
                        role="media",
                    )
            await reply(chunk)

        logged_media_reply: MediaReplyFn | None = None
        if media_reply is not None:
            async def _logged_media(path: Path, caption: str) -> bool:
                ok = await media_reply(path, caption)
                if logger:
                    await logger.log(
                        channel=channel,
                        direction="out",
                        text=caption or f"[media sent: {path.name}]",
                        media_path=path,
                        media_kind="file",
                        sender_id=sender_id,
                        is_owner=is_owner,
                        role="media",
                    )
                return ok
            logged_media_reply = _logged_media

        try:
            out = await self.agent.run_turn(channel, text, is_owner=is_owner)
            # Only the owner's messages shape her drives/will. Guests don't.
            if is_owner:
                self.drives.on_user_message()
                await self.memory.save_drives(self.drives)
            self.signals.last_agent_message_at = time.time()
            for i, chunk in enumerate(split_messages(out)):
                if i:
                    await asyncio.sleep(1.2)
                await _logged_reply(chunk)
        except Exception as e:
            log.exception("channel.chat_error", channel=channel)
            await _logged_reply(f"Error: {e}")
        finally:
            self.agent.tools.clear_message_sender()
            self.agent.tools.clear_media_sender()
            self.agent.tools.clear_owner()
            await self.signals.set_user_talking(False)
            await self.signals.set_agent_thinking(False)

    @staticmethod
    def _extract_inbound_media(text: str, settings) -> str | None:
        """Pull the saved-photo filename out of the gateway's photo prompt text
        and resolve it to the absolute path the gateway downloaded it to."""
        marker = "saved "
        idx = text.find(marker)
        if idx < 0:
            return None
        rest = text[idx + len(marker) :]
        end = rest.find("]")
        token = rest[:end] if end >= 0 else rest.split()[0]
        token = token.strip().strip(".")
        if not token:
            return None
        # Gateways download inbound photos to <data_dir>/telegram_media/<name>.
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
            "/voice on|off — voice replies\n"
            "/listen on|off — local mic listening (Termux:API)\n"
            "/inner on|off|tail — inner-monologue mirror\n"
            "/game list|play <id>|stop|look\n"
            "/models — per-role provider/model routing\n"
            "/continue — resume an unfinished tool chain\n"
            "/help — this list"
        )
