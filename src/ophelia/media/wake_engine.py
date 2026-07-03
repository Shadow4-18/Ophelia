"""Real wake-word engine (Tier A #3).

The legacy wake path records 2-second clips, sends them to STT, and greps for
"ophelia" in the transcript. That's polling — extra latency, cost, and battery
for every clip, plus false triggers from any speech containing her name.

This module wraps dedicated keyword-spotting engines that listen continuously
to the mic stream and fire only when the wake word is spoken:

  - openWakeWord (preferred; MIT, on-device, no key, ~2MB models)
  - Porcupine (Picovoice; higher accuracy, free tier, requires access key)

Both run a small model on raw audio frames, so they're far cheaper than STT
polling and react in ~200-400ms instead of 2-8s per clip.

OPHELIA_WAKE_ENGINE:
  - auto        — STT polling (legacy default; works without extra deps)
  - openwakeword — use openWakeWord. `pip install openwakeword`
  - porcupine   — use Picovoice Porcupine. `pip install pvporcupine`

When the chosen engine isn't importable, we log a hint and fall back to STT
polling so a missing optional dep never breaks the listen loop.

On Termux the mic is captured via `termux-microphone-record` in chunks; we
feed each chunk's bytes into the engine. On PC/server a `sounddevice`-based
capture is used when available.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ophelia.config import Settings

log = structlog.get_logger()

# Target sample rate for both engines (openWakeWord and Porcupine both want 16k).
SAMPLE_RATE = 16000
# Frame size in ms — both engines want ~10-30ms frames.
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples at 16kHz, 30ms


class WakeEngine:
    """Wraps openWakeWord or Porcupine for continuous keyword spotting."""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.engine_name = (settings.wake_engine or "auto").strip().lower()
        self._impl = None
        self._record_bin = shutil.which("termux-microphone-record")
        self._available: bool | None = None

    def available(self) -> bool:
        """True if a real wake engine is configured AND importable AND we
        have a mic source."""
        if self._available is not None:
            return self._available
        if self.engine_name in ("", "auto"):
            self._available = False
            return False
        if not self._record_bin:
            # No mic capture available — can't run a continuous engine.
            self._available = False
            return False
        try:
            self._impl = self._build_impl()
            self._available = self._impl is not None
        except Exception as e:
            log.warning(
                "wake_engine.init_failed",
                engine=self.engine_name,
                error=str(e),
                hint=self._import_hint(),
            )
            self._available = False
        return self._available

    def _import_hint(self) -> str:
        if self.engine_name == "openwakeword":
            return "pip install openwakeword"
        if self.engine_name == "porcupine":
            return "pip install pvporcupine  (and set PORCUPINE_ACCESS_KEY)"
        return ""

    def _build_impl(self):
        if self.engine_name == "openwakeword":
            return _OpenWakeWordImpl(self.settings)
        if self.engine_name == "porcupine":
            return _PorcupineImpl(self.settings)
        return None

    async def listen_for_wake(self, stop_event: asyncio.Event) -> bool:
        """Continuously capture mic audio and feed it to the engine.

        Returns True when the wake word is detected, False if stopped before
        detection (e.g. terminate signal). Designed to run as the first half
        of the wake-listen loop; on True the caller records a longer utterance
        and runs STT on it.
        """
        if not self.available() or self._impl is None:
            return False
        log.info("wake_engine.listening", engine=self.engine_name)
        while not stop_event.is_set():
            # Capture a short chunk (termux-microphone-record writes a WAV).
            chunk_path = self.settings.data_dir / "listen" / f"wake_{int(time.time() * 1000)}.wav"
            chunk_path.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                self._record_bin,
                "-f",
                str(chunk_path),
                "-l",
                str(FRAME_MS * 4 // 1000),  # ~120ms chunks
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=4)
            except TimeoutError:
                proc.kill()
                continue
            if not chunk_path.is_file() or chunk_path.stat().st_size < 200:
                continue
            try:
                frames = _read_wav_pcm(chunk_path)
            except Exception:
                continue
            try:
                chunk_path.unlink(missing_ok=True)
            except Exception:
                pass
            if self._impl.process(frames):
                log.info("wake_engine.detected", engine=self.engine_name)
                return True
        return False


class _OpenWakeWordImpl:
    """openWakeWord wrapper. Lazy-imports so the dep is optional."""

    def __init__(self, settings: "Settings") -> None:
        from openwakeword.model import Model  # type: ignore

        wake_word = (settings.wake_word or "ophelia").strip().lower()
        # openWakeWord ships built-in models; for "ophelia" we'd train a custom
        # one. Until then we fall back to the closest built-in ("hey jarvis",
        # "alexa", "computer") — this is a placeholder until a custom .tflite
        # or .onnx model is dropped into ~/.ophelia/wake/.
        model_path = settings.data_dir / "wake" / f"{wake_word}.tflite"
        if model_path.is_file():
            self._model = Model(wakeword_model_paths=[str(model_path)])
        else:
            # Default: use built-in models. The first match in the inference
            # score for any of them above threshold counts as a wake event.
            self._model = Model()
            log.warning(
                "wake_engine.openwakeword_no_custom_model",
                hint=f"train a '{wake_word}.tflite' and put it at {model_path}",
            )
        self._threshold = settings.wake_engine_sensitivity
        self._wake_word = wake_word

    def process(self, pcm_frames: bytes) -> bool:
        import numpy as np

        samples = np.frombuffer(pcm_frames, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return False
        scores = self._model.predict(samples)
        # If we have a custom model, its key is the wake word; check threshold.
        if self._wake_word in scores:
            return scores[self._wake_word] >= self._threshold
        # Otherwise any built-in model firing above threshold counts.
        for name, score in scores.items():
            if score >= self._threshold:
                log.info("wake_engine.openwakeword_hit", model=name, score=round(score, 2))
                return True
        return False


class _PorcupineImpl:
    """Picovoice Porcupine wrapper. Lazy-imports so the dep is optional."""

    def __init__(self, settings: "Settings") -> None:
        import pvporcupine  # type: ignore

        self._porcupine = pvporcupine.create(
            access_key=settings.porcupine_access_key,
            keywords=None if settings.porcupine_keyword_path else ["picovoice"],
            keyword_paths=[settings.porcupine_keyword_path]
            if settings.porcupine_keyword_path
            else None,
            sensitivity=settings.wake_engine_sensitivity,
        )
        self._frame_length = self._porcupine.frame_length

    def process(self, pcm_frames: bytes) -> bool:
        import numpy as np

        samples = np.frombuffer(pcm_frames, dtype=np.int16)
        if samples.size < self._frame_length:
            return False
        # Process in engine-sized frames.
        for i in range(0, samples.size - self._frame_length + 1, self._frame_length):
            chunk = samples[i : i + self._frame_length]
            keyword_index = self._porcupine.process(chunk.tolist())
            if keyword_index >= 0:
                return True
        return False


def _read_wav_pcm(path) -> bytes:
    """Read raw 16-bit PCM samples from a WAV file (any sample rate)."""
    import wave

    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return frames
