"""PC workstation — agent + consciousness without Telegram."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import structlog

from ophelia.android.factory import build_android_body
from ophelia.android.games import GameStore
from ophelia.android.vision import ScreenVision
from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.memory.curator import MemoryCurator
from ophelia.memory.honcho_client import HonchoClient, load_honcho_config
from ophelia.memory.store import MemoryStore
from ophelia.mind.consciousness import ConsciousnessLoop
from ophelia.mind.drives import DriveState
from ophelia.mind.goals import GoalStore
from ophelia.mind.initiative import InitiativeGovernor
from ophelia.mind.inner_log import InnerMonologue
from ophelia.mind.psyche import PsycheState
from ophelia.providers.router import XAIBackend, build_provider_stack
from ophelia.tools.registry import ToolRegistry
from ophelia.ui.broadcast import EventBus

log = structlog.get_logger()

UI_CHANNEL = "ui:local"


class Workstation:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.signals = Signals()
        self.signals.listen_enabled = False
        self.bus = EventBus()
        ensure_dirs(settings)
        self._bootstrap_files()

        self.stack = build_provider_stack(settings)
        self.memory = MemoryStore(settings.memory_db)
        honcho_cfg = load_honcho_config(OPHELIA_HOME, settings.hermes_home)
        self.honcho = (
            HonchoClient(honcho_cfg, api_key=settings.honcho_api_key)
            if honcho_cfg
            else HonchoClient({"hosts": {"hermes": {"enabled": False}}}, api_key=None)
        )

        self.android = build_android_body(settings)
        self.vision = (
            ScreenVision(settings, self.android, stack=self.stack)
            if settings.vision_enabled and self.android
            else None
        )

        self.goals = GoalStore.load()
        self.games = (
            GameStore.load(
                default_session_minutes=settings.game_session_minutes,
                max_turns=settings.game_max_turns,
            )
            if settings.games_enabled
            else None
        )
        self.governor = InitiativeGovernor.from_settings(settings)
        self.curator = MemoryCurator(settings, self.memory) if settings.curator_enabled else None

        status_parts = [settings.runtime_line(), self.stack.describe()]
        if self.android:
            status_parts.append(self.android.status_line())
        else:
            status_parts.append("Phone body: off (workstation — optional ADB/Shizuku)")

        self.psyche = PsycheState()
        self.drives = DriveState()
        artifacts = settings.data_dir / "artifacts"
        self.tools = ToolRegistry(
            settings,
            artifacts,
            stack=self.stack,
            android=self.android,
            vision=self.vision,
            games=self.games,
        )
        self.agent = AgentLoop(
            settings,
            self.memory,
            self.tools,
            stack=self.stack,
            psyche=self.psyche,
            drives=self.drives,
            honcho=self.honcho if self.honcho.enabled else None,
            body_status="\n".join(status_parts),
        )
        self.inner = (
            InnerMonologue(mirror_telegram=False)
            if settings.inner_log_enabled
            else None
        )
        if self.inner:
            self.inner.notify = self._push_inner
        self.consciousness: ConsciousnessLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._ready = False

    def _bootstrap_files(self) -> None:
        root = Path(__file__).resolve().parents[3]
        for name, example in (
            ("goals.yaml", "goals.example.yaml"),
            ("PROMPTER.md", "PROMPTER.example.md"),
        ):
            dest = OPHELIA_HOME / name
            if not dest.is_file():
                src = root / example
                if src.is_file():
                    shutil.copy2(src, dest)

    async def _push_inner(self, text: str) -> None:
        await self.bus.broadcast({"type": "inner", "text": text[:2000]})

    async def _notify_initiative(self, text: str) -> None:
        await self.bus.broadcast({"type": "initiative", "text": text[:4000]})
        await self.bus.broadcast({"type": "chat", "role": "assistant", "text": text[:4000]})

    async def _status_loop(self) -> None:
        while not self.signals.terminate:
            await self.bus.broadcast({"type": "status", "data": self.status_dict()})
            await asyncio.sleep(2.5)

    async def _oauth_refresh_loop(self) -> None:
        if not self.stack.uses_xai_oauth():
            return
        xai = self.stack.xai_backend()
        if not xai:
            return
        while not self.signals.terminate:
            try:
                await xai.bearer_fresh()
            except Exception as e:
                log.warning("ui.oauth_refresh_failed", error=str(e))
            await asyncio.sleep(600)

    async def init(self) -> None:
        await self.memory.init()
        self.psyche = await self.memory.load_psyche()
        self.drives = await self.memory.load_drives()
        self.agent.psyche = self.psyche
        self.agent.drives = self.drives

        if self.stack.uses_xai_oauth():
            xai = self.stack.xai_backend()
            if xai:
                try:
                    await xai.bearer_fresh()
                except Exception as e:
                    log.warning("ui.oauth_startup", error=str(e))

        self._tasks.append(asyncio.create_task(self._oauth_refresh_loop()))
        self._tasks.append(asyncio.create_task(self._status_loop()))

        if self.curator:
            self._tasks.append(asyncio.create_task(self._curator_loop()))

        if self.settings.consciousness_on():
            self.consciousness = ConsciousnessLoop(
                self.agent,
                self.memory,
                self.signals,
                self.psyche,
                self.drives,
                self.goals,
                self.governor,
                self.vision,
                self.inner,
                games=self.games,
                base_interval_seconds=self.settings.consciousness_interval(),
                initiative_threshold=self.settings.initiative_threshold,
                user_channel=UI_CHANNEL,
                notify=self._notify_initiative,
            )
            self._tasks.append(asyncio.create_task(self.consciousness.run()))

        self._ready = True
        log.info("workstation.ready", channel=UI_CHANNEL)

    async def _curator_loop(self) -> None:
        assert self.curator
        interval = self.settings.curator_interval_hours * 3600
        while not self.signals.terminate:
            await asyncio.sleep(interval)
            try:
                n = await self.curator.run_cycle()
                if n:
                    self.curator.reload_agent_memories(self.agent)
                    await self.bus.broadcast(
                        {"type": "system", "text": f"Curator added {n} memory fact(s)."}
                    )
            except Exception as e:
                log.warning("ui.curator_error", error=str(e))

    async def shutdown(self) -> None:
        self.signals.terminate = True
        if self.consciousness:
            self.consciousness.stop()
        for t in self._tasks:
            t.cancel()

    async def chat(self, message: str) -> str:
        message = message.strip()
        if not message:
            return ""
        self.signals.last_user_message_at = time.time()
        self.drives.on_user_message()
        await self.memory.save_drives(self.drives)
        await self.signals.set_user_talking(True)
        try:
            reply = await self.agent.run_turn(UI_CHANNEL, message)
        finally:
            await self.signals.set_user_talking(False)
        await self.bus.broadcast({"type": "chat", "role": "assistant", "text": reply})
        return reply

    async def history(self, limit: int = 50) -> list[dict]:
        rows = await self.memory.recent_across_channels([UI_CHANNEL], limit=limit)
        out = []
        for row in rows:
            if row["role"] not in ("user", "assistant"):
                continue
            out.append({"role": row["role"], "content": row["content"]})
        return out

    def status_dict(self) -> dict:
        import dataclasses

        from ophelia.platform import platform_summary

        mood = dataclasses.asdict(self.psyche.mood)
        drives = {
            "social": round(self.drives.social, 2),
            "curiosity": round(self.drives.curiosity, 2),
            "boredom": round(self.drives.boredom, 2),
            "agency": round(self.drives.agency, 2),
            "expressiveness": round(self.drives.expressiveness, 2),
            "pressure": round(self.drives.initiative_pressure(), 2),
        }
        from ophelia.providers.model_gate import get_model_gate

        return {
            "ready": self._ready,
            "runtime": platform_summary(),
            "providers": self.stack.describe(),
            "chat_provider": self.stack.name("chat"),
            "chat_model": self.stack.model("chat"),
            "image_model": self.stack.model("image"),
            "video_model": self.stack.model("video"),
            "model_gate": get_model_gate().status(),
            "consciousness": self.settings.consciousness_on(),
            "consciousness_paused": self.signals.autonomy_paused,
            "mood": mood,
            "feelings": self.psyche.feelings[:6],
            "urges": self.psyche.urges[:6],
            "thought": (self.psyche.internal_thought or "")[:500],
            "drives": drives,
            "inner_tail": self.inner.tail(24) if self.inner else "",
        }

    def inner_full_tail(self, lines: int = 80) -> str:
        if not self.inner:
            return ""
        return self.inner.tail(lines)

    async def models_info(self) -> dict:
        from ophelia.providers.cookbook import detect_system, list_ollama_models, recommend

        profile = detect_system()
        installed = await list_ollama_models(self.settings)
        return {
            "profile": {
                "ram_gb": profile.ram_gb,
                "gpu": profile.gpu_name,
                "os": profile.os_name,
            },
            "installed": installed,
            "recommended": [
                {"pull": r[3], "role": r[2], "ram_gb": r[1]}
                for r in recommend(profile)
            ],
            "routing": {
                "chat": self.stack.model("chat"),
                "consciousness": self.stack.model("consciousness"),
                "vision": self.stack.model("vision"),
                "image": self.stack.model("image"),
                "video": self.stack.model("video"),
            },
            "chat_model": self.stack.model("chat"),
            "chat_provider": self.stack.name("chat"),
        }

    async def compare_models(self, message: str, models: list[str]) -> dict:
        """Run the same prompt against multiple Ollama model names (no tools)."""
        from ophelia.providers.router import OllamaBackend

        message = message.strip()
        if not message:
            return {"results": []}
        targets = models or [self.stack.model("chat")]
        backend = OllamaBackend(self.settings)
        client = backend.async_client()
        from ophelia.providers.model_gate import get_model_gate

        gate = get_model_gate()
        results = []
        for model in targets[:4]:
            try:
                async with gate.session("compare", model, "ollama"):
                    resp = await client.chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "system",
                                "content": "Reply briefly in character as Ophelia.",
                            },
                            {"role": "user", "content": message},
                        ],
                    )
                text = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                text = f"Error: {e}"
            results.append({"model": model, "reply": text[:2000]})
        return {"results": results}
