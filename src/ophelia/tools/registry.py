from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ophelia.android.games import GameStore, game_tool_definitions
from ophelia.android.shizuku import AndroidBody
from ophelia.android.vision import ScreenVision
from ophelia.config import Settings
from ophelia.providers.router import XAIBackend, build_backend
from ophelia.tools.android_tools import ANDROID_TOOL_DEFINITIONS

ToolHandler = Callable[..., Awaitable[str]]


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image from a text prompt using Grok Imagine.",
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
            "description": "Start async video generation from a text prompt.",
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
]


def all_tool_definitions(settings: Settings) -> list[dict[str, Any]]:
    tools = list(TOOL_DEFINITIONS)
    if settings.android_enabled:
        tools.extend(ANDROID_TOOL_DEFINITIONS)
        if settings.games_enabled:
            tools.extend(game_tool_definitions())
    elif settings.vision_enabled:
        tools = [t for t in tools if t.get("function", {}).get("name") != "phone_see_screen"]
    return tools


class ToolRegistry:
    def __init__(
        self,
        settings: Settings,
        artifacts_dir: Any,
        android: AndroidBody | None = None,
        vision: ScreenVision | None = None,
        games: GameStore | None = None,
    ) -> None:
        from pathlib import Path

        self.settings = settings
        self._backend = build_backend(settings)
        self.android = android
        self.games = games
        self.vision = vision or (
            ScreenVision(settings, android) if android and settings.vision_enabled else None
        )
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._handlers: dict[str, ToolHandler] = {
            "generate_image": self._generate_image,
            "generate_video": self._generate_video,
            "text_to_speech": self._text_to_speech,
            "run_code": self._run_code,
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

    async def dispatch(self, name: str, arguments: str) -> str:
        handler = self._handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return "Invalid tool arguments JSON"
        return await handler(**args)

    def _xai(self) -> XAIBackend:
        if not isinstance(self._backend, XAIBackend):
            raise RuntimeError("Media tools require OPHELIA_PROVIDER=xai-oauth or xai")
        return self._backend

    async def _generate_image(self, prompt: str, aspect_ratio: str = "1:1") -> str:
        client = self._xai().async_client()
        resp = await client.images.generate(
            model="grok-imagine-image",
            prompt=prompt,
            extra_body={"aspect_ratio": aspect_ratio},
        )
        url = resp.data[0].url if resp.data else None
        if not url:
            return "Image generation returned no URL."
        return f"Image generated: {url}"

    async def _generate_video(
        self, prompt: str, duration_seconds: int = 6
    ) -> str:
        import httpx

        xai = self._xai()
        token = xai.bearer()
        if not token:
            return "No xAI credentials for video."

        async with httpx.AsyncClient(timeout=120.0) as http:
            r = await http.post(
                f"{xai.settings.xai_base_url.rstrip('/')}/videos/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "grok-imagine-video",
                    "prompt": prompt,
                    "duration": duration_seconds,
                },
            )
            r.raise_for_status()
            data = r.json()

        request_id = data.get("request_id") or data.get("id")
        return (
            f"Video job started (request_id={request_id}). "
            "Poll xAI video API or ask Ophelia to check status in a follow-up."
        )

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
        return f"TTS saved to {out}"

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

    async def _phone_see_screen(self, question: str = "") -> str:
        if not self.vision:
            return "Vision disabled or no Android body."
        return await self.vision.see(question=question or "What is on screen?")

    async def _phone_ui_dump(self) -> str:
        if not self.android:
            return "Android body disabled."
        return await self.android.ui_dump()

    async def _phone_tap(self, x: int, y: int) -> str:
        if not self.android:
            return "Android body disabled."
        return await self.android.tap(x, y)

    async def _phone_open_app(self, package: str) -> str:
        if not self.android:
            return "Android body disabled."
        return await self.android.open_app(package)

    async def _phone_shell(self, command: str) -> str:
        if not self.android:
            return "Android body disabled."
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
            return "Android body disabled."
        return await self.android.swipe(x1, y1, x2, y2, duration_ms)

    async def _phone_key(self, key: str) -> str:
        if not self.android:
            return "Android body disabled."
        return await self.android.key(key)

    async def _phone_game_look(self, game_id: str = "", intent: str = "") -> str:
        if not self.vision:
            return "Vision disabled or no Android body."
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
