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
from ophelia.mind.avatar import AvatarBridge
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
        self.avatar = AvatarBridge(
            enabled=settings.avatar_enabled,
            avatar_dir=settings.avatar_dir,
            model_path=settings.avatar_model,
            backend=settings.avatar_backend,
        )
        artifacts = settings.data_dir / "artifacts"
        self.tools = ToolRegistry(
            settings,
            artifacts,
            stack=self.stack,
            android=self.android,
            vision=self.vision,
            games=self.games,
        )
        self.tools._governor_ref = self.governor
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
        from ophelia.channels.proactive_filter import (
            is_outreach_junk,
            strip_consciousness_tick_leak,
        )

        cleaned = strip_consciousness_tick_leak(text or "")
        if not cleaned or is_outreach_junk(cleaned):
            return
        if self.settings.avatar_enabled:
            self.avatar.begin_speak(cleaned or "", source="initiative")
            await self.bus.broadcast({"type": "avatar", "data": self.avatar_dict()})
        await self.bus.broadcast({"type": "initiative", "text": cleaned[:4000]})
        await self.bus.broadcast({"type": "chat", "role": "assistant", "text": cleaned[:4000]})
        self.signals.last_agent_message_at = time.time()

    async def _status_loop(self) -> None:
        while not self.signals.terminate:
            await self.bus.broadcast({"type": "status", "data": self.status_dict()})
            if self.settings.avatar_enabled:
                await self.bus.broadcast({"type": "avatar", "data": self.avatar_dict()})
            await asyncio.sleep(2.5)

    async def _avatar_loop(self) -> None:
        """Higher-rate avatar ticks while speaking / thinking / reacting."""
        while not self.signals.terminate:
            if self.settings.avatar_enabled and (
                self.avatar.is_active
                or self.signals.agent_thinking
                or self.signals.user_talking
                or self.avatar.last() is None
            ):
                await self.bus.broadcast({"type": "avatar", "data": self.avatar_dict()})
                await asyncio.sleep(0.08)
            else:
                await asyncio.sleep(0.4)

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
        self.tools._drives_ref = self.drives
        self.tools._governor_ref = self.governor

        if self.stack.uses_xai_oauth():
            xai = self.stack.xai_backend()
            if xai:
                try:
                    await xai.bearer_fresh()
                except Exception as e:
                    log.warning("ui.oauth_startup", error=str(e))

        self._tasks.append(asyncio.create_task(self._oauth_refresh_loop()))
        self._tasks.append(asyncio.create_task(self._status_loop()))
        if self.settings.avatar_enabled:
            self._tasks.append(asyncio.create_task(self._avatar_loop()))

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
        if self.settings.avatar_enabled:
            self.avatar.note_user_text(message)
            self.avatar.begin_thinking()
            await self.bus.broadcast({"type": "avatar", "data": self.avatar_dict()})
        await self.signals.set_agent_thinking(True)
        try:
            reply = await self.agent.run_turn(UI_CHANNEL, message)
        finally:
            await self.signals.set_agent_thinking(False)
            await self.signals.set_user_talking(False)
        from ophelia.channels.proactive_filter import (
            is_outreach_junk,
            strip_consciousness_tick_leak,
        )

        reply = strip_consciousness_tick_leak(reply or "")
        if not reply or is_outreach_junk(reply):
            reply = ""
        self.signals.last_agent_message_at = time.time()
        if self.settings.avatar_enabled:
            self.avatar.begin_speak(reply or "", source="chat")
            await self.bus.broadcast({"type": "avatar", "data": self.avatar_dict()})
        if reply:
            await self.bus.broadcast({"type": "chat", "role": "assistant", "text": reply})
        return reply

    def avatar_dict(self) -> dict:
        if not self.settings.avatar_enabled:
            return {"enabled": False}
        now = time.time()
        since_user = (
            now - self.signals.last_user_message_at
            if self.signals.last_user_message_at
            else None
        )
        since_agent = (
            now - self.signals.last_agent_message_at
            if self.signals.last_agent_message_at
            else None
        )
        state = self.avatar.snapshot(
            label=self.psyche.mood.label,
            valence=self.psyche.mood.valence,
            arousal=self.psyche.mood.arousal,
            feelings=list(self.psyche.feelings[:6]),
            boredom=self.drives.boredom,
            curiosity=self.drives.curiosity,
            social=self.drives.social,
            expressiveness=self.drives.expressiveness,
            urges=list(self.psyche.urges[:4]),
            thought=self.psyche.internal_thought or "",
            user_talking=self.signals.user_talking,
            agent_thinking=self.signals.agent_thinking,
            seconds_since_user=since_user,
            seconds_since_agent=since_agent,
        )
        data = state.to_dict()
        data["enabled"] = True
        return data

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
            "avatar": self.avatar_dict() if self.settings.avatar_enabled else {"enabled": False},
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
            "providers": {
                "chat": self.stack.name("chat"),
                "consciousness": self.stack.name("consciousness"),
                "vision": self.stack.name("vision"),
                "image": self.stack.name("image"),
                "video": self.stack.name("video"),
            },
            "selectable_roles": ["chat", "consciousness", "vision", "curator"],
            "chat_model": self.stack.model("chat"),
            "chat_provider": self.stack.name("chat"),
        }

    async def select_model(
        self,
        role: str,
        model: str,
        *,
        persist: bool = True,
    ) -> dict:
        """Switch the active model for a role at runtime (and optionally in .env)."""
        role = (role or "chat").strip().lower()
        model = (model or "").strip()
        allowed = {"chat", "consciousness", "vision", "curator"}
        if role not in allowed:
            raise ValueError(f"role must be one of {sorted(allowed)}")
        if not model:
            raise ValueError("model is required")

        provider = self.stack.name(role)  # type: ignore[arg-type]
        env_key, attr = self._model_setting_for(provider, role)
        setattr(self.settings, attr, model)
        # OpenAI-compatible backends cache model at build time; drop them.
        self.stack._backends.clear()

        persisted: list[str] = []
        if persist:
            from ophelia.setup.env_io import write_env_updates

            persisted = write_env_updates({env_key: model})

        log.info(
            "workstation.model_selected",
            role=role,
            model=model,
            provider=provider,
            env_key=env_key,
            persisted=bool(persisted),
        )
        await self.bus.broadcast({"type": "status", "data": self.status_dict()})
        await self.bus.broadcast({"type": "system", "text": f"model · {role} → {model}"})
        info = await self.models_info()
        info["selected"] = {
            "role": role,
            "model": model,
            "provider": provider,
            "env_key": env_key,
            "persisted": bool(persisted),
        }
        return info

    @staticmethod
    def _model_setting_for(provider: str, role: str) -> tuple[str, str]:
        """Return (env_key, settings_attr) for a provider/role model override."""
        if provider == "ollama":
            mapping = {
                "chat": ("OLLAMA_MODEL", "ollama_model"),
                "consciousness": ("OLLAMA_CONSCIOUSNESS_MODEL", "ollama_consciousness_model"),
                "vision": ("OLLAMA_VISION_MODEL", "ollama_vision_model"),
                "curator": ("OLLAMA_CURATOR_MODEL", "ollama_curator_model"),
            }
            return mapping[role]
        if provider in ("xai", "xai-oauth"):
            mapping = {
                "chat": ("XAI_MODEL", "xai_model"),
                "consciousness": ("XAI_CONSCIOUSNESS_MODEL", "xai_consciousness_model"),
                "vision": ("XAI_VISION_MODEL", "vision_model"),
                "curator": ("XAI_CURATOR_MODEL", "xai_curator_model"),
            }
            return mapping[role]
        if provider == "openai":
            mapping = {
                "chat": ("OPENAI_MODEL", "openai_model"),
                "consciousness": ("OPENAI_CONSCIOUSNESS_MODEL", "openai_consciousness_model"),
                "vision": ("OPENAI_VISION_MODEL", "openai_vision_model"),
                "curator": ("OPENAI_CURATOR_MODEL", "openai_curator_model"),
            }
            return mapping[role]
        if provider == "deepseek":
            mapping = {
                "chat": ("DEEPSEEK_MODEL", "deepseek_model"),
                "consciousness": ("DEEPSEEK_CONSCIOUSNESS_MODEL", "deepseek_consciousness_model"),
                "vision": ("DEEPSEEK_VISION_MODEL", "deepseek_vision_model"),
                "curator": ("DEEPSEEK_CURATOR_MODEL", "deepseek_curator_model"),
            }
            return mapping[role]
        if provider == "compat":
            mapping = {
                "chat": ("OPHELIA_COMPAT_MODEL", "compat_model"),
                "consciousness": (
                    "OPHELIA_COMPAT_CONSCIOUSNESS_MODEL",
                    "compat_consciousness_model",
                ),
                "vision": ("OPHELIA_COMPAT_VISION_MODEL", "compat_vision_model"),
                "curator": ("OPHELIA_COMPAT_CURATOR_MODEL", "compat_curator_model"),
            }
            return mapping[role]
        # Fallback: treat as Ollama chat model so local installs always work.
        return ("OLLAMA_MODEL", "ollama_model")

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
