from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

from ophelia.android.factory import build_android_body
from ophelia.android.games import GameStore
from ophelia.android.vision import ScreenVision
from ophelia.channels.hub import ChannelHub
from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.media.listen_loop import LocalListenLoop
from ophelia.memory.curator import MemoryCurator
from ophelia.memory.honcho_client import HonchoClient, load_honcho_config
from ophelia.memory.store import MemoryStore
from ophelia.mind.consciousness import ConsciousnessLoop
from ophelia.mind.drives import DriveState
from ophelia.mind.goals import GoalStore
from ophelia.mind.initiative import InitiativeGovernor
from ophelia.mind.inner_log import InnerMonologue
from ophelia.mind.psyche import PsycheState
from ophelia.providers.router import ProviderStack, XAIBackend, build_provider_stack
from ophelia.tools.registry import ToolRegistry

log = structlog.get_logger()


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.signals = Signals()
        self.signals.listen_enabled = settings.listen_enabled_default
        ensure_dirs(settings)
        self._ensure_goals_file()
        self._ensure_prompter_file()
        self._ensure_games_file()

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
        status_parts = [settings.runtime_line()]
        body_status = ""
        if self.android:
            body_status = self.android.status_line()
            if self.vision and self.android.mode != "termux_only":
                body_status += " | vision=on"
        elif not settings.android_enabled:
            hint = ""
            if settings.adb_device:
                hint = " — set OPHELIA_ANDROID_ENABLED=true for optional phone body"
            body_status = f"Phone body: off (optional — not required on PC/server/VPS{hint})"
        if body_status:
            status_parts.append(body_status)

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

        artifacts = settings.data_dir / "artifacts"
        self.psyche = PsycheState()
        self.drives = DriveState()
        goals_block = self.goals.to_context_block()
        games_block = self.games.to_context_block() if self.games else ""
        status_parts.append(self.stack.describe())
        if goals_block:
            status_parts.append(goals_block)
        if games_block:
            status_parts.append(games_block)
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
            self.psyche,
            stack=self.stack,
            drives=self.drives,
            honcho=self.honcho if self.honcho.enabled else None,
            body_status="\n".join(status_parts),
        )
        self.hub = ChannelHub(
            settings,
            self.agent,
            self.signals,
            self.memory,
            self.drives,
            games=self.games,
            vision=self.vision,
        )
        # send_message tool fallback: consciousness/autonomous turns reach the user.
        self.tools.proactive_sender = self.hub.broadcast_proactive
        self.inner = (
            InnerMonologue(mirror_telegram=settings.inner_mirror_telegram)
            if settings.inner_log_enabled
            else None
        )
        if self.inner:
            self.inner.notify = self._notify_inner_mirror
            self.signals.inner_mirror = settings.inner_mirror_telegram
        self.consciousness: ConsciousnessLoop | None = None
        self.listen: LocalListenLoop | None = None

    def _ensure_goals_file(self) -> None:
        dest = OPHELIA_HOME / "goals.yaml"
        if dest.is_file():
            return
        example = Path(__file__).resolve().parents[3] / "goals.example.yaml"
        if example.is_file():
            shutil.copy2(example, dest)

    def _ensure_prompter_file(self) -> None:
        dest = OPHELIA_HOME / "PROMPTER.md"
        if dest.is_file():
            return
        example = Path(__file__).resolve().parents[3] / "PROMPTER.example.md"
        if example.is_file():
            shutil.copy2(example, dest)

    def _ensure_games_file(self) -> None:
        if not self.settings.games_enabled:
            return
        dest = OPHELIA_HOME / "games.yaml"
        if dest.is_file():
            return
        example = Path(__file__).resolve().parents[3] / "games.example.yaml"
        if example.is_file():
            shutil.copy2(example, dest)

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
                log.warning("oauth.refresh_background_failed", error=str(e))
            await asyncio.sleep(600)

    async def _curator_loop(self) -> None:
        if not self.curator:
            return
        interval = self.settings.curator_interval_hours * 3600
        while not self.signals.terminate:
            await asyncio.sleep(interval)
            try:
                n = await self.curator.run_cycle()
                if n:
                    self.curator.reload_agent_memories(self.agent)
                    log.info("curator.cycle_done", new_facts=n)
            except Exception as e:
                log.warning("curator.error", error=str(e))

    async def _greet_on_start(self) -> None:
        """Proactive hello when she comes online — first visible sign of autonomy."""
        await asyncio.sleep(3.0)
        try:
            channel = self.settings.primary_user_channel() or "consciousness"
            text = await self.agent.run_turn(
                channel,
                "[system] You just came online. Greet your user briefly in your own "
                "voice — one or two sentences. No task questions, just presence.",
            )
            await self.hub.broadcast_proactive(text)
            log.info("greet.sent")
        except Exception as e:
            log.warning("greet.failed", error=str(e))

    async def _notify_spontaneous(self, text: str) -> None:
        await self.hub.broadcast_proactive(text)

    async def _notify_inner_mirror(self, text: str) -> None:
        await self.hub.broadcast_proactive(text)

    async def start(self) -> None:
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
                    log.warning("oauth.startup_refresh_failed", error=str(e))

        tasks: list[asyncio.Task] = [
            asyncio.create_task(self._oauth_refresh_loop()),
        ]

        if self.curator:
            tasks.append(asyncio.create_task(self._curator_loop()))
            try:
                n = await self.curator.run_cycle()
                if n:
                    self.curator.reload_agent_memories(self.agent)
            except Exception as e:
                log.warning("curator.startup", error=str(e))

        if self.hub.configured_names():
            try:
                await self.hub.prepare()
            except Exception as e:
                log.warning("hub.prepare_failed", error=str(e))
            if (
                self.settings.telegram_enabled
                and self.settings.telegram_bot_token
                and not self.settings.allowed_telegram_users()
            ):
                log.warning(
                    "telegram.allowlist_missing",
                    hint="set TELEGRAM_ALLOWED_USER_IDS in ~/.ophelia/.env and /start your bot",
                )

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
                user_channel=self.settings.primary_user_channel(),
                notify=self._notify_spontaneous,
            )
            tasks.append(asyncio.create_task(self.consciousness.run()))

        self.listen = LocalListenLoop(self.settings, self.agent, self.signals)
        if self.listen.available():
            tasks.append(asyncio.create_task(self.listen.run()))

        if self.settings.greet_on_start and self.hub.configured_names():
            tasks.append(asyncio.create_task(self._greet_on_start()))

        log.info(
            "ophelia.starting",
            provider=self.settings.provider,
            channels=self.hub.configured_names(),
            consciousness=self.settings.consciousness_on(),
            listen=self.listen.available(),
            curator=bool(self.curator),
            inner=bool(self.inner),
            games=bool(self.games),
        )
        try:
            await self.hub.run()
        finally:
            self.signals.terminate = True
            if self.consciousness:
                self.consciousness.stop()
            if self.listen:
                self.listen.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
