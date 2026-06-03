"""Screenshot → Grok vision — closed perception loop for the Android body."""

from __future__ import annotations

import base64
import time
from pathlib import Path

import structlog

from ophelia.android.games import GameProfile
from ophelia.android.shizuku import AndroidBody
from ophelia.config import Settings
from ophelia.providers.router import XAIBackend, build_backend

log = structlog.get_logger()

VISION_PROMPT = """You are Ophelia's eyes on the Android phone screen.
Describe what is visible: apps, notifications, text, buttons, mood of the scene.
Then suggest ONE concrete next action if useful (tap target with approximate x,y from ui-dump if known, or open-app package, or 'none').
Be concise. This is your body — not a user request."""


class ScreenVision:
    def __init__(self, settings: Settings, android: AndroidBody | None) -> None:
        self.settings = settings
        self.android = android
        self.shots_dir = settings.data_dir / "screenshots"

    async def capture(self) -> Path | None:
        if not self.android or self.android.mode == "termux_only":
            return None
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.shots_dir / f"screen_{int(time.time())}.png"
        result = await self.android.screenshot_path(path)
        if path.is_file():
            return path
        log.warning("vision.capture_failed", result=result[:200])
        return None

    async def see(
        self,
        *,
        question: str = "What is on screen? What matters right now?",
        include_ui_dump: bool = True,
    ) -> str:
        if not self.android or self.android.mode == "termux_only":
            return "Vision unavailable — set up Shizuku (scripts/termux-shizuku-setup.sh)."

        path = await self.capture()
        ui_text = ""
        if include_ui_dump:
            ui_text = (await self.android.ui_dump())[:6000]

        if not path:
            if ui_text and "No Shizuku" not in ui_text:
                return f"No screenshot; UI tree only:\n{ui_text[:4000]}"
            return ui_text or "Could not capture screen."

        backend = build_backend(self.settings)
        if not isinstance(backend, XAIBackend):
            return f"Screenshot at {path} (vision requires xAI provider)."

        b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        client = await backend.async_client_fresh()
        model = self.settings.vision_model or self.settings.xai_model

        content: list[dict] = [
            {"type": "text", "text": f"{VISION_PROMPT}\n\nQuestion: {question}"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
            },
        ]
        if ui_text:
            content.insert(
                1,
                {
                    "type": "text",
                    "text": f"Accessibility tree (for tap coordinates):\n{ui_text[:5000]}",
                },
            )

        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=800,
            )
            text = (resp.choices[0].message.content or "").strip()
            log.info("vision.ok", model=model, chars=len(text))
            return text
        except Exception as e:
            log.warning("vision.api_failed", error=str(e))
            fallback = f"Screenshot saved {path}. Vision API failed: {e}"
            if ui_text:
                fallback += f"\n\nUI dump:\n{ui_text[:3000]}"
            return fallback

    async def see_for_game(
        self, profile: GameProfile, intent: str = ""
    ) -> str:
        return await self.see(
            question=profile.vision_question(intent),
            include_ui_dump=True,
        )

    async def explore_cycle(self, intent: str = "") -> str:
        """Full Tier-1 loop: see → brief for consciousness/act."""
        q = intent or "What do you see? Anything you want to do or tell the user?"
        return await self.see(question=q, include_ui_dump=True)
