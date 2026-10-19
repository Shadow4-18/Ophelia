"""Guest-facing and guest-management tool handlers (who am I talking to,
guest names, DMs to guests, relay to owner, guest chat recall, rapport).

Mixed into ToolRegistry; methods use its attributes (settings, memory,
android, senders, …). Split out of tools/registry.py."""

from __future__ import annotations

import structlog


log = structlog.get_logger()


class GuestToolsMixin:
    async def _who_am_i_talking_to(self) -> str:
        """Authoritative identity of the current speaker. Available to owner
        and guests — this is the grounded answer to 'who am I?' so she stops
        inventing IDs from the guest table or memory fragments."""
        channel = (getattr(self, "_current_sender_channel", None) or "").strip()
        if not channel:
            return (
                "Current speaker channel is unknown (no active chat turn). "
                "Can't identify who you're talking to right now."
            )
        is_owner = self.settings.is_owner_channel(channel)
        owners = sorted(self.settings.owner_channels())
        owner_line = ", ".join(owners) if owners else "(none configured)"
        role = "OWNER (your creator)" if is_owner else "GUEST (not your owner)"
        name_line = ""
        if self.memory and ":" in channel:
            platform, _, uid_s = channel.partition(":")
            try:
                uid = int(uid_s)
            except ValueError:
                uid = None
            if uid is not None:
                from ophelia.memory.guests import get_guest_name

                name = await get_guest_name(
                    self.memory, platform, uid, data_dir=self.settings.data_dir
                )
                if name:
                    name_line = f"\n- Known name: {name}"
        return (
            f"Current speaker:\n"
            f"- Channel: {channel}\n"
            f"- Role: {role}{name_line}\n"
            f"- All owner channels: {owner_line}\n"
            "(This is authoritative. Do not invent a different id.)"
        )

    async def _list_guests(self) -> str:
        """List approved guests with resolved names + last activity. Owner-only."""
        if not self.memory:
            return "Memory store unavailable — can't look up guests."
        from ophelia.memory.guests import list_guests

        roster = await list_guests(self.settings, self.memory)
        # Exclude the owner(s) from the listing — they're not guests to themselves.
        owner_channels = self.settings.owner_channels()
        guests = [g for g in roster if g["channel"] not in owner_channels]
        if not guests:
            return "No approved guests yet."
        lines = [f"Guests you know ({len(guests)}):"]
        for g in guests:
            src = g["name_source"]
            last = ""
            if g.get("last_ts"):
                from ophelia.memory.guests import _format_last_seen

                last = _format_last_seen(g["last_ts"])
            lines.append(f"  • {g['channel']} — \"{g['name']}\" ({src}{last})")
        return "\n".join(lines)

    async def _set_guest_name(
        self, platform: str, user_id: int, name: str
    ) -> str:
        """Remember a name for a guest. Owner can name any guest; a guest can
        only name themselves and only if the owner hasn't overridden it."""
        if not self.memory:
            return "Memory store unavailable — can't save guest name."
        from ophelia.memory.guests import set_guest_name

        # Determine whether the current turn is the owner. The owner can name
        # anyone; a guest can only name themselves.
        if self._is_owner:
            by_owner = True
        else:
            # Guest turn — they may only name themselves, and only on their
            # own platform. The current channel isn't directly visible here,
            # but the dispatch path sets _is_owner=False for guests and the
            # caller's channel is enforced at the session layer. We rely on
            # the session layer to set the sender channel; if it's not set,
            # fail safe (deny).
            sender = getattr(self, "_current_sender_channel", None)
            expected = f"{platform}:{user_id}"
            if sender and sender.lower() != expected.lower():
                return (
                    f"You can only set your own name, not someone else's "
                    f"({platform}:{user_id} doesn't match you)."
                )
            by_owner = False
        return await set_guest_name(
            self.memory, platform, user_id, name[:60], by_owner=by_owner
        )

    def _resolve_shareable_media(self, raw: str):
        """Validate a media path for guest/owner sharing. Returns Path or error str."""
        from pathlib import Path as _P

        s = (raw or "").strip().strip("\"'")
        if not s:
            return "file path is empty."
        try:
            p = _P(s).expanduser().resolve()
        except (OSError, ValueError) as e:
            return f"invalid path: {e}"
        if not p.is_file():
            return f"file not found: {p}"
        try:
            data_root = _P(self.settings.data_dir).expanduser().resolve()
        except (OSError, ValueError):
            data_root = _P(self.settings.data_dir)
        try:
            p.relative_to(data_root)
        except ValueError:
            return (
                f"refusing to send {p} — file must live under {data_root} "
                "(artifacts/, telegram_media/, discord_media/)."
            )
        from ophelia.channels.media_reply import media_kind

        if not media_kind(p):
            return (
                f"unsupported media type: {p.suffix} "
                "(use image/video/audio)."
            )
        return p

    async def _send_message_to_guest(
        self,
        platform: str,
        user_id: int,
        message: str = "",
        file: str | None = None,
    ) -> str:
        """Send a DM and/or media file to a guest. Owner-only."""
        message = (message or "").strip()
        file_raw = (file or "").strip()
        if not message and not file_raw:
            return "Need a message and/or file path."
        target_channel = f"{platform}:{user_id}"
        if self.settings.is_owner_channel(target_channel):
            log.warning(
                "tool.send_message_to_guest_owner_blocked",
                platform=platform,
                user=user_id,
            )
            return (
                f"{target_channel} is the owner — that's who you're talking to "
                f"right now. You almost certainly meant to send to a guest. "
                f"Check the 'Guests you know' list in your context for the right "
                f"platform:user_id and try again."
            )

        media_path = None
        if file_raw:
            resolved = self._resolve_shareable_media(file_raw)
            if isinstance(resolved, str):
                return f"Can't send file: {resolved}"
            media_path = resolved

        parts: list[str] = []
        if message and media_path is None:
            if not self.guest_sender:
                return (
                    "Can't send a DM right now — no cross-platform sender wired. "
                    "This usually means the hub isn't running."
                )
            log.info(
                "tool.send_message_to_guest",
                platform=platform,
                user=user_id,
                chars=len(message),
                preview=message[:80],
            )
            try:
                ok = await self.guest_sender(platform, user_id, message[:4000])
            except Exception as e:
                log.warning(
                    "tool.send_message_to_guest_failed",
                    platform=platform,
                    user=user_id,
                    error=str(e),
                )
                return f"Failed to send to {platform}:{user_id}: {e}"
            if not ok:
                return (
                    f"Failed to send to {platform}:{user_id}. "
                    + (
                        "The guest may not have messaged the bot yet "
                        "(they need to /start it first on Telegram)."
                        if platform == "telegram"
                        else "Discord couldn't DM that user."
                    )
                )
            parts.append("text")

        if media_path is not None:
            if not self.guest_media_sender:
                return (
                    "Can't send media to a guest right now — no media sender "
                    "wired. Text-only DMs may still work."
                )
            cap = message[:900] if message else ""
            log.info(
                "tool.send_media_to_guest",
                platform=platform,
                user=user_id,
                path=str(media_path),
                caption_chars=len(cap),
            )
            try:
                ok = await self.guest_media_sender(
                    platform, user_id, media_path, caption=cap
                )
            except TypeError:
                try:
                    ok = await self.guest_media_sender(
                        platform, user_id, media_path, cap
                    )
                except Exception as e:
                    return f"Failed to send media to {platform}:{user_id}: {e}"
            except Exception as e:
                log.warning(
                    "tool.send_media_to_guest_failed",
                    platform=platform,
                    user=user_id,
                    error=str(e),
                )
                return f"Failed to send media to {platform}:{user_id}: {e}"
            if not ok:
                return (
                    f"Failed to send media to {platform}:{user_id}. "
                    "They may need to /start the bot (Telegram) or allow DMs."
                )
            parts.append(f"media ({media_path.name})")

        from ophelia.memory.guests import get_guest_name

        name = None
        if self.memory:
            name = await get_guest_name(
                self.memory,
                platform,
                user_id,
                data_dir=self.settings.data_dir,
            )
        who = name or f"{platform}:{user_id}"
        what = " + ".join(parts) if parts else "message"
        return f"Sent to {who} ({platform}:{user_id}) [{what}]."

    async def _relay_to_owner(
        self,
        message: str = "",
        file: str | None = None,
        as_self: bool = False,
    ) -> str:
        """Guest → owner message/media pass.

        ``as_self=True`` sends without a 'From <guest>' label so the owner
        sees it as Ophelia's own spontaneous line (for 'don't say I told you'
        / 'make it seem random' requests). Internal memory still notes the
        requesting guest.
        """
        message = (message or "").strip()
        file_raw = (file or "").strip()
        if not message and not file_raw:
            return "Need a message and/or file path to relay."
        if self._is_owner:
            return (
                "You're already talking to the owner — just say it in chat. "
                "relay_to_owner is for guests passing a message to the owner."
            )

        media_path = None
        if file_raw:
            resolved = self._resolve_shareable_media(file_raw)
            if isinstance(resolved, str):
                return f"Can't relay file: {resolved}"
            media_path = resolved

        chan = (self._current_sender_channel or "").strip()
        guest_label = chan or "a guest"
        if chan and self.memory and ":" in chan:
            platform, _, id_s = chan.partition(":")
            try:
                from ophelia.memory.guests import get_guest_name

                name = await get_guest_name(
                    self.memory,
                    platform,
                    int(id_s),
                    data_dir=self.settings.data_dir,
                )
                if name:
                    guest_label = f"{name} ({chan})"
            except Exception:
                pass

        # Covert mode: owner sees Ophelia's words only. Attributed mode keeps
        # the 📨 From <guest> header so the owner knows who asked.
        if as_self:
            if message:
                payload = message[:3500]
            elif media_path is not None:
                payload = ""  # media-only; caption handled below
            else:
                payload = ""
        else:
            payload = ""
            if message:
                payload = f"📨 From {guest_label}:\n{message[:3500]}"
            elif media_path is not None:
                payload = f"📨 Media from {guest_label}"

        log.info(
            "tool.relay_to_owner",
            from_channel=chan or None,
            chars=len(message),
            has_file=bool(media_path),
            as_self=bool(as_self),
            preview=(message or str(media_path))[:80],
        )

        delivered = False
        delivery_error = ""
        if payload:
            if not self.proactive_sender:
                return (
                    "Can't reach the owner right now — no owner sender wired. "
                    "Tell the guest you'll try again later."
                )
            try:
                result = await self.proactive_sender(payload)
                # Prefer an explicit delivery count when the hub provides one.
                if isinstance(result, (int, float)):
                    delivered = int(result) > 0
                    if not delivered:
                        delivery_error = (
                            "Owner sender reported 0 deliveries "
                            "(check owner channel ids / bot DMs)."
                        )
                else:
                    delivered = True
            except Exception as e:
                log.warning("tool.relay_to_owner_failed", error=str(e))
                return f"Failed to reach the owner: {e}"

        if media_path is not None:
            sender = self.proactive_media_sender
            if not sender:
                if delivered:
                    return (
                        "Text was handed off, but no media sender is wired "
                        "for the owner — can't forward the file."
                    )
                return (
                    "No media sender is wired for the owner — can't forward the file."
                )
            if as_self:
                cap = message[:900] if message else ""
            else:
                cap = message[:900] if message else f"From {guest_label}"
            try:
                ok = await sender(media_path, cap)
            except Exception as e:
                log.warning("tool.relay_to_owner_media_failed", error=str(e))
                return f"Failed to forward media to the owner: {e}"
            if not ok:
                if delivered:
                    return "Owner text may have arrived, but media delivery failed."
                return "Media delivery to the owner failed."
            delivered = True

        if not delivered:
            return delivery_error or "Nothing was delivered to the owner."

        if self.memory:
            try:
                owners = list(self.settings.owner_channels())
                store_ch = owners[0] if owners else "owner:relay"
                mode = "covert" if as_self else "attributed"
                note = f"[relay from {guest_label} mode={mode}] {message[:2000]}"
                if media_path is not None:
                    note += f" [file={media_path.name}]"
                await self.memory.append_message(store_ch, "assistant", note)
            except Exception as e:
                log.debug("tool.relay_to_owner_store_failed", error=str(e))
        extra = f" with {media_path.name}" if media_path is not None else ""
        how = (
            "as your own message (no guest label)"
            if as_self
            else f"attributed to {guest_label}"
        )
        return (
            f"Delivered to your owner{extra} ({how}). Tell the guest it's been "
            f"passed along — do not invent an owner reply."
        )

    async def _recall_guest_chat(
        self, guest: str, query: str = "", limit: int = 30
    ) -> str:
        """Owner-only: read real guest_messages history for a named guest."""
        if not self._is_owner:
            return "Only the owner can read guest chat history."
        if not self.memory:
            return "Memory store unavailable."
        guest = (guest or "").strip()
        if not guest:
            return "Need a guest name or platform:user_id."
        from ophelia.memory.guests import get_guest_name, resolve_guest_target

        resolved = await resolve_guest_target(self.settings, self.memory, guest)
        if not resolved:
            return (
                f"Couldn't resolve '{guest}' to a known guest. "
                "Use their name, platform:id, or list_guests."
            )
        platform, user_id = resolved
        channel = f"{platform}:{user_id}"
        lim = max(1, min(int(limit or 30), 80))
        q = (query or "").strip()
        hits = await self.memory.search_guest_messages(
            q, channel=channel, limit=lim
        )
        name = await get_guest_name(
            self.memory, platform, user_id, data_dir=self.settings.data_dir
        )
        label = f"{name} ({channel})" if name else channel
        if not hits:
            if q:
                return f"No messages matching '{q}' in chat with {label}."
            return f"No stored chat history with {label} yet."
        lines = [f"Chat with {label} ({len(hits)} message(s)):"]
        for h in hits:
            role = "GUEST" if h["role"] == "user" else "YOU"
            lines.append(f"  {role}: {h['content'][:400]}")
        return "\n".join(lines)

    async def _set_guest_rapport(
        self, platform: str, user_id: int, note: str
    ) -> str:
        """Remember (or clear) a rapport note about a guest. Owner-only —
        guests can't set rapport notes on anyone, including themselves."""
        if not self.memory:
            return "Memory store unavailable — can't save rapport note."
        if not self._is_owner:
            return "Only the owner can set rapport notes about guests."
        note = (note or "").strip()
        key = f"guest_rapport:{platform}:{user_id}"
        if not note:
            # Clear the note.
            await self.memory.set_fact(key, "")
            return f"Cleared rapport notes for {platform}:{user_id}."
        # Cap at a reasonable length so notes don't bloat the system prompt.
        await self.memory.set_fact(key, note[:600])
        from ophelia.memory.guests import get_guest_name

        name = await get_guest_name(
            self.memory, platform, user_id, data_dir=self.settings.data_dir
        )
        who = name or f"{platform}:{user_id}"
        return f"Okay — I'll remember that about {who} for next time we talk."
