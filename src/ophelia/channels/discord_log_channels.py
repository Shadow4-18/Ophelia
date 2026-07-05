"""Discord log channel manager — auto-provision categories and per-conversation channels.

When DISCORD_GUILD_ID is set, Ophelia creates and maintains a logging layout in
that server:

  Main            — operational logs (activity, consciousness, inner thoughts, system)
  Telegram        — one text channel per Telegram chat
  DMs             — one text channel per Discord DM partner
  Discord Servers — one text channel per Discord server she is in

Channel IDs are persisted in ~/.ophelia/data/discord_log_channels.json so restarts
do not duplicate channels.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import structlog

from ophelia.config import OPHELIA_HOME, Settings

log = structlog.get_logger()

# Category display names in the logging guild.
CAT_MAIN = "Main"
CAT_TELEGRAM = "Telegram"
CAT_DM = "DMs"
CAT_SERVER = "Discord Servers"

_CAT_KEYS = {
    "main": CAT_MAIN,
    "telegram": CAT_TELEGRAM,
    "dm": CAT_DM,
    "server": CAT_SERVER,
}

# Fixed Main-category channels.
MAIN_CHANNELS: dict[str, str] = {
    "activity": "activity",
    "consciousness": "consciousness",
    "inner-thoughts": "inner-thoughts",
    "system": "system",
}

_STATE_PATH = OPHELIA_HOME / "data" / "discord_log_channels.json"


def _slug(text: str, *, max_len: int = 40) -> str:
    """Discord channel names: lowercase alphanumeric + hyphens."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "unknown")[:max_len]


def _channel_name(prefix: str, label: str, uid: str) -> str:
    base = f"{prefix}-{_slug(label)}-{uid}"
    return base[:100]


class DiscordLogChannels:
    """Create categories/channels and mirror chat logs into Discord."""

    def __init__(self, settings: Settings, *, state_path: Path | None = None) -> None:
        self.settings = settings
        self.state_path = state_path or _STATE_PATH
        self._state: dict[str, Any] | None = None
        self._lock = None  # asyncio.Lock, created lazily

    def enabled(self) -> bool:
        if not self.settings.discord_log_enabled:
            return False
        return bool(self.settings.discord_guild_id)

    def _lock_obj(self):
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state = raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._state = {}
        return self._state

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._load(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("discord_log.save_failed", error=str(e))

    def _categories(self) -> dict[str, int]:
        cats = self._load().get("categories")
        return cats if isinstance(cats, dict) else {}

    def _channels(self) -> dict[str, int]:
        chans = self._load().get("channels")
        return chans if isinstance(chans, dict) else {}

    def _set_category(self, key: str, channel_id: int) -> None:
        st = self._load()
        cats = st.setdefault("categories", {})
        cats[key] = channel_id
        self._save()

    def _set_channel(self, key: str, channel_id: int) -> None:
        st = self._load()
        chans = st.setdefault("channels", {})
        chans[key] = channel_id
        self._save()

    async def setup(self, bot) -> None:
        """Create categories and Main channels on bot ready."""
        if not self.enabled():
            return
        guild_id = int(self.settings.discord_guild_id)  # type: ignore[arg-type]
        guild = bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await bot.fetch_guild(guild_id)
            except Exception as e:
                log.warning(
                    "discord_log.guild_missing",
                    guild_id=guild_id,
                    error=str(e),
                    hint="Invite the bot to the logging server and set DISCORD_GUILD_ID",
                )
                return

        async with self._lock_obj():
            st = self._load()
            st["guild_id"] = guild_id
            self._save()

            cat_map = {
                "main": CAT_MAIN,
                "telegram": CAT_TELEGRAM,
                "dm": CAT_DM,
                "server": CAT_SERVER,
            }
            for key, name in cat_map.items():
                await self._ensure_category(guild, key, name)

            for key, name in MAIN_CHANNELS.items():
                await self._ensure_main_channel(guild, key, name)

            # Ensure a log channel exists for every server the bot is already in.
            for g in bot.guilds:
                if g.id == guild_id:
                    continue
                await self._ensure_server_channel(guild, g.id, g.name)

        log.info(
            "discord_log.setup_complete",
            guild=guild.name,
            categories=len(self._categories()),
            channels=len(self._channels()),
        )
        await self.log_system(f"Ophelia online — logging layout ready in **{guild.name}**.")

    async def on_guild_join(self, bot, joined_guild) -> None:
        """Create a per-server log channel when Ophelia joins a new Discord server."""
        if not self.enabled():
            return
        logging_guild_id = int(self.settings.discord_guild_id)  # type: ignore[arg-type]
        if joined_guild.id == logging_guild_id:
            return
        logging_guild = bot.get_guild(logging_guild_id)
        if logging_guild is None:
            return
        async with self._lock_obj():
            await self._ensure_server_channel(
                logging_guild, joined_guild.id, joined_guild.name
            )
        await self.log_system(
            f"Joined server **{joined_guild.name}** (`{joined_guild.id}`) — "
            f"log channel created."
        )

    async def _ensure_category(self, guild, key: str, name: str):
        import discord

        existing_id = self._categories().get(key)
        if existing_id:
            ch = guild.get_channel(int(existing_id))
            if ch is not None and isinstance(ch, discord.CategoryChannel):
                return ch

        # Reuse an existing category with the same name if present.
        for cat in guild.categories:
            if cat.name == name:
                self._set_category(key, cat.id)
                return cat

        try:
            cat = await guild.create_category(
                name,
                reason="Ophelia log channel auto-setup",
            )
            self._set_category(key, cat.id)
            log.info("discord_log.category_created", key=key, name=name, id=cat.id)
            return cat
        except discord.Forbidden:
            log.warning(
                "discord_log.category_forbidden",
                key=key,
                hint="Grant the bot Manage Channels in the logging server",
            )
        except Exception as e:
            log.warning("discord_log.category_failed", key=key, error=str(e))
        return None

    async def _ensure_text_channel(
        self,
        guild,
        *,
        map_key: str,
        name: str,
        category_key: str,
        topic: str = "",
    ):
        import discord

        existing_id = self._channels().get(map_key)
        if existing_id:
            ch = guild.get_channel(int(existing_id))
            if ch is not None and isinstance(ch, discord.TextChannel):
                return ch

        for ch in guild.text_channels:
            if ch.name == name:
                cat = ch.category
                if cat and cat.name == _CAT_KEYS.get(category_key):
                    self._set_channel(map_key, ch.id)
                    return ch

        cat_id = self._categories().get(category_key)
        category = guild.get_channel(int(cat_id)) if cat_id else None

        try:
            ch = await guild.create_text_channel(
                name,
                category=category,
                topic=topic[:1024] if topic else None,
                reason="Ophelia log channel auto-setup",
            )
            self._set_channel(map_key, ch.id)
            log.info("discord_log.channel_created", key=map_key, name=name, id=ch.id)
            return ch
        except discord.Forbidden:
            log.warning(
                "discord_log.channel_forbidden",
                key=map_key,
                hint="Grant the bot Manage Channels in the logging server",
            )
        except Exception as e:
            log.warning("discord_log.channel_failed", key=map_key, error=str(e))
        return None

    async def _ensure_main_channel(self, guild, key: str, name: str):
        topics = {
            "activity": "General activity and cross-platform events",
            "consciousness": "Spontaneous outreach and consciousness ticks",
            "inner-thoughts": "Inner monologue mirror",
            "system": "Startup, joins, and system events",
        }
        return await self._ensure_text_channel(
            guild,
            map_key=f"main:{key}",
            name=name,
            category_key="main",
            topic=topics.get(key, ""),
        )

    async def _ensure_telegram_channel(self, guild, channel_key: str, display_name: str):
        uid = channel_key.split(":", 1)[-1]
        name = _channel_name("tg", display_name, uid)
        return await self._ensure_text_channel(
            guild,
            map_key=f"telegram:{uid}",
            name=name,
            category_key="telegram",
            topic=f"Telegram chat log for {display_name} ({channel_key})",
        )

    async def _ensure_dm_channel(self, guild, user_id: str, display_name: str):
        name = _channel_name("dm", display_name, user_id)
        return await self._ensure_text_channel(
            guild,
            map_key=f"discord_dm:{user_id}",
            name=name,
            category_key="dm",
            topic=f"Discord DM log for {display_name} (discord:{user_id})",
        )

    async def _ensure_server_channel(self, guild, server_id: int, server_name: str):
        name = _channel_name("srv", server_name, str(server_id))
        return await self._ensure_text_channel(
            guild,
            map_key=f"discord_guild:{server_id}",
            name=name,
            category_key="server",
            topic=f"Discord server log for {server_name} ({server_id})",
        )

    async def _resolve_log_channel(self, bot, entry: dict[str, Any]):
        """Pick the Discord text channel for a chat-log entry."""
        if not self.enabled():
            return None

        guild_id = int(self.settings.discord_guild_id)  # type: ignore[arg-type]
        guild = bot.get_guild(guild_id)
        if guild is None:
            return None

        channel_key: str = entry.get("channel", "")
        ctx: dict = entry.get("log_context") or {}
        platform = ctx.get("platform") or (
            channel_key.split(":", 1)[0] if ":" in channel_key else ""
        )

        async with self._lock_obj():
            if platform == "telegram":
                display = ctx.get("display_name") or channel_key
                return await self._ensure_telegram_channel(guild, channel_key, display)

            if platform == "discord":
                if ctx.get("is_dm", True):
                    uid = channel_key.split(":", 1)[-1]
                    display = ctx.get("display_name") or uid
                    return await self._ensure_dm_channel(guild, uid, display)
                server_id = ctx.get("guild_id")
                server_name = ctx.get("guild_name") or str(server_id)
                if server_id:
                    return await self._ensure_server_channel(
                        guild, int(server_id), server_name
                    )

        return None

    async def _resolve_main_channel(self, bot, main_key: str):
        if not self.enabled():
            return None
        guild_id = int(self.settings.discord_guild_id)  # type: ignore[arg-type]
        guild = bot.get_guild(guild_id)
        if guild is None:
            return None
        async with self._lock_obj():
            return await self._ensure_main_channel(
                guild, main_key, MAIN_CHANNELS.get(main_key, main_key)
            )

    async def mirror_chat_entry(self, bot, entry: dict[str, Any]) -> None:
        """Post a chat-log row to the appropriate Discord channel."""
        ch = await self._resolve_log_channel(bot, entry)
        if ch is None:
            return
        await self._post_entry(ch, entry)

    async def log_consciousness(self, bot, text: str) -> None:
        ch = await self._resolve_main_channel(bot, "consciousness")
        if ch is None:
            return
        await self._post_plain(ch, "🌙 consciousness", text)

    async def log_inner_thought(self, bot, text: str) -> None:
        ch = await self._resolve_main_channel(bot, "inner-thoughts")
        if ch is None:
            return
        await self._post_plain(ch, "💭 inner", text)

    async def log_system(self, text: str, *, bot=None) -> None:
        if bot is None:
            return
        ch = await self._resolve_main_channel(bot, "system")
        if ch is None:
            return
        await self._post_plain(ch, "⚙️ system", text)

    async def log_activity(self, bot, text: str) -> None:
        ch = await self._resolve_main_channel(bot, "activity")
        if ch is None:
            return
        await self._post_plain(ch, "📋 activity", text)

    async def _post_plain(self, channel, title: str, text: str) -> None:
        import discord

        body = (text or "").strip()
        if not body:
            return
        stamp = time.strftime("%H:%M:%S", time.localtime())
        for i in range(0, len(body), 1900):
            chunk = body[i : i + 1900]
            embed = discord.Embed(description=chunk, color=0x7B68EE)
            if i == 0:
                embed.set_author(name=f"{title} · {stamp}")
            try:
                await channel.send(embed=embed)
            except Exception as e:
                log.warning("discord_log.post_failed", error=str(e))
                return

    async def _post_entry(self, channel, entry: dict[str, Any]) -> None:
        import discord

        direction = entry.get("direction", "?")
        text = (entry.get("text") or "").strip()
        media_path = entry.get("media_path")
        media_kind = entry.get("media_kind") or "media"
        is_owner = entry.get("is_owner", False)
        ctx = entry.get("log_context") or {}
        display = ctx.get("display_name") or entry.get("sender_id") or "unknown"

        if direction == "in":
            color = 0x3498DB
            arrow = "⬇️ in"
        else:
            color = 0x9B59B6
            arrow = "⬆️ out"

        owner_tag = " · owner" if is_owner else " · guest"
        stamp = time.strftime("%H:%M:%S", time.localtime())
        header = f"{arrow}{owner_tag} · {stamp}"

        # Server channels: show who spoke (many users share one log channel).
        if ctx.get("platform") == "discord" and not ctx.get("is_dm", True):
            header = f"{header} · **{display}**"

        try:
            if media_path:
                p = Path(str(media_path))
                if p.is_file():
                    cap = text[:1900] if text else None
                    await channel.send(
                        content=header,
                        file=discord.File(str(p), filename=p.name),
                        embed=discord.Embed(
                            description=cap or f"[{media_kind}]",
                            color=color,
                        )
                        if cap or media_kind
                        else None,
                    )
                    return

            if not text:
                return

            for i in range(0, len(text), 1900):
                chunk = text[i : i + 1900]
                embed = discord.Embed(description=chunk, color=color)
                if i == 0:
                    embed.set_author(name=header)
                await channel.send(embed=embed)
        except Exception as e:
            log.warning("discord_log.mirror_failed", error=str(e))
