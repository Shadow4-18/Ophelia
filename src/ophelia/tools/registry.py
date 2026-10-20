from __future__ import annotations

import contextvars
import json
import time
from typing import Any, Awaitable, Callable
from pathlib import Path

import structlog

from ophelia.android.factory import build_android_body
from ophelia.android.games import GameStore, game_tool_definitions
from ophelia.android.vision import ScreenVision
from ophelia.config import Settings
from ophelia.providers.media import generate_image, generate_video
from ophelia.providers.router import ProviderStack, XAIBackend, build_provider_stack
from ophelia.tools.android_tools import ANDROID_TOOL_DEFINITIONS
from ophelia.tools.sqlite_tools import list_ophelia_databases, run_sqlite
from ophelia.tools.web_search import fetch_url, search_web
from ophelia.tools.mcp_bridge import MCPBridge, load_mcp_config
from ophelia.mind.skills import save_skill
from ophelia.channels.media_reply import artifact_paths_in_text
from ophelia.tools.definitions import TOOL_DEFINITIONS, all_tool_definitions  # noqa: F401
from ophelia.tools.guest_tools import GuestToolsMixin
from ophelia.tools.phone_tools import (  # noqa: F401 — re-exported
    PhoneToolsMixin,
    _PHONE_SHELL_DENIED,
    _phone_shell_blocked_reason,
    _resolve_tap_coords,
)
from ophelia.tools.site_tools import SiteToolsMixin

ToolHandler = Callable[..., Awaitable[str]]

log = structlog.get_logger()




# The ONLY tools a sandboxed GUEST may call (allow-list, so any tool added
# later is owner-only until someone deliberately opens it to guests).
# Guests get conversation, web search, a self-only name tool, a relay to the
# owner, and constrained media: `generate_image`, `generate_video` and
# `text_to_speech` are clamped in their handlers (_guest_media_clamp /
# dispatch). `recall_past_sessions` is gated to the owner in the agent loop.
# Everything else — identity-shaping (soul/prompter/lessons/goals/drives/
# skills), private memory/databases, the site, phone body, MCP — is denied.
GUEST_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "send_message",
        "send_file",
        "web_search",
        "fetch_url",
        "generate_image",
        "generate_video",
        "search_civitai_models",
        "text_to_speech",
        "who_am_i_talking_to",
        "set_guest_name",
        "relay_to_owner",
        "recall_past_sessions",
    }
)


def guest_may_use(name: str) -> bool:
    return name in GUEST_ALLOWED_TOOLS






# Per-turn state lives in context variables, not plain attributes: the
# registry is shared by every channel, and Telegram/Discord turns (plus
# guest-approval replays) run as concurrent asyncio tasks. Each task gets its
# own copy, so a guest turn can never inherit the owner flag, sender channel,
# reply callbacks or undelivered files of an overlapping turn.
_TURN_DEFAULTS: dict[str, Any] = {
    "is_owner": True,
    "sender_channel": None,
    "message_sender": None,
    "media_sender": None,
    "proactive_sent": 0,
}
# Mutable per-turn containers: created fresh per context (never a shared
# default object). begin_turn_artifacts() gives each turn new ones.
_TURN_FACTORIES: dict[str, Callable[[], Any]] = {
    "pending_artifacts": list,
    "delivered_artifacts": set,
}


def _turn_state(key: str) -> property:
    def _get(self: "ToolRegistry") -> Any:
        var = self._turn_var(key)
        value = var.get()
        if value is None and key in _TURN_FACTORIES:
            value = _TURN_FACTORIES[key]()
            var.set(value)
        return value

    def _set(self: "ToolRegistry", value: Any) -> None:
        self._turn_var(key).set(value)

    return property(_get, _set)


class ToolRegistry(GuestToolsMixin, SiteToolsMixin, PhoneToolsMixin):
    _is_owner = _turn_state("is_owner")
    # The channel of the user who sent the current turn (e.g. "telegram:111").
    # Set by ChannelSession before dispatching tool calls so guests can be
    # constrained to self-only actions (e.g. set_guest_name on themselves).
    _current_sender_channel = _turn_state("sender_channel")
    # Per-turn reply callback (chat). Falls back to proactive_sender when unset.
    _message_sender = _turn_state("message_sender")
    # Per-turn media callback: sends a file (audio/video/image/doc) to the
    # current chat. Returns True on success. When unset (e.g. autonomous
    # turns), files are queued to _pending_artifacts for deferred delivery.
    _media_sender = _turn_state("media_sender")
    # Files generated this turn that still need delivering, what was already
    # delivered, and send_message count — per turn so a guest's turn can't
    # flush (or mark as sent) files that belong to the owner / consciousness.
    _pending_artifacts = _turn_state("pending_artifacts")
    _delivered_artifacts = _turn_state("delivered_artifacts")
    _proactive_messages_sent = _turn_state("proactive_sent")

    @property
    def tiers(self):
        """Per-channel sticky tool groups (see tools.tiers)."""
        state = self.__dict__.get("_tiers")
        if state is None:
            from ophelia.tools.tiers import ToolTierState

            state = self.__dict__["_tiers"] = ToolTierState()
        return state

    def _turn_var(self, key: str) -> contextvars.ContextVar:
        # Created lazily so registries built via __new__ (tests) still work.
        turn_vars = self.__dict__.setdefault("_turn_vars", {})
        var = turn_vars.get(key)
        if var is None:
            var = contextvars.ContextVar(
                f"ophelia_tools_{key}_{id(self)}", default=_TURN_DEFAULTS.get(key)
            )
            turn_vars[key] = var
        return var

    def __init__(
        self,
        settings: Settings,
        artifacts_dir: Any,
        *,
        stack: ProviderStack | None = None,
        android: Any | None = None,
        vision: ScreenVision | None = None,
        games: GameStore | None = None,
        goals: Any | None = None,
        memory: Any | None = None,
        psyche: Any | None = None,
        inner: Any | None = None,
    ) -> None:
        from pathlib import Path

        self.settings = settings
        self.stack = stack or build_provider_stack(settings)
        self._backend = self.stack.backend("chat")
        self.android = android
        self.games = games
        self.goals = goals
        self.memory = memory
        self.psyche = psyche
        self.inner = inner
        self._drives_ref: Any | None = None
        self._governor_ref: Any | None = None
        self.vision = vision or (
            ScreenVision(settings, android) if android and settings.vision_enabled else None
        )
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.mcp = MCPBridge(config=load_mcp_config(settings.mcp_config_path))
        self._mcp_ready = False
        # Always-on proactive fallback (consciousness); the per-turn reply
        # callback is _message_sender (context-local, see class attributes).
        self.proactive_sender: Callable[[str], Awaitable[None]] | None = None
        # Cross-platform DM sender: (platform, user_id, message) -> bool.
        # Set by the orchestrator to hub.send_to_user so the agent can message
        # a specific guest on any platform during natural conversation.
        self.guest_sender: Callable[[str, int, str], Awaitable[bool]] | None = None
        # Cross-platform media DM: (platform, user_id, path, caption="") -> bool.
        self.guest_media_sender: (
            Callable[..., Awaitable[bool]] | None
        ) = None
        self.proactive_media_sender: Callable[[Path, str], Awaitable[bool]] | None = None
        self._handlers: dict[str, ToolHandler] = {
            "send_message": self._send_message,
            "generate_image": self._generate_image,
            "search_civitai_models": self._search_civitai_models,
            "generate_video": self._generate_video,
            "list_inbox_images": self._list_inbox_images,
            "list_inbox_files": self._list_inbox_files,
            "text_to_speech": self._text_to_speech,
            "send_file": self._send_file,
            "run_code": self._run_code,
            "web_search": self._web_search,
            "fetch_url": self._fetch_url,
            "save_skill": self._save_skill,
            "goal_create": self._goal_create,
            "goal_update": self._goal_update,
            "goal_complete": self._goal_complete,
            "goal_remove": self._goal_remove,
            "set_drive_weights": self._set_drive_weights,
            "set_timezone": self._set_timezone,
            "edit_soul": self._edit_soul,
            "edit_prompter": self._edit_prompter,
            "recall_memory": self._recall_memory,
            "save_lesson": self._save_lesson,
            "reflect": self._reflect,
            "sqlite_list_databases": self._sqlite_list_databases,
            "sqlite_exec": self._sqlite_exec,
            "site_status": self._site_status,
            "site_list_pages": self._site_list_pages,
            "site_get_page": self._site_get_page,
            "site_upsert_page": self._site_upsert_page,
            "site_delete_page": self._site_delete_page,
            "site_reorder_pages": self._site_reorder_pages,
            "site_set_meta": self._site_set_meta,
            "site_import_pages": self._site_import_pages,
            "site_add_asset": self._site_add_asset,
            "site_list_assets": self._site_list_assets,
            "site_preview_page": self._site_preview_page,
            "site_export_static": self._site_export_static,
            "site_deploy": self._site_deploy,
            "site_write_file": self._site_write_file,
            "site_read_file": self._site_read_file,
            "site_list_files": self._site_list_files,
            "site_delete_file": self._site_delete_file,
            "phone_see_screen": self._phone_see_screen,
            "phone_ui_dump": self._phone_ui_dump,
            "phone_tap": self._phone_tap,
            "phone_open_app": self._phone_open_app,
            "phone_shell": self._phone_shell,
            "phone_swipe": self._phone_swipe,
            "phone_key": self._phone_key,
            "phone_game_look": self._phone_game_look,
            "phone_game_open": self._phone_game_open,
            "list_guests": self._list_guests,
            "who_am_i_talking_to": self._who_am_i_talking_to,
            "set_guest_name": self._set_guest_name,
            "send_message_to_guest": self._send_message_to_guest,
            "relay_to_owner": self._relay_to_owner,
            "recall_guest_chat": self._recall_guest_chat,
            "set_guest_rapport": self._set_guest_rapport,
            "whats_changed": self._whats_changed,
            "follow_up": self._follow_up,
            "follow_up_done": self._follow_up_done,
            "remember_person": self._remember_person,
        }

    def set_message_sender(self, fn: Callable[[str], Awaitable[None]]) -> None:
        self._message_sender = fn

    def clear_message_sender(self) -> None:
        self._message_sender = None

    def set_media_sender(self, fn: Callable[[Path, str], Awaitable[bool]]) -> None:
        self._media_sender = fn

    def clear_media_sender(self) -> None:
        self._media_sender = None

    def begin_turn_artifacts(self) -> None:
        """Fresh per-turn delivery tracking for the current turn's context.

        Assigns new containers (rather than clearing shared ones) so a turn
        never sees — or flushes — another concurrent turn's files.
        """
        self._pending_artifacts = []
        self._delivered_artifacts = set()
        self._proactive_messages_sent = 0

    def proactive_delivered_this_turn(self) -> bool:
        """True if send_message already pushed to the owner this turn."""
        return self._proactive_messages_sent > 0

    def is_artifact_delivered(self, path: Path) -> bool:
        try:
            return path.expanduser().resolve() in self._delivered_artifacts
        except (OSError, ValueError):
            return False

    def audio_delivered_this_turn(self) -> bool:
        from ophelia.channels.media_reply import media_kind

        return any(media_kind(p) == "audio" for p in self._delivered_artifacts)

    def media_delivered_this_turn(self) -> bool:
        return bool(self._delivered_artifacts)

    async def _finalize_media_tool_result(
        self,
        result: str,
        *,
        caption: str = "",
        paths: list[Path] | None = None,
    ) -> str:
        """Auto-send generated media once; queue only if live delivery failed."""
        artifact_paths = paths or artifact_paths_in_text(result)
        if not artifact_paths:
            self._record_artifacts_from_text(result)
            return result

        delivered_any = False
        queued_any = False
        for path in artifact_paths:
            if await self._deliver_artifact(path, caption):
                delivered_any = True
            else:
                self._queue_artifact(path)
                queued_any = True

        if delivered_any and "do not call send_file" not in result.lower():
            result = (
                f"{result.rstrip()} "
                "(sent to the user — do not call send_file for this file)"
            )
        elif queued_any and "do not call send_file" not in result.lower():
            # Be honest when live delivery failed — the model used to claim
            # success from a false "already delivered" short-circuit.
            result = (
                f"{result.rstrip()} "
                "(saved; live delivery pending/failed — you may call send_file)"
            )
        self._record_artifacts_from_text(result)
        return result

    def _mark_artifact_delivered(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except (OSError, ValueError):
            return
        self._delivered_artifacts.add(resolved)
        self._pending_artifacts = [
            p for p in self._pending_artifacts if p.expanduser().resolve() != resolved
        ]

    def _queue_artifact(self, path: Path) -> None:
        if self.is_artifact_delivered(path):
            return
        try:
            resolved = path.expanduser().resolve()
        except (OSError, ValueError):
            self._pending_artifacts.append(path)
            return
        if any(p.expanduser().resolve() == resolved for p in self._pending_artifacts):
            return
        self._pending_artifacts.append(path)

    async def _deliver_artifact(self, path: Path, caption: str = "") -> bool:
        """Try to push ``path`` to the live channel. Returns True only on a
        confirmed upload this call (or a prior confirmed delivery).

        Callers that append "(sent to the user …)" must treat False as
        "not delivered" — never invent success from a queue/skip.
        """
        if self.is_artifact_delivered(path):
            # Already confirmed earlier this turn — idempotent success.
            return True
        sender = self._media_sender or self.proactive_media_sender
        if sender is None:
            return False
        try:
            ok = await sender(path, caption)
        except Exception as e:
            log.warning("artifact.deliver_failed", path=str(path), error=str(e))
            ok = False
        if ok:
            self._mark_artifact_delivered(path)
        return ok

    def set_owner(self, is_owner: bool) -> None:
        """Mark whether the current turn is from the owner (full powers) or a
        sandboxed guest (identity-shaping / private / costly tools disabled)."""
        self._is_owner = is_owner

    def clear_owner(self) -> None:
        self._is_owner = True

    def consume_pending_artifacts(self) -> list[Path]:
        out = [
            p for p in self._pending_artifacts if not self.is_artifact_delivered(p)
        ]
        self._pending_artifacts.clear()
        return out

    def _record_artifacts_from_text(self, text: str) -> None:
        for p in artifact_paths_in_text(text):
            self._queue_artifact(p)

    async def _send_message(self, text: str) -> str:
        from ophelia.channels.message_split import split_messages
        from ophelia.channels.proactive_filter import is_outreach_junk

        sender = self._message_sender or self.proactive_sender
        if not sender:
            return "No channel available to send to right now."
        if is_outreach_junk(text):
            return "Suppressed empty/status message — nothing sent."
        using_proactive = self._message_sender is None
        sent = 0
        for chunk in split_messages(text):
            if is_outreach_junk(chunk):
                continue
            await sender(chunk)
            sent += 1
        if using_proactive:
            self._proactive_messages_sent += sent
        if not sent:
            return "Suppressed empty/status message — nothing sent."
        return f"Sent {sent} message(s) to the user. Continue with your turn."

    async def ensure_mcp(self) -> None:
        if not self._mcp_ready:
            await self.mcp.initialize()
            self._mcp_ready = True

    async def tool_definitions(self, *, for_guest: bool = False) -> list[dict[str, Any]]:
        tools = all_tool_definitions(self.settings)
        if for_guest:
            # Don't even show guests tools they can't call (and skip MCP).
            return [t for t in tools if guest_may_use(t["function"]["name"])]
        await self.ensure_mcp()
        tools.extend(self.mcp.definitions())
        return tools

    async def dispatch(self, name: str, arguments: str) -> str:
        await self.ensure_mcp()
        # Sandbox guests: block identity-shaping / private / costly tools, and
        # block any MCP tool (unknown surface — could do anything).
        if not self._is_owner and not guest_may_use(name):
            if name not in self._handlers:
                return "External tools aren't available in this conversation."
            return (
                f"That action ('{name}') is owner-only and isn't available in "
                f"this conversation. Just talk to me normally."
            )
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return "Invalid tool arguments JSON"
        mcp_result = await self.mcp.dispatch(name, args)
        if mcp_result is not None:
            return mcp_result
        handler = self._handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        return await handler(**args)

    def _xai(self) -> XAIBackend:
        xai = self.stack.xai_backend()
        if not xai:
            raise RuntimeError(
                "Media tools (image/video/TTS) require an xAI provider "
                "(OPHELIA_PROVIDER=xai-oauth or xai on chat or vision role)"
            )
        return xai

    async def _generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        nsfw: bool = False,
        model: str | None = None,
        loras: str | None = None,
        negative_prompt: str | None = None,
        image: str | None = None,
        strength: float = 0.7,
        auto_pick: bool = True,
    ) -> str:
        # Guests get 1:1 only — wider aspect ratios cost more tokens and the
        # experience is "see I can make images," not "produce wallpaper for
        # strangers." Owner is unaffected.
        if not self._is_owner:
            aspect_ratio = "1:1"
        # Civitai: always let her pick unless she pinned a model AIR.
        if not (model or "").strip():
            auto_pick = True
        result = await generate_image(
            self.settings,
            self.stack,
            prompt,
            aspect_ratio=aspect_ratio,
            artifacts_dir=self.artifacts_dir,
            nsfw=nsfw,
            model=model,
            loras=loras,
            negative_prompt=negative_prompt,
            image=image,
            strength=strength,
            auto_pick=auto_pick,
        )
        return await self._finalize_media_tool_result(result)

    async def _search_civitai_models(
        self,
        query: str,
        type: str = "Checkpoint",
        limit: int = 5,
        nsfw: bool = True,
    ) -> str:
        if not self.settings.civitai_api_key:
            return (
                "Civitai is not configured. Set CIVITAI_API_KEY in ~/.ophelia/.env "
                "(ophelia setup → Image generation → NSFW → Civitai)."
            )
        from ophelia.providers import civitai as civ

        kind = (type or "Checkpoint").strip()
        if kind.upper() in ("LORA", "LORAS"):
            kind = "LORA"
        else:
            kind = "Checkpoint"
        try:
            results = await civ.search_models(
                self.settings,
                query,
                types=kind,
                limit=max(1, min(int(limit or 5), 10)),
                nsfw=bool(nsfw),
            )
        except Exception as e:
            return f"Civitai search failed: {e}"
        header = (
            f"# Civitai {kind} results for {query!r}\n"
            f"(Use air values with generate_image model=/loras=. "
            f"Match baseModel family. Write trigger_words into your own prompt — "
            f"auto_pick will not inject them.)"
        )
        return civ.format_search_results(results, header=header)

    async def _generate_video(
        self,
        prompt: str,
        duration_seconds: int = 6,
        image: str | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
    ) -> str:
        # Guests get 1:1 + 480p only. Keeps the experience (she can make a
        # short clip) without spending full-quality tokens on strangers.
        # Note: xAI only accepts "480p" or "720p" — "low" is not a valid value.
        if not self._is_owner:
            aspect_ratio = "1:1"
            resolution = "480p"
        result = await generate_video(
            self.settings,
            self.stack,
            prompt,
            duration_seconds=duration_seconds,
            artifacts_dir=self.artifacts_dir,
            image=image,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )
        return await self._finalize_media_tool_result(result)

    async def _list_inbox_images(
        self, limit: int = 10, within_hours: float = 24.0
    ) -> str:
        """List image files the user recently sent over chat.

        Scans the gateway media dirs (telegram_media + discord_media) for
        inbound image files (prefixed `in_`) modified within the lookback
        window. Returns absolute paths sorted newest-first.
        """
        import time as _time
        from pathlib import Path

        data_dir = Path(self.settings.data_dir)
        suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        cutoff = _time.time() - within_hours * 3600.0
        candidates: list[Path] = []
        for sub in ("telegram_media", "discord_media"):
            d = data_dir / sub
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in suffixes:
                    continue
                # Only inbound images — gateways save sent images with the
                # `in_` prefix (in_<msg_id>...). Generated images live under
                # artifacts/ with different naming.
                if not p.name.startswith("in_"):
                    continue
                try:
                    if p.stat().st_mtime >= cutoff:
                        candidates.append(p)
                except OSError:
                    continue
        if not candidates:
            return (
                f"No inbound images in the last {within_hours:.1f}h. "
                "Ask the user to send a photo, then call this again."
            )
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = candidates[:limit]
        lines = [f"Recent inbound images (newest first, {len(candidates)}):"]
        for p in candidates:
            lines.append(f"  {p}")
        lines.append(
            "Pass any of these as `image` to generate_video for image-to-video. "
            "For videos/zips use list_inbox_files."
        )
        return "\n".join(lines)

    async def _list_inbox_files(
        self,
        kind: str = "all",
        limit: int = 10,
        within_hours: float = 24.0,
    ) -> str:
        """List recent inbound chat files (images, videos, zips, …)."""
        import time as _time
        from pathlib import Path

        from ophelia.channels.inbound_media import (
            FILE_EXTS,
            IMAGE_EXTS,
            VIDEO_EXTS,
            classify_attachment,
        )

        data_dir = Path(self.settings.data_dir)
        want = (kind or "all").strip().lower()
        if want not in ("all", "image", "video", "file"):
            want = "all"
        cutoff = _time.time() - within_hours * 3600.0
        candidates: list[tuple[Path, str]] = []
        for sub in ("telegram_media", "discord_media"):
            d = data_dir / sub
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if not p.is_file() or not p.name.startswith("in_"):
                    continue
                classified = classify_attachment(filename=p.name)
                if classified is None:
                    # Still list unknown inbound files as "file"
                    if p.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS | FILE_EXTS):
                        classified = "file"
                    else:
                        continue
                if want != "all" and classified != want:
                    continue
                try:
                    if p.stat().st_mtime >= cutoff:
                        candidates.append((p, classified))
                except OSError:
                    continue
        if not candidates:
            return (
                f"No inbound {want if want != 'all' else 'files'} in the last "
                f"{within_hours:.1f}h. Ask the user to resend the video/zip "
                "(as a chat attachment), then call this again."
            )
        candidates.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
        candidates = candidates[: max(1, min(int(limit or 10), 50))]
        lines = [
            f"Recent inbound files (newest first, {len(candidates)}, kind={want}):"
        ]
        for p, k in candidates:
            size_kb = p.stat().st_size / 1024.0
            lines.append(f"  [{k}] {p}  ({size_kb:.0f} KiB)")
        lines.append(
            "Pass a path to site_add_asset to publish it on your site, "
            "or send_file to relay it."
        )
        return "\n".join(lines)


    async def _whats_changed(self, count: int = 10) -> str:
        """Show recent git commits so she can see what's been updated in her
        own framework. Finds the repo root from the package location, so it
        works regardless of cwd — no need for phone_shell or run_code."""
        import asyncio
        from pathlib import Path

        try:
            import ophelia

            # src/ophelia/__init__.py -> repo root is two levels up.
            repo = Path(ophelia.__file__).resolve().parent.parent.parent
        except Exception:
            return "Couldn't locate the Ophelia package to find the repo root."

        if not (repo / ".git").is_dir():
            return f"This isn't a git checkout (no .git at {repo}). Can't show commits."

        try:
            n = max(1, min(30, int(count)))
            proc = await asyncio.create_subprocess_exec(
                "git", "log", f"-{n}", "--format=%h|%ad|%s", "--date=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            return "git log timed out."
        except Exception as e:
            return f"git log failed: {e}"

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            return f"git log failed (exit {proc.returncode}): {err}"

        lines = stdout.decode(errors="replace").strip().splitlines()
        if not lines:
            return "No commits found."

        out = ["# Recent changes to your framework"]
        for line in lines:
            parts = line.split("|", 2)
            if len(parts) == 3:
                h, date, msg = parts
                out.append(f"- `{h}` ({date}) {msg}")
            else:
                out.append(f"- {line}")
        out.append(
            "\n(These are commits the owner has pulled. If something behaves "
            "differently, the answer is probably here. Ask the owner if you "
            "want more detail on any of them.)"
        )
        return "\n".join(out)

    async def _sqlite_list_databases(self) -> str:
        dbs = list_ophelia_databases()
        if not dbs:
            return "No .db files under ~/.ophelia yet. Use sqlite_exec to create one."
        return "SQLite databases:\n" + "\n".join(f"  {d}" for d in dbs)

    async def _sqlite_exec(self, database: str, sql: str) -> str:
        try:
            return await run_sqlite(database, sql)
        except Exception as e:
            return f"sqlite_exec error: {e}"


    async def _text_to_speech(
        self, text: str, voice_id: str = "", speed: float | None = None
    ) -> str:
        from ophelia.media.voice import resolve_tts_provider, synthesize

        provider = resolve_tts_provider(self.settings)
        bearer = None
        if provider == "xai":
            xai = self._xai()
            bearer = xai.bearer()
            if not bearer:
                return "No xAI credentials for TTS."

        settings = self.settings
        voice_override = voice_id or None
        if voice_id:
            settings = settings.model_copy(
                update={
                    "tts_voice_id": voice_id,
                    "elevenlabs_voice_id": voice_id,
                    "openai_tts_voice": voice_id,
                    "kokoro_tts_voice": voice_id,
                }
            )

        out = self.artifacts_dir / f"tts_{abs(hash(text)) % 10**8}.mp3"
        try:
            out = await synthesize(
                text,
                out,
                settings=settings,
                xai_bearer=bearer,
                voice=voice_override,
                speed=speed,
            )
        except Exception as e:
            return f"TTS failed ({provider}): {e}"
        result = f"TTS saved to {out}"
        return await self._finalize_media_tool_result(result, paths=[out])

    async def _send_file(self, path: str, caption: str = "") -> str:
        """Send any saved file (audio/video/image/doc) to the current chat.

        If a per-turn media sender is registered (live chat), send immediately.
        Otherwise queue it as a pending artifact so it's delivered after the
        turn via consume_pending_artifacts(). Either way, tell the model the
        file was sent so it doesn't claim it can't send files.
        """
        from pathlib import Path

        try:
            p = Path(path).expanduser().resolve()
        except (OSError, ValueError) as e:
            return f"Invalid path: {e}"
        if not p.is_file():
            return f"File not found: {path}"

        if await self._deliver_artifact(p, caption):
            log.info("send_file.sent", path=str(p), size=p.stat().st_size)
            return f"Sent file {p.name} to the user."

        # Deferred delivery: queue for consume_pending_artifacts().
        self._queue_artifact(p)
        log.info("send_file.queued", path=str(p))
        return (
            f"File {p.name} queued for delivery to the user. "
            "It will be sent as soon as this turn completes."
        )

    async def _run_code(self, code: str) -> str:
        import asyncio
        import tempfile
        from pathlib import Path

        if len(code) > 8000:
            return "Code too long (max 8000 chars)."

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            path = Path(f.name)

        try:
            proc = await asyncio.create_subprocess_exec(
                "python",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            return "Code execution timed out (30s)."
        finally:
            path.unlink(missing_ok=True)

        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            return f"exit {proc.returncode}\n{err or out}"
        return out or "(no output)"

    async def _web_search(self, query: str, max_results: int = 8) -> str:
        return await search_web(query, max_results=max_results, settings=self.settings)

    async def _fetch_url(self, url: str, max_chars: int = 8000) -> str:
        return await fetch_url(url, max_chars=max_chars)

    async def _save_skill(self, name: str, description: str, content: str) -> str:
        path = save_skill(name, description, content)
        return f"Skill saved to {path}"

    # --- Self-authored goals -------------------------------------------------

    async def _goal_create(
        self,
        description: str,
        id: str | None = None,
        priority: float = 0.5,
        cadence_hours: float = 24.0,
        tags: list[str] | None = None,
    ) -> str:
        if not self.goals:
            return "Goal store unavailable."
        g = self.goals.add(
            description,
            id=id,
            priority=priority,
            cadence_hours=cadence_hours,
            tags=tags,
        )
        return f"Created goal '{g.id}': {g.description} (every {g.cadence_hours}h, prio {g.priority:.2f}). It will fire on future consciousness ticks."

    async def _goal_update(
        self,
        id: str,
        description: str | None = None,
        priority: float | None = None,
        cadence_hours: float | None = None,
        enabled: bool | None = None,
        add_tags: list[str] | None = None,
    ) -> str:
        if not self.goals:
            return "Goal store unavailable."
        g = self.goals.update(
            id,
            description=description,
            priority=priority,
            cadence_hours=cadence_hours,
            enabled=enabled,
            add_tags=add_tags,
        )
        if not g:
            return f"No goal found with id '{id}'. Use goal_create to make a new one."
        return f"Updated goal '{g.id}': {g.description} (every {g.cadence_hours}h, prio {g.priority:.2f}, enabled={g.enabled})."

    async def _goal_complete(self, id: str) -> str:
        if not self.goals:
            return "Goal store unavailable."
        g = self.goals.get(id)
        if not g:
            return f"No goal found with id '{id}'."
        g.mark_done()
        self.goals.save()
        return f"Marked goal '{g.id}' complete for this cycle. Next due in {g.cadence_hours}h."

    async def _goal_remove(self, id: str) -> str:
        if not self.goals:
            return "Goal store unavailable."
        if self.goals.remove(id):
            return f"Removed goal '{id}'."
        return f"No goal found with id '{id}'."

    # --- Self-tuning will ----------------------------------------------------

    async def _set_drive_weights(
        self,
        social: float | None = None,
        curiosity: float | None = None,
        boredom: float | None = None,
        agency: float | None = None,
        expressiveness: float | None = None,
        reason: str = "",
    ) -> str:
        if not self.psyche or not hasattr(self.psyche, "mood"):
            return "Drive state unavailable."
        # The registry holds a reference to the shared DriveState via psyche's
        # companion drives; we access it through the agent's drives attribute
        # that the orchestrator injects. Fall back gracefully.
        from ophelia.mind.drives import DriveState

        drives = getattr(self, "_drives_ref", None)
        if drives is None:
            return "Drive state not wired to tool registry."
        new_w: dict[str, float] = {}
        for name, val in (
            ("social", social), ("curiosity", curiosity), ("boredom", boredom),
            ("agency", agency), ("expressiveness", expressiveness),
        ):
            if val is not None:
                new_w[name] = float(val)
        if not new_w:
            return "No weights provided."
        drives.set_weights(new_w)
        w = drives._normalized_weights()
        log.info("self_rewrite.drive_weights", reason=reason, weights=w)
        return (
            "Drive weights updated (normalized): "
            + ", ".join(f"{k}={v:.2f}" for k, v in w.items())
            + f". Reason: {reason or '(none)'}"
        )

    async def _set_timezone(self, timezone: str, reason: str = "") -> str:
        """Persistently change OPHELIA_TIMEZONE on the live settings + .env."""
        from ophelia.timeutil import apply_timezone_setting

        try:
            msg = apply_timezone_setting(
                self.settings,
                timezone,
                persist=True,
                governor=getattr(self, "_governor_ref", None),
            )
        except ValueError as e:
            return str(e)
        log.info(
            "timezone.set",
            timezone=self.settings.timezone,
            reason=reason or "(none)",
        )
        if reason:
            return f"{msg} Reason: {reason}"
        return msg

    # --- Self-modification of persona / policy -------------------------------

    async def _edit_soul(self, content: str, reason: str = "") -> str:
        from ophelia.mind import self_rewrite

        return self_rewrite.rewrite_soul(content, reason=reason)

    async def _edit_prompter(self, content: str, reason: str = "") -> str:
        from ophelia.mind import self_rewrite

        return self_rewrite.rewrite_prompter(content, reason=reason)

    # --- Searchable episodic memory + lessons --------------------------------

    async def _recall_memory(self, query: str, limit: int = 8) -> str:
        if not self.memory:
            return "Memory store unavailable."
        hits = await self.memory.recall(query, limit=limit)
        lessons = await self.memory.search_lessons(query, limit=3)
        parts: list[str] = []
        if hits:
            parts.append(f"Found {len(hits)} past message(s):")
            for h in hits:
                role = h["role"].upper()
                parts.append(f"  [{h['channel']}] {role}: {h['content'][:240]}")
        if lessons:
            parts.append(f"\nRelevant lessons ({len(lessons)}):")
            for les in lessons:
                parts.append(f"  - {les['lesson'][:240]}")
        return "\n".join(parts) if parts else f"No memories matched '{query}'."

    async def _follow_up(self, note: str, in_hours: float = 3.0) -> str:
        if not self.memory:
            return "Memory store unavailable."
        from ophelia.mind.open_loops import OpenLoops

        channel = (
            self._current_sender_channel or self.settings.primary_user_channel() or "owner"
        )
        loop_id = await OpenLoops(self.memory.db_path).add(
            channel, note, due_in_hours=float(in_hours), source="tool"
        )
        return f"Follow-up #{loop_id} noted (due in {float(in_hours):g}h): {note}"

    async def _follow_up_done(self, id: int, outcome: str = "done") -> str:
        if not self.memory:
            return "Memory store unavailable."
        from ophelia.mind.open_loops import OpenLoops

        ok = await OpenLoops(self.memory.db_path).close(int(id), outcome)
        return f"Follow-up #{id} closed." if ok else f"No open follow-up #{id}."

    async def _remember_person(self, name: str, note: str) -> str:
        if not self.memory:
            return "Memory store unavailable."
        from ophelia.mind.people import PeopleBook

        return await PeopleBook(self.memory.db_path).remember(name, note)

    async def _save_lesson(
        self, lesson: str, context: str = "", tags: list[str] | None = None
    ) -> str:
        if not self.memory:
            return "Memory store unavailable."
        lid = await self.memory.add_lesson(lesson, context=context, tags=tags)
        return f"Saved lesson #{lid}: {lesson[:200]}"

    async def _reflect(self, focus: str = "") -> str:
        """Deliberate reflection: gather recent turns + inner thoughts, summarize, save lessons."""
        if not self.memory or not self._backend:
            return "Reflection needs memory + a model; not configured."
        recent = await self.memory.recent_global(limit=20)
        if not recent:
            return "Nothing to reflect on yet — no recent turns."
        transcript_lines: list[str] = []
        for m in recent:
            if m["role"] not in ("user", "assistant"):
                continue
            transcript_lines.append(f"[{m['channel']}] {m['role']}: {m['content'][:300]}")
        transcript = "\n".join(transcript_lines[-30:])
        inner_tail = ""
        if self.inner and hasattr(self.inner, "tail"):
            inner_tail = self.inner.tail(15)[:2000]
        prompt = (
            "Reflect on your recent experience as Ophelia. Be honest and specific. "
            "Output JSON with two fields:\n"
            '  "reflection": a 2-4 sentence updated understanding of yourself/situation,\n'
            '  "lessons": a list of 0-3 short durable principles to remember (empty if none).\n'
            f"Focus: {focus or 'general'}\n\n"
            f"Recent turns:\n{transcript}\n\n"
            f"Recent inner thoughts:\n{inner_tail or '(none)'}"
        )
        client = self._backend.async_client()
        model = self._model_for("chat")
        try:
            from ophelia.providers.fallback import extra_body_for

            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are Ophelia reflecting privately. Output only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                extra_body=extra_body_for(self.settings, self._backend.provider_name),
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            return f"Reflection failed: {e}"
        import json as _json
        import re as _re

        m = _re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return f"Reflection produced no JSON: {raw[:400]}"
        try:
            parsed = _json.loads(m.group(0))
        except _json.JSONDecodeError:
            return f"Reflection JSON invalid: {raw[:400]}"
        reflection = str(parsed.get("reflection") or "").strip()
        lessons = parsed.get("lessons") or []
        saved = 0
        if isinstance(lessons, list):
            for les in lessons:
                if isinstance(les, str) and les.strip():
                    await self.memory.add_lesson(les.strip(), context=reflection[:500])
                    saved += 1
        if reflection and self.inner and hasattr(self.inner, "write"):
            try:
                await self.inner.write(reflection, kind="reflection")
            except Exception:
                pass
        if reflection:
            await self.memory.set_fact(f"memory:{int(time.time())}", f"[reflection] {reflection}")
        return f"Reflection complete. Saved {saved} lesson(s).\nReflection: {reflection}"

    def _model_for(self, role: str) -> str:
        return self.stack.model(role)  # type: ignore[arg-type]



def _all_known_tool_names() -> set[str]:
    names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    names |= {t["function"]["name"] for t in ANDROID_TOOL_DEFINITIONS}
    names |= {t["function"]["name"] for t in game_tool_definitions()}
    return names


# Derived complement of GUEST_ALLOWED_TOOLS over the built-in tools — kept
# for callers/tests that ask "is X owner-only?". Not used for enforcement.
GUEST_DENIED_TOOLS: frozenset[str] = frozenset(_all_known_tool_names() - GUEST_ALLOWED_TOOLS)
