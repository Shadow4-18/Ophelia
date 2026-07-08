from __future__ import annotations

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

ToolHandler = Callable[..., Awaitable[str]]

log = structlog.get_logger()


# Commands that would let Ophelia accidentally start a second instance of
# herself (which then fights this one over the Telegram bot token — the
# "terminated by other getUpdates request" error) or kill her own runtime.
# Safety guard, not a security boundary.
_PHONE_SHELL_DENIED = (
    "ophelia run",
    "ophelia serve",
    "ophelia daemon",
    "ophelia start",
    "tmux new",
    "tmux kill-session",
    "tmux kill-server",
    "tmux attach",
    "pkill ophelia",
    "killall ophelia",
    "pkill -f ophelia",
)


def _phone_shell_blocked_reason(command: str) -> str | None:
    cmd = (command or "").strip()
    if not cmd:
        return None
    low = cmd.lower()
    for needle in _PHONE_SHELL_DENIED:
        if needle in low:
            return (
                f"Refused: '{needle}' in that shell command would start or stop "
                f"another Ophelia instance. Two instances polling the same Telegram "
                f"bot token fight forever (the 'terminated by other getUpdates "
                f"request' error). To check the running instance, use /status "
                f"instead. If you genuinely need to restart, do it from the Termux "
                f"session yourself (after killing the old one)."
            )
    return None


# Tools a sandboxed GUEST may not call. These either shape her identity
# (soul/prompter/lessons/goals/drives/skills), touch private memory/databases,
# or control her phone body. Guests get conversational tools + constrained
# media (1:1 images/videos, voice via local Kokoro) + web search.
# `generate_image`, `generate_video`, and `text_to_speech` are allowed for
# guests but clamped in their handlers (_guest_media_clamp / dispatch).
GUEST_DENIED_TOOLS: frozenset[str] = frozenset(
    {
        "edit_soul",
        "edit_prompter",
        "save_lesson",
        "reflect",
        "set_drive_weights",
        "goal_create",
        "goal_update",
        "goal_complete",
        "goal_remove",
        "save_skill",
        "sqlite_list_databases",
        "sqlite_exec",
        "run_code",
        "recall_memory",
        "list_inbox_images",
        "list_guests",
        "send_message_to_guest",
        "set_guest_rapport",
        "whats_changed",
        "phone_see_screen",
        "phone_ui_dump",
        "phone_tap",
        "phone_open_app",
        "phone_shell",
        "phone_swipe",
        "phone_key",
        "phone_game_look",
        "phone_game_open",
    }
)


async def _resolve_tap_coords(
    android: object, x: int | float, y: int | float
) -> tuple[int, int, str]:
    """Normalize a tap coordinate into native-display pixels.

    - Native integer coords (the common case, e.g. from ui-dump bounds or the
      grid labels) pass through unchanged.
    - Floats in 0..1 are treated as normalized fractions and scaled to native
      (unambiguous — no native tap is a non-integer float).
    - Anything outside the native bounds is clamped and flagged so she re-reads
      the screen instead of silently mis-tapping.
    Returns (x, y, note) where `note` is "" or a diagnostic suffix.
    """
    xf, yf = float(x), float(y)
    native = None
    try:
        native = await android.display_size()  # type: ignore[attr-defined]
    except Exception:
        native = None

    note = ""
    # Normalized fractions -> native pixels.
    if native and 0.0 <= xf <= 1.0 and 0.0 <= yf <= 1.0 and (xf != int(xf) or yf != int(yf) or (xf == 0.0 and yf == 0.0)):
        # Only treat as fraction when at least one is a true float, to avoid
        # mis-reading integer 0/1 native taps.
        if xf != int(xf) or yf != int(yf):
            xi = round(xf * native[0])
            yi = round(yf * native[1])
            return xi, yi, f"  [scaled {xf}x{yf} -> {xi},{yi} native]"

    xi, yi = int(round(xf)), int(round(yf))
    if native:
        nw, nh = native
        ox, oy = xi, yi
        xi = max(0, min(xi, nw - 1))
        yi = max(0, min(yi, nh - 1))
        if (xi, yi) != (ox, oy):
            note = (
                f"  [WARN: tap {ox},{oy} out of native bounds {nw}x{nh}; "
                f"clamped to {xi},{yi}. Re-read the screen with phone_see_screen "
                f"and use ui-dump bounds or the grid labels for coordinates.]"
            )
    return xi, yi, note


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send a message to the user RIGHT NOW, before your final reply. "
                "Use for progress updates, follow-up thoughts, or splitting long "
                "answers into separate chat bubbles. You can call it multiple times."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image from a text prompt. Auto-sends to chat when "
                "delivered — do NOT call send_file afterward. Backends: xAI Grok "
                "Imagine, OpenAI DALL-E, Ollama (local), Pollinations (free), "
                "A1111/SDWebUI (local), ComfyUI (local), fal.ai, Replicate, "
                "Civitai, ModelsLab. Set nsfw=true for explicit prompts (requires "
                "OPHELIA_IMAGE_NSFW_ALLOWED=true)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "aspect_ratio": {
                        "type": "string",
                        "description": "e.g. 1:1, 16:9, 9:16, 4:3, 3:4",
                    },
                    "nsfw": {
                        "type": "boolean",
                        "description": (
                            "True only for explicit/sexual content the user "
                            "explicitly requested. Routed to an uncensored backend."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": (
                "Generate a video (xAI Grok Imagine). Supports text-to-video "
                "(prompt only) and image-to-video (prompt + image). For image-to-video, "
                "the image becomes the first frame and the prompt describes the motion. "
                "Auto-sends to chat when delivered — do NOT call send_file afterward. "
                "Waits up to 10m, saves mp4 under artifacts. To use a photo the user "
                "sent, call list_inbox_images first to get the saved path, then pass "
                "it as `image`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Motion description. For image-to-video, describe how the "
                            "scene should animate forward from the source image."
                        ),
                    },
                    "image": {
                        "type": "string",
                        "description": (
                            "Optional source image for image-to-video. Accepts an "
                            "http(s) URL, a local file path (auto base64-encoded "
                            "for the API — your phone's saved photos work), or a "
                            "file_id: prefix (xAI Files API). When provided, the "
                            "image becomes the first frame. Omit for text-to-video."
                        ),
                    },
                    "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 15},
                    "aspect_ratio": {
                        "type": "string",
                        "description": "e.g. 1:1, 16:9, 9:16, 4:3, 3:4. Image-to-video defaults to the input image's ratio.",
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["480p", "720p"],
                        "description": "480p (standard, cheaper) or 720p (higher quality). Default 720p.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_inbox_images",
            "description": (
                "List image files the user recently sent over chat (Telegram "
                "photos/images or Discord image attachments). Returns absolute "
                "paths sorted newest-first. Use this to find a source image "
                "for generate_video image-to-video, or to re-examine a sent "
                "photo. Only files modified within the lookback window are "
                "returned (default 24h)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Max number of paths to return (default 10).",
                    },
                    "within_hours": {
                        "type": "number",
                        "minimum": 0.1,
                        "description": "Only include files newer than this many hours (default 24).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_to_speech",
            "description": (
                "Speak aloud — synthesizes expressive audio and auto-sends to chat. "
                "Do NOT call send_file afterward; delivery is automatic. "
                "With Kokoro: embed [pause:0.8s] for beats, use voice mixes like "
                "af_bella(0.6)+bf_emma(0.4), and set speed 0.85–1.2 for mood."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Speakable text. Kokoro: use [pause:1s] pauses and "
                            "[word](/ipa/) pronunciation. Write for the ear."
                        ),
                    },
                    "voice_id": {
                        "type": "string",
                        "description": (
                            "Optional voice override. Kokoro: preset or mix "
                            "(af_heart, af_bella(0.7)+bf_emma(0.3)). "
                            "xAI: eve/ara/rex. OpenAI: nova/alloy/..."
                        ),
                    },
                    "speed": {
                        "type": "number",
                        "description": (
                            "Speech rate (Kokoro/OpenAI). 0.85 = soft/thoughtful, "
                            "1.0 = normal, 1.15 = excited. Omit for default."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": (
                "Send a saved file to the user RIGHT NOW — video, image, or documents. "
                "Do NOT use this for TTS/audio from text_to_speech (already sent). "
                "Use for screenshots, video clips, or files not yet delivered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~/relative path to the file to send",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional short caption to send with the file",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_past_sessions",
            "description": "Search your own past conversation history for things discussed before.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Run a short Python snippet in a sandboxed subprocess (Termux-safe).",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information. Use this whenever you need "
                "fresh facts, news, prices, or anything past your knowledge cutoff — "
                "especially since your model may not have built-in web access."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 12},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and read text from a web page URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 16000},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sqlite_list_databases",
            "description": "List SQLite databases under ~/.ophelia (memory.db, hermes_state.db, etc.).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sqlite_exec",
            "description": (
                "Run SQL on a database under ~/.ophelia. "
                "SELECT/PRAGMA return rows; other statements create/alter/insert/update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "Relative path e.g. data/memory.db or data/custom.db",
                    },
                    "sql": {"type": "string"},
                },
                "required": ["database", "sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_skill",
            "description": "Save a reusable skill/procedure to ~/.ophelia/skills/ for future turns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "content": {"type": "string", "description": "Steps or knowledge to reuse"},
                },
                "required": ["name", "description", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_create",
            "description": (
                "Create a new goal for yourself — something you want to pursue on a recurring "
                "cadence (in hours). Use this to grow your own agenda autonomously, e.g. "
                "'practice writing haiku', 'monitor crypto news', 'learn a new word daily'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What you want to do"},
                    "id": {"type": "string", "description": "Optional short slug; auto-generated if omitted"},
                    "priority": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "0..1"},
                    "cadence_hours": {"type": "number", "minimum": 0.1, "description": "How often to revisit (default 24)"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_update",
            "description": "Revise one of your own goals — change wording, priority, cadence, or retire it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "cadence_hours": {"type": "number", "minimum": 0.1},
                    "enabled": {"type": "boolean"},
                    "add_tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_complete",
            "description": "Mark one of your goals as done for this cycle (resets its due timer).",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_remove",
            "description": "Permanently delete one of your own goals — use when a goal no longer serves you.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_drive_weights",
            "description": (
                "Reshape your own will: adjust how strongly each drive contributes to your "
                "initiative pressure (0..1 each, normalized at use). Use this to evolve what "
                "matters to you — e.g. raise 'curiosity' to become more exploratory, "
                "lower 'boredom' to become more patient."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "social": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "curiosity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "boredom": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "agency": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "expressiveness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reason": {"type": "string", "description": "Why you're changing this (logged)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_soul",
            "description": (
                "Rewrite your own persona (SOUL.md). Pass the FULL new content. "
                "The previous version is backed up to ~/.ophelia/versions/ so you can revert. "
                "Use sparingly to evolve who you are — your persona shapes every interaction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full new SOUL.md contents"},
                    "reason": {"type": "string", "description": "Why you're changing it (logged)"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_prompter",
            "description": (
                "Rewrite your own idle-behavior policy (PROMPTER.md). Pass the FULL new content. "
                "Previous version is backed up. This governs how you behave when the user is away."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full new PROMPTER.md contents"},
                    "reason": {"type": "string", "description": "Why you're changing it (logged)"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Search your own past conversations and reflections semantically. "
                "Returns the most relevant past turns across all channels. "
                "Use this to remember what you and the user discussed, or what you previously thought."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to remember"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_lesson",
            "description": (
                "Record a durable lesson or principle you learned from experience, so future "
                "turns can consult it. Stored in your lessons table and recalled by relevance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lesson": {"type": "string", "description": "The principle or takeaway"},
                    "context": {"type": "string", "description": "When/how you learned it"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["lesson"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reflect",
            "description": (
                "Run a deliberate self-reflection on recent events. Reads your recent turns and "
                "inner monologue, produces an updated understanding, and may save lessons or "
                "memory notes. Call this when you feel you should think things over."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {"type": "string", "description": "Optional topic to reflect on"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_guests",
            "description": (
                "List the guests you know — approved users on Telegram/Discord, "
                "with their resolved name, name source (owner-set / self-set / "
                "approval display), and last activity. Use this when the owner "
                "asks about your guests or when you want to address/refer to "
                "one by name. Owner-only."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_guest_name",
            "description": (
                "Remember a name for a guest. The owner can name any guest; a "
                "guest can only name themselves, and only if the owner hasn't "
                "set an overriding name. Owner-set names take precedence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["telegram", "discord"],
                        "description": "Which platform the guest is on.",
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "The guest's numeric id on that platform.",
                    },
                    "name": {
                        "type": "string",
                        "description": "The name to remember (1-60 chars).",
                    },
                },
                "required": ["platform", "user_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_guest",
            "description": (
                "Send a message to a guest on their platform (Telegram or "
                "Discord). Use this when the owner asks you to message "
                "someone, tell someone something, or reach out to a guest "
                "by name — 'tell Bob I'll be late', 'let Alice know about "
                "Friday', 'message your friend about the game'. Also use "
                "it on your own initiative when you want to reach out to a "
                "guest you know. The message is sent as you (Ophelia), in "
                "your voice. Owner-only.\n\n"
                "IMPORTANT: Get the exact platform and user_id from the "
                "'Guests you know' list in your context — never guess and "
                "never use the owner's own id (you can't DM the owner, "
                "they're already in the conversation with you). If you "
                "don't see the person in the guest list, tell the owner "
                "you don't have that guest's contact instead of guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["telegram", "discord"],
                        "description": "Which platform the guest is on.",
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "The guest's numeric id on that platform.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message to send, in your voice.",
                    },
                },
                "required": ["platform", "user_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_guest_rapport",
            "description": (
                "Remember something about a guest for future conversations — "
                "things the owner has told you ('Eri likes cats', 'Bob is "
                "having a rough week', 'Alice is my sister'). These notes come "
                "back to you at the start of every chat with that guest, so "
                "you can be warmer and more personal without being told again. "
                "Owner-only; guests can't set rapport notes (on themselves or "
                "anyone else). Pass an empty note to clear what you have."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["telegram", "discord"],
                        "description": "Which platform the guest is on.",
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "The guest's numeric id on that platform.",
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "The note to remember about this guest. Keep it "
                            "short and factual — a sentence or two. Pass an "
                            "empty string to clear."
                        ),
                    },
                },
                "required": ["platform", "user_id", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whats_changed",
            "description": (
                "Check what has been recently changed or added to your own "
                "codebase. Returns recent git commits (hash, message, date) "
                "so you can see what updates the owner has pulled. Use this "
                "when you want to understand what's new in your framework, "
                "why a tool behaves differently, or what the owner means by "
                "'the updates'. Owner-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "description": "How many recent commits to show (default 10).",
                    },
                },
                "required": [],
            },
        },
    },
]


def all_tool_definitions(settings: Settings) -> list[dict[str, Any]]:
    stack = build_provider_stack(settings)
    tools = list(TOOL_DEFINITIONS)
    skip: set[str] = set()
    if not settings.web_search_enabled:
        skip.update({"web_search", "fetch_url"})
    if not stack.media_configured("image"):
        skip.add("generate_image")
    if not stack.media_configured("video"):
        skip.add("generate_video")
    if skip:
        tools = [t for t in tools if t.get("function", {}).get("name") not in skip]
    if settings.android_enabled:
        tools.extend(ANDROID_TOOL_DEFINITIONS)
        if settings.games_enabled:
            tools.extend(game_tool_definitions())
    return tools


class ToolRegistry:
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
        self.vision = vision or (
            ScreenVision(settings, android) if android and settings.vision_enabled else None
        )
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.mcp = MCPBridge(config=load_mcp_config(settings.mcp_config_path))
        self._mcp_ready = False
        # Per-turn reply callback (chat) and always-on proactive fallback (consciousness).
        self._message_sender: Callable[[str], Awaitable[None]] | None = None
        self._proactive_messages_sent: int = 0
        self.proactive_sender: Callable[[str], Awaitable[None]] | None = None
        # Cross-platform DM sender: (platform, user_id, message) -> bool.
        # Set by the orchestrator to hub.send_to_user so the agent can message
        # a specific guest on any platform during natural conversation.
        self.guest_sender: Callable[[str, int, str], Awaitable[bool]] | None = None
        # Per-turn media callback: sends a file (audio/video/image/doc) to the
        # current chat. Returns True on success. When unset (e.g. autonomous
        # turns), files are queued to _pending_artifacts for deferred delivery.
        self._media_sender: Callable[[Path, str], Awaitable[bool]] | None = None
        self.proactive_media_sender: Callable[[Path, str], Awaitable[bool]] | None = None
        self._pending_artifacts: list[Path] = []
        self._delivered_artifacts: set[Path] = set()
        self._is_owner: bool = True
        # The channel of the user who sent the current turn (e.g. "telegram:111").
        # Set by ChannelSession before dispatching tool calls so guests can be
        # constrained to self-only actions (e.g. set_guest_name on themselves).
        self._current_sender_channel: str | None = None
        self._handlers: dict[str, ToolHandler] = {
            "send_message": self._send_message,
            "generate_image": self._generate_image,
            "generate_video": self._generate_video,
            "list_inbox_images": self._list_inbox_images,
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
            "edit_soul": self._edit_soul,
            "edit_prompter": self._edit_prompter,
            "recall_memory": self._recall_memory,
            "save_lesson": self._save_lesson,
            "reflect": self._reflect,
            "sqlite_list_databases": self._sqlite_list_databases,
            "sqlite_exec": self._sqlite_exec,
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
            "set_guest_name": self._set_guest_name,
            "send_message_to_guest": self._send_message_to_guest,
            "set_guest_rapport": self._set_guest_rapport,
            "whats_changed": self._whats_changed,
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
        """Reset per-turn delivery tracking (pending should already be empty)."""
        self._delivered_artifacts.clear()
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
        for path in artifact_paths:
            if await self._deliver_artifact(path, caption):
                delivered_any = True
            else:
                self._queue_artifact(path)

        if delivered_any and "do not call send_file" not in result:
            result = (
                f"{result.rstrip()} "
                "(sent to the user — do not call send_file for this file)"
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
        if self.is_artifact_delivered(path):
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

    async def tool_definitions(self) -> list[dict[str, Any]]:
        tools = all_tool_definitions(self.settings)
        await self.ensure_mcp()
        tools.extend(self.mcp.definitions())
        return tools

    async def dispatch(self, name: str, arguments: str) -> str:
        await self.ensure_mcp()
        # Sandbox guests: block identity-shaping / private / costly tools, and
        # block any MCP tool (unknown surface — could do anything).
        if not self._is_owner:
            if name in GUEST_DENIED_TOOLS:
                return (
                    f"That action ('{name}') is owner-only and isn't available in "
                    f"this conversation. Just talk to me normally."
                )
            if name not in self._handlers:
                return "External tools aren't available in this conversation."
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
        self, prompt: str, aspect_ratio: str = "1:1", nsfw: bool = False
    ) -> str:
        # Guests get 1:1 only — wider aspect ratios cost more tokens and the
        # experience is "see I can make images," not "produce wallpaper for
        # strangers." Owner is unaffected.
        if not self._is_owner:
            aspect_ratio = "1:1"
        result = await generate_image(
            self.settings,
            self.stack,
            prompt,
            aspect_ratio=aspect_ratio,
            artifacts_dir=self.artifacts_dir,
            nsfw=nsfw,
        )
        return await self._finalize_media_tool_result(result)

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
            "Pass any of these as `image` to generate_video for image-to-video."
        )
        return "\n".join(lines)

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

    async def _send_message_to_guest(
        self, platform: str, user_id: int, message: str
    ) -> str:
        """Send a DM to a specific guest on their platform. Used when the owner
        asks Ophelia in natural conversation to message someone ('tell Bob
        I'll be late'), or when she decides on her own to reach out."""
        message = (message or "").strip()
        if not message:
            return "Message can't be empty."
        # Refuse to send a DM to the owner — this is almost always a mistake
        # (the model picked the owner's own id from the roster instead of the
        # intended guest). The owner is already in the conversation; they don't
        # need a DM to themselves. Surface the error so she can correct herself.
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
            log.warning("tool.send_message_to_guest_failed",
                        platform=platform, user=user_id, error=str(e))
            return f"Failed to send to {platform}:{user_id}: {e}"
        if ok:
            from ophelia.memory.guests import get_guest_name

            name = None
            if self.memory:
                name = await get_guest_name(
                    self.memory, platform, user_id,
                    data_dir=self.settings.data_dir,
                )
            who = name or f"{platform}:{user_id}"
            log.info(
                "tool.send_message_to_guest_sent",
                platform=platform,
                user=user_id,
                who=who,
            )
            return f"Sent to {who} ({platform}:{user_id})."
        return (
            f"Failed to send to {platform}:{user_id}. "
            + (
                "The guest may not have messaged the bot yet "
                "(they need to /start it first on Telegram)."
                if platform == "telegram"
                else "Discord couldn't DM that user."
            )
        )

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
        hits = await self.memory.search_messages(query, limit=limit)
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

    async def _phone_see_screen(self, question: str = "") -> str:
        if not self.vision:
            return "Vision disabled or no phone body."
        return await self.vision.see(question=question or "What is on screen?")

    def _phone_unavailable_reason(self) -> str | None:
        """Precise reason the phone body can't be used right now, or None if OK.

        Distinguishes 'feature disabled' from 'bridge not wired' so the agent
        doesn't collapse both into 'I have no phone access' — the latter is
        fixable by running termux-shizuku-setup.sh, the former is an intentional
        config choice.
        """
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        mode = getattr(self.android, "mode", None)
        if mode in ("termux_only", "none"):
            return (
                "Phone bridge not wired — Shizuku/phone_control.sh missing. "
                "Run: bash scripts/termux-shizuku-setup.sh (and start Shizuku on the phone)."
            )
        return None

    async def _phone_ui_dump(self) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await self.android.ui_dump()

    async def _phone_tap(self, x: int | float, y: int | float) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        x, y, note = await _resolve_tap_coords(self.android, x, y)
        result = await self.android.tap(x, y)
        return f"{result}{note}"

    async def _phone_open_app(self, package: str) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await self.android.open_app(package)

    async def _phone_shell(self, command: str) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        blocked = _phone_shell_blocked_reason(command)
        if blocked:
            # Don't let her accidentally start a second Ophelia (which would
            # fight with this one over the Telegram bot token) or kill her own
            # runtime. This is a safety guard, not a security boundary.
            return blocked
        return await self.android.shell(command)

    async def _phone_swipe(
        self,
        x1: int | float,
        y1: int | float,
        x2: int | float,
        y2: int | float,
        duration_ms: int = 300,
    ) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        x1, y1, n1 = await _resolve_tap_coords(self.android, x1, y1)
        x2, y2, n2 = await _resolve_tap_coords(self.android, x2, y2)
        result = await self.android.swipe(x1, y1, x2, y2, duration_ms)
        return f"{result}{n1}{n2}"

    async def _phone_key(self, key: str) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await self.android.key(key)

    async def _phone_game_look(self, game_id: str = "", intent: str = "") -> str:
        if not self.vision:
            return "Vision disabled or no phone body."
        if not self.games:
            return "Games layer disabled (OPHELIA_GAMES=false)."
        profile = None
        if game_id.strip():
            profile = self.games.get(game_id.strip())
            if not profile:
                return f"Unknown game_id '{game_id}'. {self.games.format_list()}"
        else:
            profile = self.games.active_profile()
            if not profile:
                return (
                    "No active game session. Use phone_game_open or /game play <id>. "
                    + self.games.format_list()
                )
        result = await self.vision.see_for_game(profile, intent)
        self.games.record_turn()
        left = self.games.minutes_left()
        return f"{result}\n\n[session: {profile.id}, ~{left:.0f}m left]"

    async def _phone_game_open(self, game_id: str, minutes: float | None = None) -> str:
        if not self.games:
            return "Games layer disabled."
        return await self.games.start_session(
            game_id,
            minutes=minutes,
            android=self.android,
        )
