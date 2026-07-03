"""Local speech loop on Termux — mic → STT → Ophelia → TTS (no Telegram required)."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import structlog

from ophelia.config import Settings
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.media.tts_context import tts_turn_extra
from ophelia.media.voice import resolve_tts_provider, synthesize, transcribe_audio
from ophelia.providers.router import build_provider_stack

log = structlog.get_logger()


class LocalListenLoop:
    def __init__(
        self,
        settings: Settings,
        agent: AgentLoop,
        signals: Signals,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.signals = signals
        self._running = False
        self.audio_dir = settings.data_dir / "listen"
        self._record_bin = shutil.which("termux-microphone-record")

    def available(self) -> bool:
        return bool(self._record_bin)

    async def run(self) -> None:
        if not self.available():
            log.warning("listen.unavailable", hint="pkg install termux-api + Termux:API app")
            return
        self._running = True
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        log.info("listen.started", seconds=self.settings.listen_seconds)

        while self._running and not self.signals.terminate:
            if self.signals.user_talking or self.signals.agent_thinking:
                await asyncio.sleep(1)
                continue
            if not self.signals.listen_enabled:
                await asyncio.sleep(2)
                continue

            await self._cycle()
            await asyncio.sleep(self.settings.listen_interval_seconds)

    async def _cycle(self) -> None:
        wav = self.audio_dir / f"listen_{int(time.time())}.wav"
        secs = self.settings.listen_seconds
        proc = await asyncio.create_subprocess_exec(
            self._record_bin,
            "-f",
            str(wav),
            "-l",
            str(secs),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=secs + 15)
        except TimeoutError:
            proc.kill()
            return

        if not wav.is_file() or wav.stat().st_size < 500:
            return

        # Tier A #2: prefer local whisper.cpp STT, fall back to xAI cloud.
        from ophelia.media.stt_local import local_stt_configured, transcribe_audio_local

        text: str | None = None
        token: str | None = None
        if local_stt_configured(self.settings):
            try:
                text = await transcribe_audio_local(wav, settings=self.settings)
            except Exception as e:
                log.debug("listen.local_stt_error", error=str(e))
        if text is None:
            xai = build_provider_stack(self.settings).xai_backend()
            if not xai:
                return
            try:
                token = await xai.bearer_fresh()
            except Exception as e:
                log.warning("listen.auth", error=str(e))
                return
            try:
                text = await transcribe_audio(
                    wav, bearer=token, base_url=self.settings.xai_base_url
                )
            except Exception as e:
                log.warning("listen.stt_failed", error=str(e))
                return

        if not text or len(text) < 2:
            return

        log.info("listen.heard", text=text[:80])
        await self.signals.set_agent_thinking(True)
        try:
            reply = await self.agent.run_turn(
                "listen:local",
                text,
                system_extra=(
                    "User spoke aloud near the phone (local listen mode). Be concise for TTS.\n"
                    + tts_turn_extra(self.settings, voice_reply=True)
                ),
            )
        finally:
            await self.signals.set_agent_thinking(False)

        if not reply.strip():
            return
        # Mood → behavior (Tier A #5): cap burst + mood-derived TTS speed.
        from ophelia.mind.mood_behavior import mood_knobs

        knobs = mood_knobs(getattr(self.agent, "psyche", None))
        spoken = reply[:knobs.burst_max_chars]
        # Tier A #4: voice mind rewrites for speech (pauses, breath, mood).
        voice_mind = getattr(self.agent, "voice_mind", None)
        if voice_mind is not None and voice_mind.enabled:
            try:
                spoken = await voice_mind.rewrite_for_speech(
                    spoken, psyche=self.agent.psyche, agent=self.agent
                )
            except Exception as e:
                log.debug("listen.voice_mind_failed", error=str(e))
        mp3 = self.audio_dir / f"reply_{int(time.time())}.mp3"
        try:
            bearer = token if resolve_tts_provider(self.settings) == "xai" else None
            speed = None
            if hasattr(self.agent, "life") and self.agent.life:
                speed = self.agent.life.voice_speed(psyche=self.agent.psyche)
            out = await synthesize(
                spoken,
                mp3,
                settings=self.settings,
                xai_bearer=bearer,
                speed=speed,
            )
        except Exception as e:
            log.warning("listen.tts_failed", error=str(e))
            return

        player = shutil.which("termux-media-player") or shutil.which("mpv")
        if player and "termux-media-player" in player:
            await asyncio.create_subprocess_exec("termux-media-player", str(out))
        elif player:
            await asyncio.create_subprocess_exec(player, str(out))
        else:
            log.info("listen.reply_text", reply=reply[:200])

    def stop(self) -> None:
        self._running = False
