"""Voice: STT (xAI) + multi-backend TTS (ElevenLabs / Kokoro / OpenAI / xAI)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from ophelia.config import Settings

log = structlog.get_logger()

# Split spoken text into sentence-sized chunks for lower time-to-first-audio.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|(?<=\n)\s*")

# Inline pause tags her voice prompts use: [pause:0.6s]. No TTS server she
# runs understands them (the koko server read "pause colon 0.6 is" aloud),
# so they are turned into real silence here.
_PAUSE_TAG = re.compile(r"\[\s*pause\s*:\s*([0-9]*\.?[0-9]+)\s*s?\s*\]", re.IGNORECASE)
_MAX_PAUSE = 3.0


def split_pauses(text: str) -> list[tuple[str, float]]:
    """[(spoken segment, seconds of silence after it)]."""
    out: list[tuple[str, float]] = []
    pos = 0
    for m in _PAUSE_TAG.finditer(text or ""):
        seg = text[pos:m.start()].strip()
        secs = min(_MAX_PAUSE, float(m.group(1)))
        if seg:
            out.append((seg, secs))
        elif out:
            seg_prev, p = out[-1]
            out[-1] = (seg_prev, min(_MAX_PAUSE, p + secs))
        pos = m.end()
    tail = (text or "")[pos:].strip()
    if tail:
        out.append((tail, 0.0))
    return out


_MAX_SPLITS = 3  # each split is another call to the phone's TTS server
_MIN_SPLIT_S = 0.5  # shorter beats: a comma does it


def merge_pauses(segments: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Keep only the longest few real pauses as silences; the rest join
    their neighbours with a comma (Kokoro breathes there anyway)."""
    keep = sorted((p for _, p in segments[:-1] if p >= _MIN_SPLIT_S), reverse=True)[:_MAX_SPLITS]
    out: list[tuple[str, float]] = []
    buf = ""
    for seg, pause in segments:
        buf = f"{buf.rstrip(' ,')}, {seg}" if buf and not buf.endswith((".", "!", "?", "…")) \
            else (f"{buf} {seg}" if buf else seg)
        if pause in keep:
            keep.remove(pause)
            out.append((buf, pause))
            buf = ""
    if buf:
        out.append((buf, 0.0))
    return out


def strip_pauses(text: str, replacement: str = " ") -> str:
    """Pause tags out of the text (for engines that can't take them). After
    punctuation the punctuation already makes the beat, so the tag just goes."""
    src = text or ""

    def sub(m: re.Match) -> str:
        before = src[:m.start()].rstrip()
        return " " if before.endswith((".", "!", "?", "…", ",", ";", ":")) or not before \
            else replacement

    out = _PAUSE_TAG.sub(sub, src)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _strip_id3(data: bytes) -> bytes:
    if data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[10 + size:]
    return data


_L3_KBPS = {True: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
            False: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}


def _drop_info_frame(data: bytes) -> bytes:
    """Remove a leading Xing/Info/VBRI frame: it states the length of this
    clip alone, and some players stop there after clips are joined."""
    data = _strip_id3(data)
    if len(data) < 4 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return data
    version = (data[1] >> 3) & 3
    br_idx, sr_idx, pad = data[2] >> 4, (data[2] >> 2) & 3, (data[2] >> 1) & 1
    if version == 1 or sr_idx == 3 or br_idx in (0, 15):
        return data
    mpeg1 = version == 3
    rate = _MP3_RATES[version][sr_idx]
    length = (144 if mpeg1 else 72) * _L3_KBPS[mpeg1][br_idx] * 1000 // rate + pad
    head = data[:min(length, 64)]
    if any(tag in head for tag in (b"Xing", b"Info", b"VBRI")):
        return data[length:]
    return data


def _mp3_format(data: bytes) -> tuple[int, int, int] | None:
    """(version bits, sample-rate index, channel mode) of the first MPEG
    audio frame, or None if this isn't MP3."""
    data = _strip_id3(data)
    for i in range(min(len(data) - 4, 4096)):
        b1 = data[i + 1]
        if data[i] == 0xFF and (b1 & 0xE0) == 0xE0:
            version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
            sr_idx = (data[i + 2] >> 2) & 3
            if version != 1 and layer == 1 and sr_idx != 3:  # Layer III
                return version, sr_idx, data[i + 3] >> 6
    return None


def _silent_mp3(fmt: tuple[int, int, int], seconds: float) -> bytes:
    """Silent Layer III frames in the given format: header, all-zero side
    info (no audio data) and padding, repeated for ``seconds``."""
    version, sr_idx, mode = fmt
    rate = _MP3_RATES[version][sr_idx]
    mpeg1 = version == 3
    bitrate = 32000 if mpeg1 else 8000  # bitrate index 1
    frame_len = (144 if mpeg1 else 72) * bitrate // rate
    samples = 1152 if mpeg1 else 576
    header = bytes([0xFF, 0xE0 | (version << 3) | (1 << 1) | 1,
                    (1 << 4) | (sr_idx << 2), mode << 6])
    frame = header + b"\x00" * (frame_len - 4)
    return frame * max(1, round(seconds * rate / samples))


def _join_mp3s(parts: list[tuple[Path, float]], out_path: Path) -> Path | None:
    """Concatenate MP3 clips with silent frames between them (MP3 streams
    concatenate frame by frame). None if they aren't all MP3."""
    chunks: list[bytes] = []
    for i, (path, pause) in enumerate(parts):
        data = path.read_bytes()
        fmt = _mp3_format(data)
        if fmt is None:
            return None
        chunks.append(_drop_info_frame(data))
        if pause > 0:
            chunks.append(_silent_mp3(fmt, pause))
    out_path = out_path.with_suffix(".mp3")
    out_path.write_bytes(b"".join(chunks))
    return out_path


def _join_wavs(parts: list[tuple[Path, float]], out_path: Path) -> Path | None:
    """Concatenate WAV clips with silence between them. None if the clips
    aren't all WAV with the same format."""
    import wave

    params = None
    frames: list[bytes] = []
    try:
        for path, pause in parts:
            with wave.open(str(path), "rb") as w:
                p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = p
                elif p != params:
                    return None
                frames.append(w.readframes(w.getnframes()))
            if pause > 0:
                ch, width, rate = params
                frames.append(b"\x00" * (int(rate * pause) * ch * width))
    except (wave.Error, EOFError, OSError):
        return None
    if params is None:
        return None
    out_path = out_path.with_suffix(".wav")
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        for chunk in frames:
            w.writeframes(chunk)
    return out_path


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


def split_speech_chunks(text: str, *, max_chars: int = 220) -> list[str]:
    """Split text into speakable chunks for lower time-to-first-audio.

    Prefer sentence boundaries; fall back to comma/length cuts so Kokoro
    (and other clip-based TTS) can start playing before a long monologue
    finishes synthesizing.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    # Strip markdown-ish noise that sounds bad spoken.
    raw = re.sub(r"[*_`#]+", "", raw)
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(raw) if p and p.strip()]
    if not parts:
        parts = [raw]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        candidate = f"{buf} {part}".strip() if buf else part
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(part) <= max_chars:
            buf = part
        else:
            # Hard-wrap long clauses.
            while len(part) > max_chars:
                cut = part.rfind(",", 0, max_chars)
                if cut < max_chars // 3:
                    cut = max_chars
                chunks.append(part[:cut].strip())
                part = part[cut:].lstrip(" ,")
            buf = part
    if buf:
        chunks.append(buf)
    return chunks


async def synthesize(
    text: str,
    out_path: Path,
    *,
    settings: Settings,
    xai_bearer: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
) -> Path:
    """Synthesize speech with the configured TTS backend. ``[pause:Xs]`` tags
    become real silence (Kokoro/OpenAI) or a spoken beat ("…") elsewhere;
    they are never read out."""
    provider = resolve_tts_provider(settings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("tts.synthesize", provider=provider,
             voice=voice or _default_voice(provider, settings),
             speed=speed, chars=len(text or ""), pauses=len(_PAUSE_TAG.findall(text or "")))
    if _PAUSE_TAG.search(text or ""):
        if provider in ("kokoro", "openai"):
            joined = await _synthesize_with_pauses(
                text, out_path, settings=settings, voice=voice, speed=speed)
            if joined is not None:
                return joined
            text = strip_pauses(text, ", ")
        else:
            text = strip_pauses(text, "… ")
    if provider == "elevenlabs":
        return await _elevenlabs_tts(text, out_path, settings)
    if provider == "kokoro":
        from ophelia.media.kokoro_mix import resolve_kokoro_voice

        raw_voice = voice or settings.kokoro_tts_voice
        return await _openai_compatible_tts(
            text,
            out_path,
            base_url=kokoro_base_url(settings),
            api_key="not-needed",
            model="kokoro",
            voice=resolve_kokoro_voice(raw_voice, settings=settings),
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


def _default_voice(provider: str, settings: Settings) -> str:
    return str({
        "kokoro": getattr(settings, "kokoro_tts_voice", ""),
        "openai": getattr(settings, "openai_tts_voice", ""),
        "elevenlabs": getattr(settings, "elevenlabs_voice_id", ""),
        "xai": getattr(settings, "tts_voice_id", ""),
    }.get(provider, ""))


def _openai_tts_args(provider: str, settings: Settings, voice: str | None,
                     speed: float | None) -> dict[str, Any]:
    if provider == "kokoro":
        from ophelia.media.kokoro_mix import resolve_kokoro_voice

        return dict(base_url=kokoro_base_url(settings), api_key="not-needed", model="kokoro",
                    voice=resolve_kokoro_voice(voice or settings.kokoro_tts_voice,
                                               settings=settings),
                    speed=speed if speed is not None else settings.kokoro_tts_speed)
    return dict(base_url=settings.openai_base_url, api_key=settings.openai_api_key or "",
                model=settings.openai_tts_model, voice=voice or settings.openai_tts_voice,
                speed=speed)


async def _synthesize_with_pauses(
    text: str, out_path: Path, *, settings: Settings, voice: str | None, speed: float | None,
) -> Path | None:
    """Each segment synthesized on its own, joined with the tagged silences.
    None when the clips can't be joined (the caller then speaks the text
    without the tags)."""
    provider = resolve_tts_provider(settings)
    segments = merge_pauses(split_pauses(text))
    if not segments:
        return None
    args = _openai_tts_args(provider, settings, voice, speed)
    tmp_dir = out_path.parent / f".{out_path.stem}_parts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        parts: list[tuple[Path, float]] = []
        for i, (seg, pause) in enumerate(segments):
            clip = await _openai_compatible_tts(seg, tmp_dir / f"part{i:02d}.mp3", **args)
            parts.append((clip, pause))
        # MP3 keeps it a voice note on Telegram; WAV-only servers still work.
        joined = _join_mp3s(parts, out_path) or _join_wavs(parts, out_path)
        if joined is None:
            log.info("tts.pauses_unjoinable", provider=provider)
        return joined
    except Exception as e:
        log.warning("tts.pauses_failed", provider=provider, error=str(e)[:200])
        return None
    finally:
        for f in tmp_dir.glob("*"):
            f.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


async def synthesize_streaming(
    text: str,
    out_dir: Path,
    *,
    settings: Settings,
    xai_bearer: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
    max_chars: int = 220,
) -> AsyncIterator[Path]:
    """Yield per-sentence audio files as soon as each chunk is ready.

    Callers (workstation UI, listen loop) can start playback on the first
    yield instead of waiting for the full reply to finish TTS.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_speech_chunks(text, max_chars=max_chars)
    if not chunks:
        return
    for i, chunk in enumerate(chunks):
        path = out_dir / f"chunk_{i:02d}.mp3"
        yield await synthesize(
            chunk,
            path,
            settings=settings,
            xai_bearer=xai_bearer,
            voice=voice,
            speed=speed,
        )


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
    """Blend Kokoro voice packs with L2 renorm and save a ``.pt`` locally.

    Prefers Ophelia's local baker (correct magnitude). Falls back to the
    Kokoro-FastAPI ``/audio/voices/combine`` endpoint only if local baking
    fails (e.g. offline with no cached voices.bin) — that path is known to
    omit L2 renormalization and can sound muffled/peaky.
    """
    from ophelia.media.kokoro_mix import bake_voice_mix

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        return bake_voice_mix(expression, out_path, settings=settings)
    except Exception as e:
        log.warning(
            "kokoro_combine.local_bake_failed",
            error=str(e),
            hint="falling back to Kokoro-FastAPI /audio/voices/combine",
        )

    url = f"{kokoro_base_url(settings)}/audio/voices/combine"
    async with httpx.AsyncClient(timeout=120.0) as http:
        # FastAPI combine often rejects weighted ``name(w)`` forms — expand
        # integer weights into repeated names when needed by the server.
        r = await http.post(url, json=expression)
        if r.status_code >= 400 and "(" in expression:
            # af_bella(2)+af_sky(1) -> af_bella+af_bella+af_sky (approx)
            from ophelia.media.kokoro_mix import parse_mix_expression

            try:
                tokens = parse_mix_expression(expression)
                expanded: list[str] = []
                for name, weight, op in tokens:
                    reps = max(1, int(round(abs(weight))))
                    piece = "+".join([name] * reps)
                    if not expanded:
                        expanded.append(piece)
                    else:
                        expanded.append(f"{op}{piece}" if op == "-" else piece)
                # join: first piece, then +rest (subtract already prefixed)
                flat = expanded[0]
                for piece in expanded[1:]:
                    if piece.startswith("-"):
                        flat += piece
                    else:
                        flat += "+" + piece
                r = await http.post(url, json=flat)
            except Exception:
                pass
        r.raise_for_status()
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
    response_format: str = "mp3",
) -> Path:
    """OpenAI /audio/speech (Kokoro servers speak the same API)."""
    url = f"{base_url.rstrip('/')}/audio/speech"
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": response_format,
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
    if r.content[:4] == b"RIFF" or ("wav" in ctype and r.content[:3] != b"ID3"):
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
