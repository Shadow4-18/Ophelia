from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable
from pathlib import Path

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
            "description": "Generate an image from a text prompt (xAI, OpenAI, or Ollama flux).",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "aspect_ratio": {
                        "type": "string",
                        "description": "e.g. 1:1, 16:9, 9:16",
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
                "Generate a video from a text prompt (xAI Grok Imagine). "
                "Waits up to 10m, saves mp4 under artifacts, returns path for Telegram."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 15},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_to_speech",
            "description": "Convert text to speech audio; returns path to saved file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice_id": {
                        "type": "string",
                        "description": "Built-in voice, e.g. eve, ara, rex",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hermes_memory",
            "description": "Search imported Hermes session history (state.db) for past conversations.",
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
        self.proactive_sender: Callable[[str], Awaitable[None]] | None = None
        self._pending_artifacts: list[Path] = []
        self._handlers: dict[str, ToolHandler] = {
            "send_message": self._send_message,
            "generate_image": self._generate_image,
            "generate_video": self._generate_video,
            "text_to_speech": self._text_to_speech,
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
        }

    def set_message_sender(self, fn: Callable[[str], Awaitable[None]]) -> None:
        self._message_sender = fn

    def clear_message_sender(self) -> None:
        self._message_sender = None

    def consume_pending_artifacts(self) -> list[Path]:
        out = list(self._pending_artifacts)
        self._pending_artifacts.clear()
        return out

    def _record_artifacts_from_text(self, text: str) -> None:
        for p in artifact_paths_in_text(text):
            if p not in self._pending_artifacts:
                self._pending_artifacts.append(p)

    async def _send_message(self, text: str) -> str:
        from ophelia.channels.message_split import split_messages

        sender = self._message_sender or self.proactive_sender
        if not sender:
            return "No channel available to send to right now."
        sent = 0
        for chunk in split_messages(text):
            await sender(chunk)
            sent += 1
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

    async def _generate_image(self, prompt: str, aspect_ratio: str = "1:1") -> str:
        result = await generate_image(
            self.settings,
            self.stack,
            prompt,
            aspect_ratio=aspect_ratio,
            artifacts_dir=self.artifacts_dir,
        )
        self._record_artifacts_from_text(result)
        return result

    async def _generate_video(
        self, prompt: str, duration_seconds: int = 6
    ) -> str:
        result = await generate_video(
            self.settings,
            self.stack,
            prompt,
            duration_seconds=duration_seconds,
            artifacts_dir=self.artifacts_dir,
        )
        self._record_artifacts_from_text(result)
        return result

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

    async def _text_to_speech(self, text: str, voice_id: str = "eve") -> str:
        import httpx

        xai = self._xai()
        token = xai.bearer()
        if not token:
            return "No xAI credentials for TTS."

        out = self.artifacts_dir / f"tts_{abs(hash(text)) % 10**8}.mp3"
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.post(
                f"{xai.settings.xai_base_url.rstrip('/')}/tts",
                headers={"Authorization": f"Bearer {token}"},
                json={"text": text, "voice_id": voice_id, "language": "en"},
            )
            r.raise_for_status()
            out.write_bytes(r.content)
        result = f"TTS saved to {out}"
        self._record_artifacts_from_text(result)
        return result

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
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are Ophelia reflecting privately. Output only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
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

    async def _phone_ui_dump(self) -> str:
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        return await self.android.ui_dump()

    async def _phone_tap(self, x: int, y: int) -> str:
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        return await self.android.tap(x, y)

    async def _phone_open_app(self, package: str) -> str:
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        return await self.android.open_app(package)

    async def _phone_shell(self, command: str) -> str:
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        return await self.android.shell(command)

    async def _phone_swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> str:
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        return await self.android.swipe(x1, y1, x2, y2, duration_ms)

    async def _phone_key(self, key: str) -> str:
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
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
