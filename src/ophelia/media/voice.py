"""Voice: STT (xAI) + multi-backend TTS (ElevenLabs / Kokoro / OpenAI / xAI)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from ophelia.config import Settings

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


def resolve_tts_provider(settings: Settings) -> str:
    """Resolve OPHELIA_TTS_PROVIDER, expanding 'auto' to the best configured backend."""
    p = (settings.tts_provider or "auto").strip().lower()
    if p != "auto":
        return p
    if settings.elevenlabs_api_key:
        return "elevenlabs"
    if settings.kokoro_tts_url:
        return "kokoro"
    if settings.openai_api_key:
        return "openai"
    return "xai"


def kokoro_base_url(settings: Settings) -> str:
    return (settings.kokoro_tts_url or "http://127.0.0.1:8880/v1").rstrip("/")


async def synthesize(
    text: str,
    out_path: Path,
    *,
    settings: Settings,
    xai_bearer: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
) -> Path:
    """Synthesize speech with the configured TTS backend."""
    provider = resolve_tts_provider(settings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "elevenlabs":
        return await _elevenlabs_tts(text, out_path, settings)
    if provider == "kokoro":
        return await _openai_compatible_tts(
            text,
            out_path,
            base_url=kokoro_base_url(settings),
            api_key="not-needed",
            model="kokoro",
            voice=voice or settings.kokoro_tts_voice,
            speed=speed if speed is not None else settings.kokoro_tts_speed,
        )
    if provider == "openai":
        return await _openai_compatible_tts(
            text,
            out_path,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key or "",
            model=settings.openai_tts_model,
            voice=voice or settings.openai_tts_voice,
            speed=speed,
        )
    if provider == "xai":
        if not xai_bearer:
            raise RuntimeError("TTS provider is xai but no xAI bearer token available")
        return await synthesize_speech(
            text,
            out_path,
            bearer=xai_bearer,
            base_url=settings.xai_base_url,
            voice_id=voice or settings.tts_voice_id,
        )
    raise RuntimeError(f"Unknown TTS provider: {provider}")


async def kokoro_list_voices(settings: Settings) -> list[dict[str, Any]]:
    """List voices from a Kokoro-FastAPI server."""
    url = f"{kokoro_base_url(settings)}/audio/voices"
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(url)
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict) and "voices" in data:
        return data["voices"]
    if isinstance(data, list):
        return data
    return []


async def kokoro_combine_voices(
    expression: str,
    out_path: Path,
    *,
    settings: Settings,
) -> Path:
    """Blend Kokoro voice packs and save the combined .pt tensor locally."""
    url = f"{kokoro_base_url(settings)}/audio/voices/combine"
    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(url, json=expression)
        r.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    return out_path


async def _elevenlabs_tts(text: str, out_path: Path, settings: Settings) -> Path:
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{settings.elevenlabs_voice_id}?output_format=mp3_44100_128"
    )
    async with httpx.AsyncClient(timeout=120.0) as http:
        r = await http.post(
            url,
            headers={"xi-api-key": settings.elevenlabs_api_key or ""},
            json={"text": text, "model_id": settings.elevenlabs_tts_model},
        )
        r.raise_for_status()
    out_path = out_path.with_suffix(".mp3")
    out_path.write_bytes(r.content)
    return out_path


async def _openai_compatible_tts(
    text: str,
    out_path: Path,
    *,
    base_url: str,
    api_key: str,
    model: str,
    voice: str,
    speed: float | None = None,
) -> Path:
    """OpenAI /audio/speech — Kokoro-FastAPI adds speed + inline expression parsing."""
    url = f"{base_url.rstrip('/')}/audio/speech"
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }
    if speed is not None and speed != 1.0:
        payload["speed"] = speed

    async with httpx.AsyncClient(timeout=300.0) as http:
        r = await http.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        if r.status_code in (400, 404, 422):
            # Retry without response_format / speed for minimal servers (Kokoros)
            fallback: dict[str, Any] = {"model": model, "input": text, "voice": voice}
            if speed is not None and speed != 1.0:
                fallback["speed"] = speed
            r = await http.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=fallback,
            )
        r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "wav" in ctype or r.content[:4] == b"RIFF":
        out_path = out_path.with_suffix(".wav")
    else:
        out_path = out_path.with_suffix(".mp3")
    out_path.write_bytes(r.content)
    return out_path


async def synthesize_speech(
    text: str,
    out_path: Path,
    *,
    bearer: str,
    base_url: str,
    voice_id: str = "eve",
) -> Path:
    """xAI TTS (legacy backend)."""
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
