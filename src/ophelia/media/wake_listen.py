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
        # Tier A #3: real wake-word engine (openWakeWord / Porcupine). When
        # available, replaces the 2s-clip STT polling with continuous keyword
        # spotting — far less latency, cost, and battery.
        from ophelia.media.wake_engine import WakeEngine

        self.wake_engine = WakeEngine(settings)

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
        # Tier A #3: pick the wake path. Real engine if configured+importable;
        # otherwise legacy STT polling.
        use_engine = self.wake_engine.available()
        log.info("wake_listen.started", wake_word=self._wake, engine=use_engine)
        while self._running and not self.signals.terminate:
            if not self.signals.listen_enabled:
                await asyncio.sleep(1)
                continue
            if self.signals.user_talking or self.signals.agent_thinking:
                await asyncio.sleep(0.5)
                continue
            if use_engine:
                stop = asyncio.Event()
                # Set up a watcher that flips stop on state changes so the
                # engine loop can yield promptly when she starts talking or
                # autonomy is paused.
                triggered = await self._engine_wake_until(stop)
                if triggered:
                    await self._conversation(None)
            else:
                await self._poll_wake()
                await asyncio.sleep(0.3)

    async def _engine_wake_until(self, stop: asyncio.Event) -> bool:
        """Run the wake engine, stopping early when listen is disabled or she
        starts talking / thinking. Returns True if wake fired."""
        task = asyncio.create_task(self.wake_engine.listen_for_wake(stop))
        try:
            while not self.signals.terminate:
                if not self.signals.listen_enabled or self.signals.user_talking or self.signals.agent_thinking:
                    stop.set()
                    break
                if task.done():
                    break
                await asyncio.sleep(0.5)
            return await task
        except asyncio.CancelledError:
            stop.set()
            return False

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

        # Tier A #2: try local STT first (whisper.cpp on-device), fall back to
        # xAI cloud STT. A missing/unreachable local server degrades to cloud
        # transparently — the listen loop never hard-breaks.
        from ophelia.media.stt_local import local_stt_configured, transcribe_audio_local

        token: str | None = None
        heard: str | None = None
        if local_stt_configured(self.settings):
            try:
                heard = await transcribe_audio_local(snippet, settings=self.settings)
            except Exception as e:
                log.debug("wake_listen.local_stt_error", error=str(e))
        if heard is None:
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
        # Tier A #2: local-first STT for the actual utterance too.
        from ophelia.media.stt_local import local_stt_configured, transcribe_audio_local

        text: str | None = None
        if local_stt_configured(self.settings):
            try:
                text = await transcribe_audio_local(wav, settings=self.settings)
            except Exception as e:
                log.debug("wake_listen.conv_local_stt_error", error=str(e))
        if text is None:
            if not token:
                # Need an xAI bearer for cloud STT — acquire one if we skipped
                # the wake-phase cloud path entirely (local STT handled wake).
                xai = build_provider_stack(self.settings).xai_backend()
                if xai:
                    try:
                        token = await xai.bearer_fresh()
                    except Exception as e:
                        log.warning("wake_listen.conv_auth", error=str(e))
                        return
                else:
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
        # Mood → behavior (Tier A #5): cap burst length and derive TTS speed
        # from the same knobs so her voice and pacing move with her mood.
        from ophelia.mind.mood_behavior import mood_knobs

        knobs = mood_knobs(getattr(self.agent, "psyche", None))
        spoken = reply[:knobs.burst_max_chars]
        # Tier A #4: voice mind rewrites for speech (pauses, breath, mood-matched).
        voice_mind = getattr(self.agent, "voice_mind", None)
        if voice_mind is not None and voice_mind.enabled:
            try:
                spoken = await voice_mind.rewrite_for_speech(
                    spoken, psyche=self.agent.psyche, agent=self.agent
                )
            except Exception as e:
                log.debug("wake_listen.voice_mind_failed", error=str(e))
        mp3 = self.audio_dir / f"reply_{int(time.time())}.mp3"
        try:
            bearer = token if resolve_tts_provider(self.settings) == "xai" else None
            speed = None
            if self.agent.life:
                speed = self.agent.life.voice_speed(psyche=self.agent.psyche)
            out = await synthesize(
                spoken,
                mp3,
                settings=self.settings,
                xai_bearer=bearer,
                speed=speed,
            )
        except Exception as e:
            log.warning("wake_listen.tts_failed", error=str(e))
            return
        player = shutil.which("termux-media-player") or shutil.which("mpv")
        if player:
            await asyncio.create_subprocess_exec(player, str(out))

    def stop(self) -> None:
        self._running = False
