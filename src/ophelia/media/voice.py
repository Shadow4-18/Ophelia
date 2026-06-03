"""Telegram voice ↔ xAI STT/TTS."""

from __future__ import annotations

from pathlib import Path

import httpx
import structlog

log = structlog.get_logger()


async def transcribe_audio(
    audio_path: Path,
    *,
    bearer: str,
    base_url: str,
) -> str:
    url = f"{base_url.rstrip('/')}/audio/transcriptions"
    async with httpx.AsyncClient(timeout=120.0) as http:
        with audio_path.open("rb") as f:
            r = await http.post(
                url,
                headers={"Authorization": f"Bearer {bearer}"},
                files={"file": (audio_path.name, f, "audio/ogg")},
                data={"model": "grok-audio"},
            )
        if r.status_code == 404:
            # OpenAI-compatible whisper-style fallback path
            url2 = f"{base_url.rstrip('/')}/transcriptions"
            with audio_path.open("rb") as f:
                r = await http.post(
                    url2,
                    headers={"Authorization": f"Bearer {bearer}"},
                    files={"file": (audio_path.name, f)},
                    data={"model": "whisper-1"},
                )
        r.raise_for_status()
        data = r.json()
    return (data.get("text") or data.get("transcript") or "").strip()


async def synthesize_speech(
    text: str,
    out_path: Path,
    *,
    bearer: str,
    base_url: str,
    voice_id: str = "eve",
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0) as http:
        r = await http.post(
            f"{base_url.rstrip('/')}/tts",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"text": text, "voice_id": voice_id, "language": "en"},
        )
        r.raise_for_status()
        out_path.write_bytes(r.content)
    return out_path
