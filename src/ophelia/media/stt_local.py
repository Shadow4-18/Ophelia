"""Local STT via whisper.cpp (Tier A #2).

Today the wake-word path records 2-second clips and sends them to xAI cloud
STT to grep for "ophelia". That works but adds latency, cost, and breaks
offline. whisper.cpp / tiny.en on-device makes "Hey Ophelia" feel instant and
always-on — closer to actually hearing you.

This module talks to a local whisper.cpp OpenAI-compatible server
(`--server` mode, port 8080 by default) or falls back to the `whisper-cli`
binary for one-shot transcription. On Termux you'd run:

    pkg install whisper.cpp cmake
    # Pull tiny.en model (~75MB):
    whisper.cpp-download-ggml-model tiny.en
    whisper.cpp-server -m ~/whisper/models/ggml-tiny.en.bin -p 8080

Then in .env:
    OPHELIA_STT_PROVIDER=local
    WHISPER_SERVER_URL=http://127.0.0.1:8080/v1
    WHISPER_MODEL=tiny.en      # optional; server default otherwise

If the local server is unreachable we transparently fall back to xAI cloud
STT so a missing local server never hard-breaks the listen loop.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import structlog

if TYPE_CHECKING:
    from ophelia.config import Settings

log = structlog.get_logger()


def local_stt_configured(settings: "Settings") -> bool:
    """True if OPHELIA_STT_PROVIDER=local and a server URL or whisper-cli is set."""
    p = (settings.stt_provider or "auto").strip().lower()
    if p != "local":
        return False
    return bool(settings.whisper_server_url) or bool(shutil.which("whisper-cli"))


async def transcribe_audio_local(
    audio_path: Path,
    *,
    settings: "Settings",
) -> str | None:
    """Transcribe via local whisper.cpp server or CLI.

    Returns the transcribed text, or None to signal "fall back to cloud STT"
    (when local STT isn't configured or the call failed).
    """
    if not local_stt_configured(settings):
        return None

    # Preferred path: OpenAI-compatible whisper.cpp server.
    if settings.whisper_server_url:
        try:
            return await _transcribe_via_server(audio_path, settings)
        except Exception as e:
            log.debug("stt_local.server_failed", error=str(e))
            # Fall through to CLI, then to cloud (return None).

    # Fallback path: whisper-cli one-shot. Slower (process spawn per call)
    # but works without a running server.
    cli = shutil.which("whisper-cli")
    if cli:
        try:
            return await _transcribe_via_cli(audio_path, cli, settings)
        except Exception as e:
            log.debug("stt_local.cli_failed", error=str(e))

    return None


async def _transcribe_via_server(audio_path: Path, settings: "Settings") -> str:
    base = settings.whisper_server_url.rstrip("/")
    url = f"{base}/audio/transcriptions"
    payload: dict = {}
    if settings.whisper_model:
        payload["model"] = settings.whisper_model
    async with httpx.AsyncClient(timeout=30.0) as http:
        with audio_path.open("rb") as f:
            r = await http.post(
                url,
                files={"file": (audio_path.name, f, "audio/wav")},
                data=payload,
            )
        if r.status_code == 404:
            # Try the non-/v1 path some servers expose.
            url2 = f"{base.removesuffix('/v1')}/transcriptions"
            with audio_path.open("rb") as f:
                r = await http.post(
                    url2,
                    files={"file": (audio_path.name, f)},
                    data=payload,
                )
        r.raise_for_status()
        data = r.json()
    return (data.get("text") or "").strip()


async def _transcribe_via_cli(
    audio_path: Path, cli: str, settings: "Settings"
) -> str:
    """One-shot transcription via whisper-cli binary.

    Assumes the model path is set via WHISPER_MODEL_PATH env or the CLI's
    default search locations. Output is written to stdout in plain text
    (use -otxt); we read it back.
    """
    args = [cli, str(audio_path), "-otxt", "-of", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return out.decode("utf-8", errors="replace").strip()
