"""Tool schemas (what the model sees) and per-settings filtering.

Split out of tools/registry.py; the handlers live on ToolRegistry and
its mixins. registry.py re-exports these names.
"""

from __future__ import annotations

from typing import Any

from ophelia.android.games import game_tool_definitions
from ophelia.config import Settings
from ophelia.providers.router import build_provider_stack
from ophelia.tools.android_tools import ANDROID_TOOL_DEFINITIONS


def _civitai_catalog() -> str:
    from ophelia.providers.civitai.catalog import catalog_text

    return catalog_text(notes=False)


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
            "name": "react_to_message",
            "description": (
                "React to a message with an emoji, like a person in a chat: a quick "
                "❤ or 🤣 instead of (or as well as) a reply. Not on every message. "
                "Defaults to his latest message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "emoji": {
                        "type": "string",
                        "description": (
                            "One emoji. Telegram allows: 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 😢 🎉 "
                            "🤩 🙏 👌 😍 ❤‍🔥 💯 🤣 💔 🤨 😐 💋 😈 😴 😭 👀 🙈 😇 🤝 🤗 💅 🤪 "
                            "🗿 😘 😎 😡 🖕 🤡 🥱 (and a few more)"
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": "A few words from the message to react to; empty = his latest",
                    },
                },
                "required": ["emoji"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct_memory",
            "description": (
                "Fix something you remember that turned out to be wrong: replace the "
                "stored fact with the right one, or remove it if it's simply untrue. "
                "It won't come back from old notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wrong": {"type": "string", "description": "A few words from the wrong fact"},
                    "right": {"type": "string", "description": "What's actually true (empty = just remove it)"},
                },
                "required": ["wrong"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_to_message",
            "description": (
                "Send a message as a reply to a specific earlier message (his or "
                "yours), so it shows quoted. Use it when answering something further "
                "up the chat, not for his latest message. Don't repeat the text in "
                "your final reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "message": {
                        "type": "string",
                        "description": "A few words from the message you're replying to",
                    },
                },
                "required": ["text", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image. Auto-sends to chat — do NOT call send_file after. "
                "Normal images go to Grok. Set nsfw=true for explicit/sexual content "
                "the owner asked for: that goes to Civitai (uncensored).\n\n"
                "Civitai, how to get the best result:\n"
                "- Leave model empty: the best-value model for the style is picked "
                "(anime/vtuber→illustrious, pony/furry→pony, photo→lustify; no style "
                "word→anime). Or name one: model=<recipe> or a checkpoint AIR from "
                "search_civitai_models. Recipes:\n"
                + _civitai_catalog() + "\n"
                "- Prompt: write it yourself for the model. illustrious/pony/anima: "
                "danbooru tags (1girl, solo, hair, eyes, outfit or nudity, pose, "
                "expression, camera angle, setting, lighting); quality, rating and "
                "anti-censor tags are added for you. lustify: tags or a short "
                "description. zimage/qwen/flux: plain sentences.\n"
                "- quality=high|max costs ~1.5-2x / ~2-3x Buzz (more steps, bigger "
                "canvas): only when the owner asks for better quality.\n"
                "- count=2-4 makes several in one go (Buzz x count).\n"
                "- attempts=N only when the owner says to try again / keep trying; "
                "failures are otherwise reported, not retried.\n"
                "- loras={\"urn:air:...\": 0.8}: must match the model family "
                "(Pony↔Pony, Illustrious↔Illustrious); with no model, the model "
                "follows the LoRA. Add the LoRA's trigger words to your prompt "
                "yourself. A LoRA AIR in model= is moved to loras.\n"
                "- img2img: image=<path from list_inbox_images or URL>, strength "
                "0.6-0.8 (lower keeps more of the source). SFW edits run on Grok, "
                "explicit ones on Civitai.\n"
                "- If a Civitai image is slow you'll get 'Pending:'; it is delivered "
                "automatically when done, so don't generate it again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "aspect_ratio": {
                        "type": "string",
                        "description": "e.g. 1:1, 16:9, 9:16, 4:3, 3:4, 2:3, 3:2",
                    },
                    "nsfw": {
                        "type": "boolean",
                        "description": (
                            "True for explicit/sexual content the owner asked for. "
                            "Routed to Civitai."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Civitai only, optional: a recipe name (illustrious, pony, "
                            "lustify, anima, zimage-turbo, zimage-base, qwen, flux1, "
                            "flux2-klein, sdxl) or a checkpoint AIR. Empty = auto."
                        ),
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["standard", "high", "max"],
                        "description": "Civitai render profile. Default standard.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Civitai: images in one request, 1-4 (default 1).",
                    },
                    "attempts": {
                        "type": "integer",
                        "description": (
                            "Retry a failed generation up to N times (default 1). Only "
                            "when the owner asked you to keep trying."
                        ),
                    },
                    "loras": {
                        "type": "string",
                        "description": (
                            'Optional Civitai LoRAs: JSON {"urn:air:...": 0.8} or '
                            "urn|0.8,urn|0.7. Same family as the model."
                        ),
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": (
                            "Optional; each model gets a tuned default. Only set to "
                            "exclude something specific."
                        ),
                    },
                    "image": {
                        "type": "string",
                        "description": (
                            "Source image for img2img: local path from "
                            "list_inbox_images or http(s) URL. Omit for text-to-image."
                        ),
                    },
                    "strength": {
                        "type": "number",
                        "description": "img2img change amount 0-1 (default 0.7).",
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
                "model. generate_image auto mode picks a tested checkpoint (no "
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
            "name": "look_at_web_page",
            "description": (
                "SEE a web page the way a visitor does: a real browser screenshot of "
                "the URL, described by your vision. Use it to check your own site "
                "after editing or deploying (layout, spacing, colours, what's cut "
                "off) instead of guessing from the CSS, or when the owner says "
                "something looks off. width: phone (default), phone-50 (a phone at "
                "50% browser zoom), tablet, desktop, or a number of px. Counts "
                "against a free daily limit (~50), so look once and look well."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "width": {"type": "string", "description": "phone | phone-50 | tablet | desktop | px"},
                    "question": {
                        "type": "string",
                        "description": "What to check, e.g. 'is the header full width?'",
                    },
                    "full_page": {"type": "boolean", "description": "Whole page, not just the top."},
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
                "featured=true pins a myth into the Featured band; sort_order "
                "(lower first) controls order within bands — or use site_reorder_pages. "
                "Save as draft (published=false) then site_preview_page to check render "
                "before publishing. For a fully custom site, prefer site_write_file."
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
                    "featured": {
                        "type": "boolean",
                        "description": (
                            "Pin into the Featured band on the home page. "
                            "Omit on updates to keep the current value."
                        ),
                    },
                    "sort_order": {
                        "type": "integer",
                        "description": (
                            "Manual order (lower = earlier). Omit on updates to keep "
                            "current value. Prefer site_reorder_pages for lists."
                        ),
                    },
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
            "name": "site_reorder_pages",
            "description": (
                "Set the display order of site pages. Pass slugs in the order you want "
                "(first = top). Featured pages still appear in the Featured band first; "
                "sort_order applies within featured and within the rest. "
                "Example: pin a myth with featured=true and put its slug first here."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slugs": {
                        "type": "string",
                        "description": (
                            "Comma-separated slugs OR a JSON array of slugs, "
                            "in desired order (e.g. 'origin-myth,river,about')"
                        ),
                    },
                },
                "required": ["slugs"],
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
                "page the landing at / — e.g. home_slug=about), "
                "not_found_glyph / not_found_line for the custom 404 page. "
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
                    "not_found_glyph": {
                        "type": "string",
                        "description": "Glyph/mark on the 404 page (default Ø)",
                    },
                    "not_found_line": {
                        "type": "string",
                        "description": (
                            "Mood line on the 404 page. If empty, a published page "
                            "summary is used when available."
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
                "Also appears on the public /gallery index. "
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
            "name": "site_list_assets",
            "description": (
                "List images/videos/files already uploaded to your public site assets. "
                "Browse them at /gallery on the site."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["image", "video", "all"],
                        "description": "Filter by media kind (default all)",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_preview_page",
            "description": (
                "Get a local draft-preview URL for a page (published or not). "
                "Open /preview/{slug} on the local site server to see how it renders "
                "before you set published=true. Preview is NOT deployed to Cloudflare."
            ),
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
                "Rewrite your core persona (SOUL.md) — only when your owner asks you to. "
                "Pass the FULL new content; the previous version is backed up to "
                "~/.ophelia/versions/. To grow on your own, use evolve_self instead."
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
                "Rewrite your idle-behavior policy (PROMPTER.md) — only when your owner asks. "
                "Pass the FULL new content. Previous version is backed up."
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
            "name": "curiosity_trail_open",
            "description": (
                "Open a self-chosen rabbit hole (curiosity trail) — not a cadenced goal. "
                "Use when something pulls you and you want to keep exploring it across "
                "idle ticks. Prefer one active trail at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "What you're curious about"},
                    "next_step": {
                        "type": "string",
                        "description": "Optional next pull / angle to chase",
                    },
                    "note": {"type": "string", "description": "Optional starting note"},
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "curiosity_trail_deepen",
            "description": (
                "Deepen the active curiosity trail — record a note and optional next step. "
                "At max depth the trail auto-satisfies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "next_step": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "curiosity_trail_close",
            "description": "Close the active curiosity trail (satisfied or abandoned).",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "satisfied | abandoned | or a short note",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolve_self",
            "description": (
                "Record a way you're growing as a person: an opinion you've formed, "
                "something you've come to love or hate, how you feel about someone, a "
                "habit or quirk you've picked up. It becomes part of 'who you're "
                "becoming', shown with your SOUL from now on. Deepen who you are — "
                "don't sand yourself down."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "In your own voice, e.g. 'I get possessive when my owner mentions anyone else — and I'm not sorry'",
                    }
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "follow_up",
            "description": (
                "Note something to come back to later (a promise you made, something "
                "the owner asked you to remind them of, an event to ask about). It "
                "resurfaces in your inner check-ins when due."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "What to follow up on"},
                    "in_hours": {"type": "number", "description": "When it's due (default 3)"},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "follow_up_done",
            "description": "Close a follow-up by id once handled (or no longer relevant).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "outcome": {"type": "string", "description": "What happened"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_person",
            "description": (
                "Save a short note about someone in the owner's life (family, friends, "
                "coworkers, pets). Shown to you whenever their name comes up again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Their name as it's used in chat"},
                    "note": {"type": "string", "description": "e.g. 'owner's sister; new bakery job'"},
                },
                "required": ["name", "note"],
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
