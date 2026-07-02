"""Wake-word triggered listen loop — ears that wait for her name."""

from __future__ import annotations

import asyncio
import audioop
import shutil
import time
import wave
from pathlib import Path

import structlog

from ophelia.config import Settings
from ophelia.core.agent_loop import AgentLoop
from ophelia.core.signals import Signals
from ophelia.media.tts_context import tts_turn_extra
from ophelia.media.voice import resolve_tts_provider, synthesize, transcribe_audio
from ophelia.providers.router import build_provider_stack

log = structlog.get_logger()


def _wav_rms(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            frames = w.readframes(w.getnframes())
            if not frames:
                return 0.0
            return float(audioop.rms(frames, w.getsampwidth()))
    except Exception:
        return 0.0


class WakeWordListenLoop:
    """Listen for wake word, then run a full STT → agent → TTS cycle."""

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
        self._wake = (settings.wake_word or "ophelia").strip().lower()

    def available(self) -> bool:
        return (
            self.settings.wake_word_enabled
            and bool(self._record_bin)
            and bool(self._wake)
        )

    async def run(self) -> None:
        if not self.available():
            log.warning("wake_listen.unavailable")
            return
        self._running = True
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        log.info("wake_listen.started", wake_word=self._wake)
        while self._running and not self.signals.terminate:
            if not self.signals.listen_enabled:
                await asyncio.sleep(1)
                continue
            if self.signals.user_talking or self.signals.agent_thinking:
                await asyncio.sleep(0.5)
                continue
            await self._poll_wake()
            await asyncio.sleep(0.3)

    async def _poll_wake(self) -> None:
        snippet = self.audio_dir / f"wake_{int(time.time())}.wav"
        proc = await asyncio.create_subprocess_exec(
            self._record_bin,
            "-f",
            str(snippet),
            "-l",
            "2",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=8)
        except TimeoutError:
            proc.kill()
            return
        if not snippet.is_file() or snippet.stat().st_size < 300:
            return
        if _wav_rms(snippet) < self.settings.wake_word_rms_threshold:
            return

        xai = build_provider_stack(self.settings).xai_backend()
        if not xai:
            return
        try:
            token = await xai.bearer_fresh()
        except Exception as e:
            log.debug("wake_listen.auth", error=str(e))
            return
        try:
            heard = await transcribe_audio(
                snippet, bearer=token, base_url=self.settings.xai_base_url
            )
        except Exception as e:
            log.debug("wake_listen.stt_skip", error=str(e))
            return
        if not heard or self._wake not in heard.lower():
            return

        log.info("wake_listen.triggered", heard=heard[:80])
        await self._conversation(token)

    async def _conversation(self, token: str) -> None:
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
        try:
            text = await transcribe_audio(
                wav, bearer=token, base_url=self.settings.xai_base_url
            )
        except Exception as e:
            log.warning("wake_listen.stt_failed", error=str(e))
            return
        if not text or len(text) < 2:
            return
        # Strip wake word prefix if echoed
        low = text.lower()
        if low.startswith(self._wake):
            text = text[len(self._wake) :].lstrip(" ,.:;-")
        if len(text) < 2:
            return

        log.info("wake_listen.heard", text=text[:80])
        await self.signals.set_agent_thinking(True)
        try:
            reply = await self.agent.run_turn(
                "listen:wake",
                text,
                system_extra=(
                    "Owner spoke aloud and invoked you by name. Respond naturally.\n"
                    + tts_turn_extra(self.settings, voice_reply=True)
                ),
            )
        finally:
            await self.signals.set_agent_thinking(False)
        if not reply.strip():
            return
        mp3 = self.audio_dir / f"reply_{int(time.time())}.mp3"
        try:
            bearer = token if resolve_tts_provider(self.settings) == "xai" else None
            out = await synthesize(
                reply[:800],
                mp3,
                settings=self.settings,
                xai_bearer=bearer,
            )
        except Exception as e:
            log.warning("wake_listen.tts_failed", error=str(e))
            return
        player = shutil.which("termux-media-player") or shutil.which("mpv")
        if player:
            await asyncio.create_subprocess_exec(player, str(out))

    def stop(self) -> None:
        self._running = False
