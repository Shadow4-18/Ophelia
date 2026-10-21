"""Tool tiers — send the model only the tools a turn plausibly needs.

All 60-odd tool schemas are ~34 KB of JSON (≈8-9k tokens) on every chat
turn. Big cloud models shrug that off; a small local model (llama3.2 on a
PC, or anything on a phone CPU where prompt processing dominates latency)
picks tools worse and answers slower. So for local chat we send:

- the CORE group always (talk, search, recall, relay, …),
- other groups when the conversation mentions them (keywords below),
- any group she asked for with ``load_tools`` (sticky for a few turns),
- everything when resuming an unfinished tool chain.

``OPHELIA_TOOL_TIERS``: ``auto`` (default — only when the chat provider is
local Ollama), ``on``, or ``off`` (always send every tool).

Dispatch never consults tiers: a tool she remembers from history still runs
even if its schema wasn't sent this turn.
"""

from __future__ import annotations

import re
from typing import Any

CORE = "core"

TOOL_GROUPS: dict[str, frozenset[str]] = {
    CORE: frozenset(
        {
            "load_tools",
            "send_message",
            "react_to_message",
            "reply_to_message",
            "send_file",
            "web_search",
            "fetch_url",
            "recall_memory",
            "recall_past_sessions",
            "save_lesson",
            "reflect",
            "who_am_i_talking_to",
            "set_guest_name",
            "relay_to_owner",
            "run_code",
            "whats_changed",
            "evolve_self",
            "follow_up",
            "follow_up_done",
            "remember_person",
        }
    ),
    "media": frozenset(
        {
            "generate_image",
            "generate_video",
            "search_civitai_models",
            "text_to_speech",
            "list_inbox_images",
            "list_inbox_files",
        }
    ),
    "self": frozenset(
        {
            "edit_soul",
            "edit_prompter",
            "set_drive_weights",
            "set_timezone",
            "save_skill",
            "goal_create",
            "goal_update",
            "goal_complete",
            "goal_remove",
            "curiosity_trail_open",
            "curiosity_trail_deepen",
            "curiosity_trail_close",
        }
    ),
    "guests": frozenset(
        {"list_guests", "send_message_to_guest", "set_guest_rapport", "recall_guest_chat"}
    ),
    "data": frozenset({"sqlite_list_databases", "sqlite_exec"}),
    # site_* and phone_* are matched by prefix (see group_of).
    "site": frozenset(),
    "phone": frozenset(),
}

GROUP_DESCRIPTIONS: dict[str, str] = {
    "media": "make/send images, video, voice (TTS); browse photos/files people sent",
    "self": "edit your soul/prompter, drives, timezone, skills, goals, curiosity trails",
    "guests": "list guests, DM a guest, rapport, read guest chats",
    "site": "your public website: pages, assets, files, preview, deploy",
    "data": "SQLite databases",
    "phone": "phone body: screen, tap, swipe, apps, games, battery, notifications, vibrate",
}

_TRIGGERS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "media": r"\b(image|picture|pic|pics|photo|selfie|draw|drawing|paint|art|artwork|"
        r"render|generate|video|clip|animate|gif|voice|speak|say it|sing|audio|"
        r"tts|sound|song|meme|wallpaper|lora|civitai|send me)\b",
        "self": r"\b(soul|prompter|personality|goal|goals|drive|drives|timezone|"
        r"time zone|skill|skills|habit|identity|curious|curiosity|rabbit hole)\b",
        "guests": r"\b(guest|guests|friend|friends|dm|dms|relay|rapport|stranger)\b",
        "site": r"\b(site|website|web ?page|page|pages|blog|wiki|deploy|html|css|"
        r"landing|cloudflare|post)\b",
        "data": r"\b(sqlite|database|db|sql|table)\b",
        "phone": r"\b(phone|screen|tap|swipe|app|apps|game|games|play|playing|"
        r"open|scroll|battery|notification|notifications|vibrate|buzz)\b",
    }.items()
}

STICKY_TURNS = 6


def group_of(tool_name: str) -> str:
    if tool_name.startswith("site_") or tool_name == "look_at_web_page":
        return "site"
    if tool_name.startswith("phone_"):
        return "phone"
    for name, members in TOOL_GROUPS.items():
        if tool_name in members:
            return name
    # Unknown/MCP tools ride with core so nothing silently disappears.
    return CORE


def triggered_groups(text: str) -> set[str]:
    return {name for name, rx in _TRIGGERS.items() if rx.search(text or "")}


def filter_definitions(
    tools: list[dict[str, Any]], groups: set[str]
) -> list[dict[str, Any]]:
    active = groups | {CORE}
    return [t for t in tools if group_of(t["function"]["name"]) in active]


def tiers_active(settings: Any, provider: str) -> bool:
    mode = (getattr(settings, "tool_tiers", "auto") or "auto").lower()
    if mode == "off":
        return False
    if mode == "on":
        return True
    p = (provider or "").lower()
    return p.startswith("ollama")


def load_tools_definition(available_groups: list[str]) -> dict[str, Any]:
    lines = "; ".join(
        f"{g} = {GROUP_DESCRIPTIONS[g]}" for g in available_groups if g in GROUP_DESCRIPTIONS
    )
    return {
        "type": "function",
        "function": {
            "name": "load_tools",
            "description": (
                "Load more of your tools for this conversation when you need "
                f"something not listed. Groups: {lines}. After loading, call "
                "the tool you needed in the same turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "array",
                        "items": {"type": "string", "enum": available_groups},
                        "description": "Tool groups to load",
                    }
                },
                "required": ["groups"],
            },
        },
    }


class ToolTierState:
    """Per-channel sticky groups (loaded via load_tools or triggered recently)."""

    def __init__(self) -> None:
        self._sticky: dict[str, dict[str, int]] = {}

    def begin_turn(self, channel: str, text: str) -> set[str]:
        sticky = self._sticky.setdefault(channel, {})
        for g in list(sticky):
            sticky[g] -= 1
            if sticky[g] <= 0:
                del sticky[g]
        for g in triggered_groups(text):
            sticky[g] = max(sticky.get(g, 0), STICKY_TURNS)
        return set(sticky)

    def load(self, channel: str, groups: list[str]) -> list[str]:
        sticky = self._sticky.setdefault(channel, {})
        loaded = [g for g in groups if g in TOOL_GROUPS and g != CORE]
        for g in loaded:
            sticky[g] = STICKY_TURNS
        return loaded

    def active(self, channel: str) -> set[str]:
        return set(self._sticky.get(channel, {}))
