"""Persistent guest names + roster.

Two fact keys per guest, with precedence:
  - guest_name_owner:<channel>   — set by the owner (highest precedence).
  - guest_name_self:<channel>    — set by the guest themselves.

Owner override wins. If only a self-name exists, that's used. If neither
exists, fall back to the display name captured at approval time (from
pending_guests.json), then to the raw channel string.

The owner can always override a guest's self-chosen name by setting their
own; the guest's self-name stays on file (not deleted) so re-enabling it
later is a one-line change.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

from ophelia.config import Settings
from ophelia.memory.store import MemoryStore

log = structlog.get_logger()

_OWNER_PREFIX = "guest_name_owner:"
_SELF_PREFIX = "guest_name_self:"


def _channel(platform: str, user_id: int | str) -> str:
    return f"{platform}:{user_id}"


async def get_guest_name(
    memory: MemoryStore,
    platform: str,
    user_id: int | str,
    *,
    data_dir: Path | None = None,
) -> str | None:
    """Resolved name with precedence: owner-set > self-set > approval display name.

    Returns None if no name is known at all. `data_dir` (when provided) is used
    to locate pending_guests.json for the approval-display-name fallback; if
    omitted, only owner-set and self-set names are considered.
    """
    chan = _channel(platform, user_id)
    owner_name = await memory.get_fact(f"{_OWNER_PREFIX}{chan}")
    if owner_name:
        return owner_name
    self_name = await memory.get_fact(f"{_SELF_PREFIX}{chan}")
    if self_name:
        return self_name
    if data_dir is not None:
        display = _approval_display_name(platform, int(user_id), data_dir)
        return display
    return None


async def set_guest_name(
    memory: MemoryStore,
    platform: str,
    user_id: int | str,
    name: str,
    *,
    by_owner: bool,
) -> str:
    """Set a guest name. Owner sets win; a guest can only set their own
    self-name and only if the owner hasn't overridden it.

    Returns a short human-readable confirmation of what happened.
    """
    chan = _channel(platform, user_id)
    name = name.strip()
    if not name:
        return "Name can't be empty."

    if by_owner:
        await memory.set_fact(f"{_OWNER_PREFIX}{chan}", name)
        return f"Okay — I'll call {chan} '{name}' (owner set)."

    # Guest setting their own name.
    owner_set = await memory.get_fact(f"{_OWNER_PREFIX}{chan}")
    if owner_set:
        return (
            f"The owner has already named you '{owner_set}'. "
            "Ask them to change it if you'd prefer a different name."
        )
    await memory.set_fact(f"{_SELF_PREFIX}{chan}", name)
    return f"Got it — I'll call you '{name}'."


async def list_guests(
    settings: Settings, memory: MemoryStore
) -> list[dict[str, Any]]:
    """Return the full guest roster with resolved names + approval metadata.

    Each entry: {platform, user_id, channel, name, name_source, status,
    first_message, last_ts}.
    """
    out: list[dict[str, Any]] = []
    approvals = _load_approvals(settings.data_dir / "pending_guests.json")
    # Index approvals by channel for quick lookup.
    by_channel: dict[str, dict[str, Any]] = {}
    for key, rec in approvals.items():
        if ":" in key:
            by_channel[key] = rec

    # Build the roster from the allowlists (not just pending_guests) — these
    # are the users who can actually talk to her right now.
    seen: set[str] = set()
    for uid in _ordered_telegram(settings):
        chan = f"telegram:{uid}"
        seen.add(chan)
        out.append(await _entry(memory, "telegram", uid, chan, by_channel.get(chan)))
    for uid in _ordered_discord(settings):
        chan = f"discord:{uid}"
        if chan in seen:
            continue
        seen.add(chan)
        out.append(await _entry(memory, "discord", uid, chan, by_channel.get(chan)))
    return out


async def _entry(
    memory: MemoryStore,
    platform: str,
    user_id: int,
    chan: str,
    approval_rec: dict[str, Any] | None,
) -> dict[str, Any]:
    owner_name = await memory.get_fact(f"{_OWNER_PREFIX}{chan}")
    self_name = await memory.get_fact(f"{_SELF_PREFIX}{chan}")
    if owner_name:
        name, source = owner_name, "owner"
    elif self_name:
        name, source = self_name, "self"
    elif approval_rec and approval_rec.get("display_name"):
        name, source = approval_rec["display_name"], "approval"
    else:
        name, source = chan, "channel"
    return {
        "platform": platform,
        "user_id": user_id,
        "channel": chan,
        "name": name,
        "name_source": source,
        "status": (approval_rec or {}).get("status", "approved"),
        "first_message": (approval_rec or {}).get("first_message", ""),
        "last_ts": (approval_rec or {}).get("ts"),
    }


def _ordered_telegram(settings: Settings) -> list[int]:
    return settings._allowed_telegram_users_ordered()


def _ordered_discord(settings: Settings) -> list[int]:
    return settings._allowed_discord_users_ordered()


def _approval_display_name(
    platform: str, user_id: int, data_dir: Path
) -> str | None:
    rec = _load_approvals(data_dir / "pending_guests.json").get(f"{platform}:{user_id}")
    if not rec:
        return None
    return rec.get("display_name") or None


def _load_approvals(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


async def resolve_guest_target(
    settings: Settings, memory: MemoryStore, query: str
) -> tuple[str, int] | None:
    """Resolve a free-form guest reference to (platform, user_id).

    Tries in order:
    1. Exact 'platform:id' match (e.g. 'telegram:111').
    2. Bare numeric id — binds to the first enabled platform that has it.
    3. Name match against the full roster: owner-set names (memory store),
       self-set names (memory store), and approval display names
       (pending_guests.json). Case-insensitive, must be unambiguous.

    Returns None if nothing matches unambiguously.
    """
    q = query.strip()
    if not q:
        return None
    # Exact channel form
    if ":" in q:
        platform, _, id_s = q.partition(":")
        try:
            return platform.lower(), int(id_s)
        except ValueError:
            return None
    # Bare numeric — bind to first enabled platform that has this id
    if q.isdigit():
        uid = int(q)
        if settings.telegram_enabled and uid in (settings._allowed_telegram_users_ordered()):
            return "telegram", uid
        if settings.discord_enabled and uid in (settings._allowed_discord_users_ordered()):
            return "discord", uid
        return None
    # Name match — build the full roster (with resolved names from memory)
    # and match case-insensitively. Must be unambiguous.
    roster = await list_guests(settings, memory)
    q_lower = q.lower()
    matches: list[tuple[str, int]] = []
    for g in roster:
        name = (g.get("name") or "").lower()
        if name and name == q_lower:
            matches.append((g["platform"], g["user_id"]))
    if len(matches) == 1:
        return matches[0]
    return None


def guests_context_block(
    roster: list[dict[str, Any]],
    *,
    owner_channel: str,
    activity: dict[str, list[dict]] | None = None,
    owner_channels: set[str] | None = None,
) -> str:
    """Format the roster for the owner's system prompt.

    The owner themselves is excluded from the list (they know who they are).
    Includes name + name_source + last activity so she has social context.
    The platform:user_id is shown so the agent can call send_message_to_guest
    without needing to look it up first.

    If `activity` is provided (channel -> recent messages), includes a short
    digest of what she's talked about with each guest recently — the bridge
    that lets her say 'oh, Eri was telling me about X yesterday' to the owner.

    `owner_channels` (if given) is the full set of owner channels across all
    platforms — used to exclude the owner's entries on every platform, not
    just the one they're messaging from. Without this, an owner who is first
    on both Telegram and Discord would see their own Discord entry listed as
    a 'guest' and might accidentally send a message to themselves.
    """
    exclude = {owner_channel.lower()} if owner_channel else set()
    if owner_channels:
        exclude |= {c.lower() for c in owner_channels}
    others = [g for g in roster if g["channel"].lower() not in exclude]
    if not others:
        return ""
    lines = [
        "# Guests you know",
        "(To message a guest, call send_message_to_guest with their platform "
        "and user_id — the exact platform:user_id shown below. The owner may "
        "ask you to 'tell' or 'message' someone by name; match the name to "
        "the entry below and send to THAT platform:user_id, never to the "
        "owner's own id. You can also reach out on your own when it feels right.)",
    ]
    activity = activity or {}
    for g in others:
        name = g["name"]
        src = g["name_source"]
        last = _format_last_seen(g.get("last_ts"))
        lines.append(f"- {g['channel']} — \"{name}\" ({src}{last})")
        msgs = activity.get(g["channel"])
        if msgs:
            digest = _activity_digest(msgs)
            if digest:
                lines.append(f"    recently with them: {digest}")
    return "\n".join(lines) + "\n"


def _activity_digest(msgs: list[dict]) -> str:
    """Compress a few recent guest messages into a one-line topic digest.

    Keeps it short (the system prompt shouldn't be dominated by guest history)
    but enough that she knows the gist — e.g. 'they asked about cats, you sent
    a picture'. Skips very long messages and trims to ~120 chars total.
    """
    snippets: list[str] = []
    for m in msgs[-4:]:
        content = (m.get("content") or "").strip().replace("\n", " ")
        if not content or len(content) > 200:
            continue
        role = m.get("role", "")
        if role == "user":
            snippets.append(f"they said: {content[:80]}")
        elif role == "assistant":
            snippets.append(f"you said: {content[:80]}")
    if not snippets:
        return ""
    digest = "; ".join(snippets)
    if len(digest) > 160:
        digest = digest[:157] + "..."
    return digest


def _format_last_seen(ts: float | None) -> str:
    if not ts:
        return ""
    delta = time.time() - ts
    if delta < 60:
        ago = "just now"
    elif delta < 3600:
        ago = f"{int(delta // 60)}m ago"
    elif delta < 86400:
        ago = f"{int(delta // 3600)}h ago"
    else:
        ago = f"{int(delta // 86400)}d ago"
    return f", last activity {ago}"
