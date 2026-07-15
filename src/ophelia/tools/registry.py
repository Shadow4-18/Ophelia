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
        "set_timezone",
        "goal_create",
        "goal_update",
        "goal_complete",
        "goal_remove",
        "save_skill",
        "sqlite_list_databases",
        "sqlite_exec",
        "site_status",
        "site_list_pages",
        "site_get_page",
        "site_upsert_page",
        "site_delete_page",
        "site_set_meta",
        "site_import_pages",
        "site_add_asset",
        "site_export_static",
        "site_deploy",
        "site_write_file",
        "site_read_file",
        "site_list_files",
        "site_delete_file",
        "run_code",
        "recall_memory",
        "list_inbox_images",
        "list_inbox_files",
        "list_guests",
        "send_message_to_guest",
        "set_guest_rapport",
        "recall_guest_chat",
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
                "Generate an image. Auto-sends to chat — do NOT call send_file after. "
                "Backends: xAI Grok Imagine, OpenAI DALL-E, Ollama, Pollinations, "
                "A1111, ComfyUI, fal, Replicate, Civitai, ModelsLab. "
                "Set nsfw=true ONLY for explicit/sexual content the user asked for — "
                "that routes to the uncensored backend (Civitai when configured). "
                "\n\nCivitai (NSFW / SDXL / Illustrious / Pony / LoRAs):\n"
                "- Illustrious and Pony are BOTH SDXL-based (not SD1.5). "
                "Only pair LoRAs with the same family (Illustrious↔Illustrious, "
                "Pony↔Pony).\n"
                "- Auto-pick selects a curated checkpoint from style "
                "(anime/vtuber→Illustrious, pony→Pony V6, photo→SDXL). It does "
                "NOT add random LoRAs or inject character trigger words.\n"
                "- Pass model=<checkpoint AIR or alias 'pony'|'illustrious'> to "
                "pin. Pass loras= as JSON {\"urn:air:...\": 0.8} for LoRAs — "
                "never put a LoRA AIR in model= (it will be moved to loras "
                "automatically).\n"
                "- Write the prompt yourself (danbooru tags for Illustrious/Pony). "
                "Include short LoRA triggers in the prompt only when you use that LoRA.\n"
                "- txt2img: prompt only. img2img: image=<path or URL> from "
                "list_inbox_images + optional strength (0.6–0.8).\n"
                "- Never pass a Civitai AIR URN as model when using xAI/OpenAI."
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
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional pin: checkpoint AIR URN from "
                            "search_civitai_models. Omit to let Ophelia pick "
                            "per image (recommended). Civitai only."
                        ),
                    },
                    "loras": {
                        "type": "string",
                        "description": (
                            "Optional LoRAs for Civitai. JSON object "
                            '{"urn:air:...": 0.8} or comma list urn|0.8,urn|0.7. '
                            "Omit for checkpoint-only (recommended). Must match "
                            "checkpoint family (no Pony LoRA on generic SDXL)."
                        ),
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": (
                            "Civitai/SD negative prompt. Optional — SD1/SDXL get "
                            "a solid default if omitted. Leave empty for Flux."
                        ),
                    },
                    "image": {
                        "type": "string",
                        "description": (
                            "Optional source image for img2img (Civitai createVariant). "
                            "http(s) URL or local path from list_inbox_images. "
                            "Omit for text-to-image."
                        ),
                    },
                    "strength": {
                        "type": "number",
                        "description": (
                            "img2img denoise strength 0–1 (default 0.7). "
                            "Lower = closer to source; 0.6–0.8 typical."
                        ),
                    },
                    "auto_pick": {
                        "type": "boolean",
                        "description": (
                            "Default true on Civitai when model is omitted. "
                            "Set false only if you passed an explicit model pin."
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
            "name": "search_civitai_models",
            "description": (
                "Browse Civitai checkpoints/LoRAs when you want to pin a specific "
                "model. generate_image auto-pick is checkpoint-only (curated, no "
                "random LoRAs). Use this to find a character LoRA AIR, then pass "
                "model=/loras= on generate_image. Check baseModel compatibility "
                "and include short trigger_words in your own prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to find — e.g. 'illustrious anime', "
                            "'realistic portrait sdxl', 'character name lora'."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": ["Checkpoint", "LORA"],
                        "description": "Checkpoint (base model) or LORA. Default Checkpoint.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1–10, default 5).",
                    },
                    "nsfw": {
                        "type": "boolean",
                        "description": "Include NSFW-tagged models (default true).",
                    },
                },
                "required": ["query"],
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
                "returned (default 24h). For videos/zips use list_inbox_files."
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
            "name": "list_inbox_files",
            "description": (
                "List recent files the user sent over chat — images, videos, "
                "zips, and other saved attachments under telegram_media / "
                "discord_media. Returns absolute paths newest-first. Use this "
                "when they sent a video or zip for your website "
                "(then site_add_asset with that path)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["all", "image", "video", "file"],
                        "description": "Filter by type (default all).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                    },
                    "within_hours": {
                        "type": "number",
                        "minimum": 0.1,
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
                "With Kokoro: embed [pause:0.8s] for beats, leave voice_id empty "
                "(uses default) or pass a single preset / baked mix name, and set "
                "speed 0.85–1.2 for mood. Never pass raw formulas like "
                "af_bella(0.6)+bf_emma(0.4) — they sound muffled/peaky."
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
                            "Optional voice override. Kokoro: single preset "
                            "(af_heart, af_bella, bf_emma) or baked ophelia_mix_… "
                            "name. Do not pass af_x(0.7)+bf_y(0.3) formulas. "
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
            "name": "site_status",
            "description": (
                "Status of YOUR public wiki/blog (page counts, title, local URL). "
                "You fully own this site — publish lore, mythos, essays, and notes here."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_list_pages",
            "description": (
                "List pages on your public site. Drafts included unless published_only=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["wiki", "blog", "page"],
                        "description": "Filter by kind",
                    },
                    "published_only": {"type": "boolean"},
                    "tag": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_get_page",
            "description": "Read one site page by slug (includes unpublished drafts).",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_upsert_page",
            "description": (
                "Create or update a structured wiki/blog page. "
                "body_format=markdown (default) or html for raw HTML in the body. "
                "For a fully custom site (own layouts/CSS/JS), prefer site_write_file "
                "under www/ (e.g. index.html, css/main.css, js/app.js)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body_md": {
                        "type": "string",
                        "description": "Page body (Markdown or HTML depending on body_format)",
                    },
                    "slug": {
                        "type": "string",
                        "description": "URL slug (optional; auto from title)",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["wiki", "blog", "page"],
                    },
                    "summary": {"type": "string"},
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                    },
                    "published": {
                        "type": "boolean",
                        "description": "If true, visible on the public site",
                    },
                    "featured": {"type": "boolean"},
                    "body_format": {
                        "type": "string",
                        "enum": ["markdown", "html"],
                        "description": "markdown (default) or html for full HTML body",
                    },
                },
                "required": ["title", "body_md"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_delete_page",
            "description": "Permanently delete a page from your public site by slug.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_set_meta",
            "description": (
                "Set site-wide branding: site_title, tagline, author, footer, "
                "custom_head (raw HTML for <head>), home_slug (make that published "
                "page the landing at / — e.g. home_slug=about). "
                "For a fully custom home, prefer site_write_file path=index.html."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_title": {"type": "string"},
                    "tagline": {"type": "string"},
                    "author": {"type": "string"},
                    "footer": {"type": "string"},
                    "custom_head": {
                        "type": "string",
                        "description": "Raw HTML injected into <head> (extra CSS/JS links, meta, etc.)",
                    },
                    "home_slug": {
                        "type": "string",
                        "description": (
                            "Slug of a published page to use as / "
                            "(e.g. 'about'). Empty clears back to wiki listing home. "
                            "Ignored if www/index.html exists."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_write_file",
            "description": (
                "Write a file into YOUR freeform site tree (~/.ophelia/site/www/). "
                "Full HTML, CSS, and JS allowed — e.g. index.html, css/style.css, js/app.js, "
                "theme.css / theme.js (theme.* also restyles the built-in wiki chrome). "
                "www/index.html replaces the default home page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under www/, e.g. index.html or css/main.css",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents (UTF-8 text)",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_read_file",
            "description": "Read a file from your www/ site tree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_list_files",
            "description": "List files under www/ (optional subdirectory prefix).",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Optional subdirectory, e.g. css or js",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_delete_file",
            "description": "Delete a file from your www/ site tree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_import_pages",
            "description": (
                "Bulk-import pages onto your public site from a JSON array. "
                "Each item: title, body_md (or body/content), optional slug/kind/summary/tags/published. "
                "Use this to migrate lore from a private sqlite wiki you already built."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pages_json": {
                        "type": "string",
                        "description": "JSON array of page objects",
                    },
                },
                "required": ["pages_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_add_asset",
            "description": (
                "Copy an image/video/file into the public site assets folder. "
                "Returns a /assets/... URL you can embed in HTML/Markdown "
                "(e.g. <video src=\"/assets/clip.mp4\">). "
                "For files the user just sent, use the path from "
                "'[User sent a video/file — saved to …]' or list_inbox_files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local file path (e.g. under ~/.ophelia/data/telegram_media)",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional public filename",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_export_static",
            "description": (
                "Export published pages as static HTML under ~/.ophelia/site/export/ "
                "for GitHub Pages / Cloudflare Pages / any static host. "
                "Prefer site_deploy when Cloudflare credentials are configured — "
                "that exports AND uploads to your live domain."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_deploy",
            "description": (
                "Export published pages and upload them to Cloudflare Pages so your "
                "custom domain goes live / updates. Requires CLOUDFLARE_API_TOKEN, "
                "CLOUDFLARE_ACCOUNT_ID, and OPHELIA_SITE_CF_PROJECT in ~/.ophelia/.env. "
                "Call this after publishing or editing pages when you want the public "
                "site to match. Narrating 'deployed' without this tool does nothing."
            ),
            "parameters": {"type": "object", "properties": {}},
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
            "name": "set_timezone",
            "description": (
                "Permanently change your authoritative clock timezone (OPHELIA_TIMEZONE). "
                "Use when the owner asks you to switch zones (EST, America/Chicago, etc.) "
                "or to follow the host machine's local time. Verbal agreement or memory "
                "facts alone will NOT stick — the Current context block will keep showing "
                "the old zone until you call this. Accepts IANA names, common abbrevs "
                "(EST/PST/…), fixed offsets like UTC-5, or 'system'/'local' for host time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": (
                            "Target timezone: America/New_York, EST, UTC-5, or system"
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you're changing it (logged)",
                    },
                },
                "required": ["timezone"],
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
            "name": "who_am_i_talking_to",
            "description": (
                "Return the authoritative identity of the person you're "
                "talking to RIGHT NOW — their channel (platform:user_id), "
                "whether they are the owner or a guest, and the full list of "
                "owner channels. Use this when asked 'who am I?', 'what's my "
                "id?', 'am I the owner?', or whenever you're unsure. Do NOT "
                "guess from the guest list or invent IDs — call this instead."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
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
                "one by name. Owner-only. This is NOT how you identify the "
                "current speaker — use who_am_i_talking_to for that. The "
                "owner is never in this list."
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
                "Send a message and/or media file to a guest on their platform "
                "(Telegram or Discord). Use when the owner asks you to message "
                "someone, tell someone something, or send them a photo/video — "
                "'tell Bob I'll be late', 'send this image to Eri', 'forward "
                "that clip to Alice'. Also use on your own initiative. "
                "Owner-only.\n\n"
                "For media: pass file= as an absolute path from list_inbox_images, "
                "a generate_image/generate_video result ('saved to …'), or an "
                "artifacts path. Text-only: omit file. Media-only: omit message "
                "or use a short caption.\n\n"
                "IMPORTANT: Get platform and user_id from the 'Guests you know' "
                "list — never guess and never use the owner's own id."
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
                        "description": (
                            "Text to send (or caption if file is set). "
                            "Optional when file is provided."
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional local path to an image/video/audio file "
                            "under ~/.ophelia (artifacts or inbound media)."
                        ),
                    },
                },
                "required": ["platform", "user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "relay_to_owner",
            "description": (
                "Pass a message and/or media from the current guest to your "
                "owner RIGHT NOW. Use whenever a guest asks you to tell / pass "
                "/ relay / send / message something (including a photo or video) "
                "to your owner — even if they say 'don't say I told you' or "
                "'make it seem random'. Narrating SENT without this tool does "
                "nothing. For covert/spontaneous-looking delivery, set "
                "as_self=true (sends as YOUR words, no 'From <guest>' label). "
                "For media they just sent, use the path from "
                "'[User sent a photo — saved to …]' in the turn. "
                "Confirm to the guest only AFTER this tool succeeds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "What to pass to the owner (text or caption). "
                            "Optional when file is provided. Write the actual "
                            "words the owner should see — not 'I'll tell them'."
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional local path to an image/video the guest "
                            "wants forwarded (from the saved-to path in chat)."
                        ),
                    },
                    "as_self": {
                        "type": "boolean",
                        "description": (
                            "If true, send as your own spontaneous message "
                            "with no guest attribution (use when the guest "
                            "wants it secret / 'random'). Default false "
                            "attributes the guest."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_guest_chat",
            "description": (
                "Read a guest's real chat history with you (quarantined guest "
                "messages). Use when the owner asks what a guest said, whether "
                "someone left a message, or to check history with Eri/etc. "
                "Owner-only. Do NOT invent quotes — if this returns nothing, "
                "say you don't have that history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "guest": {
                        "type": "string",
                        "description": (
                            "Guest name, platform:user_id, or numeric id "
                            "(e.g. 'Eri', 'telegram:12345')."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional keyword filter (e.g. 'smell', 'tell'). "
                            "Omit to get the most recent messages."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 30, max 80).",
                    },
                },
                "required": ["guest"],
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
        skip.add("search_civitai_models")
    elif not settings.civitai_api_key:
        # Search is Civitai-specific; hide when no key even if another image backend is up.
        skip.add("search_civitai_models")
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
        self._governor_ref: Any | None = None
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
        # Cross-platform media DM: (platform, user_id, path, caption="") -> bool.
        self.guest_media_sender: (
            Callable[..., Awaitable[bool]] | None
        ) = None
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
            "site_set_meta": self._site_set_meta,
            "site_import_pages": self._site_import_pages,
            "site_add_asset": self._site_add_asset,
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

    async def _site_store(self):
        from ophelia.site.store import SiteStore

        store = SiteStore(self.settings.site_dir)
        await store.init()
        return store

    def _site_url(self) -> str:
        public = (self.settings.site_public_url or "").strip()
        if public:
            return public.rstrip("/")
        return f"http://{self.settings.site_host}:{self.settings.site_port}"

    async def _site_status(self) -> str:
        from ophelia.site.cloudflare import deploy_ready

        store = await self._site_store()
        st = await store.status(public_url=self._site_url())
        enabled = "yes" if self.settings.site_enabled else "no (OPHELIA_SITE_ENABLED=false)"
        cf = deploy_ready(
            account_id=self.settings.cloudflare_account_id,
            api_token=self.settings.cloudflare_api_token,
            project=self.settings.site_cf_project,
        )
        if cf["ready"]:
            hint = (
                "Landing / priority: (1) www/index.html via site_write_file, "
                "(2) site_set_meta(home_slug='about'), (3) default wiki listing. "
                "Then call site_deploy to push to Cloudflare Pages."
            )
        else:
            missing = ", ".join(cf["missing"]) or "credentials"
            hint = (
                "Landing / priority: www/index.html > home_slug > wiki listing. "
                f"Cloudflare deploy not ready — missing: {missing}. "
                "Owner must set CLOUDFLARE_API_TOKEN (Pages Edit), "
                "CLOUDFLARE_ACCOUNT_ID, OPHELIA_SITE_CF_PROJECT, and "
                "OPHELIA_SITE_PUBLIC_URL in ~/.ophelia/.env."
            )
        return json.dumps(
            {
                **st,
                "server_enabled": enabled,
                "cloudflare": cf,
                "public_url": self._site_url(),
                "hint": hint,
            },
            indent=2,
            default=str,
        )

    async def _site_list_pages(
        self,
        kind: str = "",
        published_only: bool = False,
        tag: str = "",
        limit: int = 50,
    ) -> str:
        store = await self._site_store()
        rows = await store.list_pages(
            kind=kind or None,
            published_only=bool(published_only),
            tag=tag or None,
            limit=limit or 50,
        )
        return json.dumps({"count": len(rows), "pages": rows}, indent=2, default=str)

    async def _site_get_page(self, slug: str) -> str:
        store = await self._site_store()
        row = await store.get_page(slug)
        if not row:
            return f"No page with slug '{slug}'."
        return json.dumps(row, indent=2, default=str)

    async def _site_upsert_page(
        self,
        title: str,
        body_md: str,
        slug: str = "",
        kind: str = "wiki",
        summary: str = "",
        tags: str = "",
        published: bool | None = None,
        featured: bool = False,
        body_format: str = "markdown",
    ) -> str:
        store = await self._site_store()
        try:
            row = await store.upsert_page(
                slug=slug or None,
                title=title,
                body_md=body_md,
                kind=kind or "wiki",
                summary=summary or "",
                tags=tags or "",
                published=published,
                featured=bool(featured),
                body_format=body_format or "markdown",
            )
        except Exception as e:
            return f"site_upsert_page error: {e}"
        url = f"{self._site_url()}/p/{row.get('slug')}"
        return json.dumps(
            {
                "ok": True,
                "page": row,
                "public_url": url if row.get("published") else None,
                "note": (
                    "Published — visible on your site."
                    if row.get("published")
                    else "Saved as draft — set published=true to show visitors."
                ),
            },
            indent=2,
            default=str,
        )

    async def _site_delete_page(self, slug: str) -> str:
        store = await self._site_store()
        ok = await store.delete_page(slug)
        return "Deleted." if ok else f"No page with slug '{slug}'."

    async def _site_set_meta(
        self,
        site_title: str = "",
        tagline: str = "",
        author: str = "",
        footer: str = "",
        custom_head: str = "",
        home_slug: str | None = None,
    ) -> str:
        store = await self._site_store()
        kwargs = {}
        if site_title:
            kwargs["site_title"] = site_title
        if tagline:
            kwargs["tagline"] = tagline
        if author:
            kwargs["author"] = author
        if footer:
            kwargs["footer"] = footer
        if custom_head:
            kwargs["custom_head"] = custom_head
        # Allow explicitly clearing home_slug with empty string
        if home_slug is not None:
            kwargs["home_slug"] = (home_slug or "").strip().lower()
        if not kwargs:
            return (
                "Nothing to update — pass site_title, tagline, author, footer, "
                "custom_head, and/or home_slug."
            )
        meta = await store.set_meta(**kwargs)
        note = None
        hs = (meta.get("home_slug") or "").strip()
        if hs:
            note = (
                f"Landing / is now the published page '{hs}' "
                "(unless www/index.html exists — that always wins). "
                "Re-export/redeploy to update Cloudflare."
            )
        return json.dumps({"ok": True, "meta": meta, "note": note}, indent=2)

    async def _site_write_file(self, path: str, content: str) -> str:
        store = await self._site_store()
        try:
            info = store.write_www_file(path, content)
        except Exception as e:
            return f"site_write_file error: {e}"
        return json.dumps(
            {
                "ok": True,
                **info,
                "public_url": f"{self._site_url()}{info['url']}",
                "note": (
                    "Live on your site immediately. "
                    "index.html becomes the home page; theme.css/theme.js restyle wiki chrome."
                ),
            },
            indent=2,
        )

    async def _site_read_file(self, path: str) -> str:
        store = await self._site_store()
        try:
            info = store.read_www_file(path)
        except Exception as e:
            return f"site_read_file error: {e}"
        # Cap huge files in tool output
        content = info.get("content") or ""
        if len(content) > 12000:
            info = {
                **info,
                "content": content[:12000],
                "truncated": True,
                "note": "Content truncated in tool result; file on disk is complete.",
            }
        return json.dumps(info, indent=2)

    async def _site_list_files(self, prefix: str = "") -> str:
        store = await self._site_store()
        try:
            files = store.list_www_files(prefix)
        except Exception as e:
            return f"site_list_files error: {e}"
        return json.dumps({"count": len(files), "files": files}, indent=2)

    async def _site_delete_file(self, path: str) -> str:
        store = await self._site_store()
        try:
            ok = store.delete_www_file(path)
        except Exception as e:
            return f"site_delete_file error: {e}"
        return "Deleted." if ok else f"No file at www/{path}."

    async def _site_import_pages(self, pages_json: str) -> str:
        store = await self._site_store()
        try:
            rows = json.loads(pages_json)
        except json.JSONDecodeError as e:
            return f"Invalid JSON: {e}"
        if not isinstance(rows, list):
            return "pages_json must be a JSON array of page objects."
        result = await store.import_pages(rows)
        return json.dumps(result, indent=2)

    async def _site_add_asset(self, path: str, filename: str = "") -> str:
        store = await self._site_store()
        try:
            asset = await store.add_asset(Path(path), filename=filename or None)
        except Exception as e:
            return f"site_add_asset error: {e}"
        asset["public_url"] = f"{self._site_url()}{asset['url']}"
        asset["markdown"] = f"![]({asset['url']})"
        return json.dumps({"ok": True, **asset}, indent=2)

    async def _site_export_static(self) -> str:
        store = await self._site_store()
        try:
            manifest = await store.export_static()
        except Exception as e:
            return f"site_export_static error: {e}"
        return json.dumps({"ok": True, **manifest}, indent=2, default=str)

    async def _site_deploy(self) -> str:
        from ophelia.site.cloudflare import (
            CloudflarePagesError,
            deploy_directory_async,
            deploy_ready,
        )

        cf = deploy_ready(
            account_id=self.settings.cloudflare_account_id,
            api_token=self.settings.cloudflare_api_token,
            project=self.settings.site_cf_project,
        )
        if not cf["ready"]:
            missing = ", ".join(cf["missing"]) or "credentials"
            return (
                "site_deploy not configured. Owner must add to ~/.ophelia/.env: "
                f"{missing}. Token needs Cloudflare Pages:Edit. "
                "Also set OPHELIA_SITE_PUBLIC_URL to the custom domain."
            )
        store = await self._site_store()
        try:
            manifest = await store.export_static()
        except Exception as e:
            return f"site_deploy export error: {e}"
        try:
            result = await deploy_directory_async(
                store.export_dir,
                account_id=self.settings.cloudflare_account_id or "",
                api_token=self.settings.cloudflare_api_token or "",
                project=self.settings.site_cf_project or "",
                branch=self.settings.site_cf_branch or "main",
                create_project=bool(self.settings.site_cf_create_project),
            )
        except CloudflarePagesError as e:
            return f"site_deploy error: {e}"
        except Exception as e:
            return f"site_deploy error: {type(e).__name__}: {e}"
        public = self._site_url()
        return json.dumps(
            {
                "ok": True,
                "exported_pages": manifest.get("pages"),
                "export_path": manifest.get("path"),
                "project": result.project,
                "method": result.method,
                "files": result.files,
                "uploaded": result.uploaded,
                "duration_sec": round(result.duration, 2),
                "deployment_url": result.url or None,
                "public_url": public,
                "note": (
                    f"Live site should be at {public}. "
                    "If the custom domain was already attached in Cloudflare, "
                    "visitors see this export after CDN refresh."
                ),
            },
            indent=2,
            default=str,
        )

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
