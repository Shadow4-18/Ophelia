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
from ophelia.media.voice import synthesize_speech, transcribe_audio
from ophelia.providers.router import XAIBackend, build_backend

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

        backend = build_backend(self.settings)
        if not isinstance(backend, XAIBackend):
            return
        try:
            token = await backend.bearer_fresh()
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
                system_extra="User spoke aloud near the phone (local listen mode). Be concise for TTS.",
            )
        finally:
            await self.signals.set_agent_thinking(False)

        if not reply.strip():
            return

        mp3 = self.audio_dir / f"reply_{int(time.time())}.mp3"
        try:
            await synthesize_speech(
                reply[:800],
                mp3,
                bearer=token,
                base_url=self.settings.xai_base_url,
                voice_id=self.settings.tts_voice_id,
            )
        except Exception as e:
            log.warning("listen.tts_failed", error=str(e))
            return

        player = shutil.which("termux-media-player") or shutil.which("mpv")
        if player and "termux-media-player" in player:
            await asyncio.create_subprocess_exec("termux-media-player", str(mp3))
        elif player:
            await asyncio.create_subprocess_exec(player, str(mp3))
        else:
            log.info("listen.reply_text", reply=reply[:200])

    def stop(self) -> None:
        self._running = False
