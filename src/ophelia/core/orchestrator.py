from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import structlog

from ophelia.android.factory import build_android_body
from ophelia.android.games import GameStore
from ophelia.android.harden import HealthCheckLoop
from ophelia.android.vision import ScreenVision
from ophelia.channels.hub import ChannelHub
from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.media.listen_loop import LocalListenLoop
from ophelia.media.wake_listen import WakeWordListenLoop
from ophelia.memory.curator import MemoryCurator
from ophelia.memory.honcho_client import HonchoClient, load_honcho_config
from ophelia.memory.store import MemoryStore
from ophelia.mind.alarms import AlarmLoop
from ophelia.mind.ambient_commentary import AmbientCommentaryLoop
from ophelia.mind.consciousness import ConsciousnessLoop
from ophelia.mind.director import Director
from ophelia.mind.dream import DreamLoop
from ophelia.mind.drives import DriveState
from ophelia.mind.goals import GoalStore
from ophelia.mind.humor_tracker import HumorTracker
from ophelia.mind.initiative import InitiativeGovernor
from ophelia.mind.inner_log import InnerMonologue
from ophelia.mind.life_context import LifeContext
from ophelia.mind.presence import PresenceSignals
from ophelia.mind.psyche import PsycheState
from ophelia.mind.schedule_learner import ScheduleLearner
from ophelia.providers.errors import api_error_detail
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
        self._ollama_proc: asyncio.subprocess.Process | None = None
        self._ollama_log = None
        self._kokoro_proc: asyncio.subprocess.Process | None = None
        self._kokoro_log = None
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
            goals=self.goals,
            memory=self.memory,
            psyche=self.psyche,
        )
        self.tools._drives_ref = self.drives
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
        # send_message_to_guest tool: cross-platform DM to a specific user.
        self.tools.guest_sender = self.hub.send_to_user
        self.inner = (
            InnerMonologue(mirror_telegram=settings.inner_mirror_telegram)
            if settings.inner_log_enabled
            else None
        )
        if self.inner:
            self.inner.notify = self._notify_inner
            self.signals.inner_mirror = settings.inner_mirror_telegram
            self.tools.inner = self.inner
        self.consciousness: ConsciousnessLoop | None = None
        self.dream: DreamLoop | None = None
        self.listen: LocalListenLoop | None = None
        self.wake_listen: WakeWordListenLoop | None = None
        self.life = LifeContext(settings, self.signals)
        # Tier B #6: learn owner schedule from observed Telegram activity.
        self.schedule_learner = ScheduleLearner(self.memory, settings)
        self.life.schedule_learner = self.schedule_learner
        # Tier B #7: BT / router / last-seen presence signals.
        self.presence = PresenceSignals(settings)
        self.life.presence_signals = self.presence
        # Tier C #13: curator reconciliation needs the authoritative context.
        if self.curator:
            self.curator.life = self.life
        # Tier A #4: voice mind — speech-first rewrite before TTS.
        from ophelia.mind.voice_mind import VoiceMind

        self.voice_mind = VoiceMind(settings)
        self.agent.voice_mind = self.voice_mind
        # Tier A #1: director — fast decision layer over the ensemble.
        self.director = Director(
            settings,
            agent=self.agent,
            psyche=self.psyche,
            drives=self.drives,
        )
        self.agent.director = self.director
        self.humor = HumorTracker(self.memory)
        self.agent.life = self.life
        self.agent.humor = self.humor
        self.humor.bind_agent(self.agent)
        self.alarms: AlarmLoop | None = None
        self.ambient: AmbientCommentaryLoop | None = None

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

    async def _heartbeat_loop(self) -> None:
        """Write a heartbeat file every 30s for `ophelia status` / external monitors."""
        import json as _json
        import time as _t

        hb = OPHELIA_HOME / "data" / "heartbeat.json"
        hb.parent.mkdir(parents=True, exist_ok=True)
        while not self.signals.terminate:
            try:
                age = _t.time() - self.signals.last_user_message_at
                data = {
                    "ts": _t.time(),
                    "paused": self.signals.autonomy_paused,
                    "mood": self.psyche.mood.label,
                    "valence": round(self.psyche.mood.valence, 2),
                    "arousal": round(self.psyche.mood.arousal, 2),
                    "drives": {
                        "social": round(self.drives.social, 2),
                        "curiosity": round(self.drives.curiosity, 2),
                        "boredom": round(self.drives.boredom, 2),
                        "agency": round(self.drives.agency, 2),
                        "expressiveness": round(self.drives.expressiveness, 2),
                    },
                    "pressure": round(self.drives.initiative_pressure(), 2),
                    "last_user_msg_ago": round(age, 0),
                    "channels": self.hub.configured_names(),
                    "consciousness": self.settings.consciousness_on(),
                    "dream": self.settings.dream_enabled,
                }
                hb.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                log.warning("heartbeat.failed", error=str(e))
            await asyncio.sleep(30)

    async def _pause_poll_loop(self) -> None:
        """Honor `ophelia pause` / `ophelia resume` control flags from CLI."""
        flag = OPHELIA_HOME / "data" / "pause.flag"
        was_paused = False
        while not self.signals.terminate:
            try:
                if flag.is_file() and not self.signals.autonomy_paused:
                    self.signals.autonomy_paused = True
                    log.info("autonomy.paused", source="cli_flag")
                elif not flag.is_file() and self.signals.autonomy_paused and was_paused:
                    self.signals.autonomy_paused = False
                    log.info("autonomy.resumed", source="cli_flag")
                was_paused = self.signals.autonomy_paused
            except Exception:
                pass
            await asyncio.sleep(5)

    async def _ensure_ollama_running(self) -> None:
        """Start `ollama serve` if Ophelia wants Ollama and it isn't up.

        On Termux, Ollama isn't a system service, so it must be launched each
        session. Rather than force the user to remember `ollama serve` before
        `ophelia run`, spawn it ourselves (detached) when:
          - autostart is enabled (auto-on under Termux, off elsewhere), AND
          - the `ollama` binary is on PATH, AND
          - Ollama is plausibly wanted (provider is ollama/auto, or any
            OLLAMA_*_MODEL is set), AND
          - it isn't already reachable.
        Then poll for a few seconds until the API responds.
        """
        from ophelia.providers.router import _ollama_reachable

        s = self.settings
        if not s.ollama_autostart_enabled():
            return
        if not shutil.which("ollama"):
            return
        if _ollama_reachable(s):
            return  # already up (e.g. system service on desktop)

        wanted = (
            (s.provider or "auto").strip().lower() in ("ollama", "auto")
            or any(
                (getattr(s, f"provider_{r}", None) or "").strip().lower() == "ollama"
                for r in ("chat", "consciousness", "vision", "curator", "image", "video")
            )
            or bool(s.ollama_vision_model)
            or bool(s.ollama_consciousness_model)
            or bool(s.ollama_curator_model)
            or bool(s.ollama_image_model)
        )
        if not wanted:
            return

        log.info("ollama.autostart", reason="not reachable, ollama wanted")
        log_path = OPHELIA_HOME / "ollama.log"
        # Keep models resident so infrequent roles (vision) don't reload from
        # flash on every call. Default 5m is too short; pass our keep-alive as
        # OLLAMA_KEEP_ALIVE so it applies to every endpoint Ollama serves.
        env = dict(os.environ)
        env["OLLAMA_KEEP_ALIVE"] = s.ollama_keep_alive
        try:
            self._ollama_log = open(log_path, "ab")  # kept open for child lifetime
            proc = await asyncio.create_subprocess_exec(
                "ollama", "serve",
                stdout=self._ollama_log,
                stderr=self._ollama_log,
                env=env,
                start_new_session=True,  # detach: survives Ophelia exit
            )
        except Exception as e:
            log.warning("ollama.autostart_failed", error=str(e))
            return
        self._ollama_proc = proc

        # Poll until the API is up (model load happens on first request).
        for _ in range(20):
            await asyncio.sleep(0.5)
            if _ollama_reachable(s):
                log.info("ollama.autostart_up", pid=proc.pid)
                return
        log.warning(
            "ollama.autostart_timeout",
            hint=f"ollama may still be starting — see {log_path}",
        )

    async def _ensure_kokoro_running(self) -> None:
        """Start `koko openai` if Kokoro TTS is configured and the server is down.

        On Termux, Kokoros often runs inside proot Ubuntu. When autostart is
        enabled we spawn it ourselves (detached) when:
          - autostart is enabled (auto-on under Termux, off elsewhere), AND
          - Kokoro is the active TTS provider, AND
          - the server is not already reachable, AND
          - we can resolve a koko binary or autostart command.
        """
        from ophelia.media.kokoro_server import (
            describe_kokoro_autostart_hint,
            kokoro_reachable,
            kokoro_wanted,
            resolve_kokoro_autostart,
        )

        s = self.settings
        if not s.kokoro_autostart_enabled():
            return
        if not kokoro_wanted(s):
            return
        if await kokoro_reachable(s):
            return

        resolved = resolve_kokoro_autostart(s)
        if not resolved:
            log.warning(
                "kokoro.autostart_skip",
                reason="no koko binary found",
                hint=describe_kokoro_autostart_hint(s),
            )
            return

        argv, cwd, mode = resolved
        log.info("kokoro.autostart", reason="not reachable, kokoro wanted", mode=mode)
        log_path = OPHELIA_HOME / "kokoro.log"
        env = dict(os.environ)
        try:
            self._kokoro_log = open(log_path, "ab")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=self._kokoro_log,
                stderr=self._kokoro_log,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except Exception as e:
            log.warning("kokoro.autostart_failed", error=str(e))
            return
        self._kokoro_proc = proc

        for _ in range(40):
            await asyncio.sleep(0.5)
            if await kokoro_reachable(s):
                log.info("kokoro.autostart_up", pid=proc.pid, mode=mode)
                return
        log.warning(
            "kokoro.autostart_timeout",
            hint=f"kokoro may still be starting — see {log_path}",
        )

    async def _validate_models_at_startup(self) -> None:
        """Ping each chat-style role's model once. Warn loudly on failure.

        A bad model name (typo, wrong version) would otherwise fail every turn
        with a cryptic 400. Catching it here gives the user an actionable hint
        before any real work starts.
        """
        roles_to_check = ("chat", "consciousness", "curator", "vision")
        for role in roles_to_check:
            try:
                ok, msg = await self.stack.check(role)
            except Exception as e:
                from ophelia.providers.errors import api_error_detail

                log.error(
                    "startup.model_check_error",
                    role=role,
                    provider=self.stack.name(role),  # type: ignore[arg-type]
                    model=self.stack.model(role),  # type: ignore[arg-type]
                    error=api_error_detail(e),
                )
                continue
            if not ok:
                log.error(
                    "startup.model_check_failed",
                    role=role,
                    provider=self.stack.name(role),  # type: ignore[arg-type]
                    model=self.stack.model(role),  # type: ignore[arg-type]
                    detail=msg,
                )

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
            log.warning("greet.failed", error=api_error_detail(e))

    async def _notify_spontaneous(self, text: str) -> None:
        await self.hub.broadcast_proactive(text)

    async def _notify_spontaneous_voice(self, text: str) -> None:
        await self.hub.broadcast_proactive_voice(text)

    async def _notify_spontaneous_media(
        self, paths: list, *, caption: str = ""
    ) -> None:
        await self.hub.broadcast_proactive_media(paths, caption=caption)

    async def _notify_inner(self, text: str) -> None:
        from ophelia.channels.proactive_filter import is_outreach_junk

        if is_outreach_junk(text):
            return
        await self.hub.mirror_inner_thought(text)
        if self.settings.inner_mirror_telegram or self.signals.inner_mirror:
            await self.hub.broadcast_proactive(text)

    async def _notify_inner_mirror(self, text: str) -> None:
        await self._notify_inner(text)

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

        # Validate that each configured chat-role model is actually accepted by
        # its provider. A bad model name (e.g. a typo) would otherwise fail
        # every single turn with a cryptic 400. Warn loudly here instead.
        await self._ensure_ollama_running()
        await self._ensure_kokoro_running()
        await self._validate_models_at_startup()

        # Preload the Ollama vision model (if it's the vision provider) so the
        # first photo a user sends isn't a multi-second cold load. Runs in the
        # background — never blocks startup.
        try:
            from ophelia.media.vision_input import warmup_vision
            asyncio.create_task(warmup_vision(self.settings, stack=self.stack))
        except Exception as e:
            log.warning("vision.warmup_skip", error=str(e))

        tasks: list[asyncio.Task] = [
            asyncio.create_task(self._oauth_refresh_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._pause_poll_loop()),
        ]

        # Tier C #12: Android kill-switch health check. Runs every 10min on
        # Termux and re-applies the wake-lock / boot script if they vanish.
        # No-op off Termux.
        self.health_check = HealthCheckLoop(self.settings, self.signals)
        tasks.append(asyncio.create_task(self.health_check.run()))

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
                notify_media=self._notify_spontaneous_media,
                notify_voice=self._notify_spontaneous_voice,
                action_cooldown_seconds=self.settings.tick_action_cooldown_seconds,
                idle_nudge_rotate=self.settings.tick_idle_nudge_rotate,
                life=self.life,
                settings=self.settings,
                humor=self.humor,
                director=self.director,
            )
            tasks.append(asyncio.create_task(self.consciousness.run()))

        if self.settings.dream_enabled:
            self.dream = DreamLoop(
                self.agent,
                self.memory,
                self.signals,
                self.inner,
                interval_hours=self.settings.dream_interval_hours,
                notify=self._notify_spontaneous,
            )
            tasks.append(asyncio.create_task(self.dream.run()))

        self.wake_listen = WakeWordListenLoop(self.settings, self.agent, self.signals)
        if self.wake_listen.available():
            tasks.append(asyncio.create_task(self.wake_listen.run()))
        else:
            self.listen = LocalListenLoop(self.settings, self.agent, self.signals)
            if self.listen.available():
                tasks.append(asyncio.create_task(self.listen.run()))

        if self.settings.alarms.strip():
            self.alarms = AlarmLoop(
                self.settings,
                self.agent,
                self.signals,
                self.life,
                notify_text=self._notify_spontaneous,
                notify_voice=self._notify_spontaneous_voice,
            )
            tasks.append(asyncio.create_task(self.alarms.run()))

        if self.vision and self.settings.ambient_commentary_enabled:
            self.ambient = AmbientCommentaryLoop(
                self.settings,
                self.agent,
                self.signals,
                self.life,
                self.governor,
                self.vision,
                notify=self._notify_spontaneous,
            )
            tasks.append(asyncio.create_task(self.ambient.run()))

        if self.settings.greet_on_start and self.hub.configured_names():
            tasks.append(asyncio.create_task(self._greet_on_start()))

        log.info(
            "ophelia.starting",
            provider=self.settings.provider,
            channels=self.hub.configured_names(),
            consciousness=self.settings.consciousness_on(),
            dream=self.settings.dream_enabled,
            listen=self.wake_listen.available()
            if self.wake_listen
            else bool(self.listen and self.listen.available()),
            curator=bool(self.curator),
            inner=bool(self.inner),
            games=bool(self.games),
        )
        # Clean shutdown on SIGTERM/SIGINT (e.g. systemd, reboot) so psyche/drives flush.
        import signal as _signal

        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except (NotImplementedError, RuntimeError):
                # Windows/unsupported — KeyboardInterrupt still works.
                pass
        try:
            await self.hub.run()
        finally:
            self.signals.terminate = True
            if self.consciousness:
                self.consciousness.stop()
            if self.dream:
                self.dream.stop()
            if self.listen:
                self.listen.stop()
            if self.wake_listen:
                self.wake_listen.stop()
            if self.alarms:
                self.alarms.stop()
            if self.ambient:
                self.ambient.stop()
            if self.health_check:
                self.health_check.stop()
            # Final flush of psyche/drives.
            try:
                await self.memory.save_psyche(self.psyche)
                await self.memory.save_drives(self.drives)
            except Exception:
                pass
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _request_shutdown(self) -> None:
        log.info("ophelia.shutdown_requested")
        self.signals.terminate = True
