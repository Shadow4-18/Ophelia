"""Pause tags in her voice text become silence; they're never read out
("Pause colon 0. 6 is" in a voice note)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ophelia.media import voice

_FMT = (2, 1, 3)  # MPEG-2, 24 kHz, mono: what Kokoro returns


def _clip(seconds: float) -> bytes:
    return voice._silent_mp3(_FMT, seconds)


def _settings(provider):
    return SimpleNamespace(tts_provider=provider, kokoro_tts_url="http://k/v1",
                           kokoro_tts_voice="af_bella", kokoro_tts_speed=1.0,
                           kokoro_voices_dir=None, openai_base_url="http://o/v1",
                           openai_api_key="k", openai_tts_model="tts-1",
                           openai_tts_voice="nova", elevenlabs_api_key="e",
                           elevenlabs_voice_id="v", elevenlabs_tts_model="m",
                           xai_base_url="http://x", tts_voice_id="ara")


def test_split_and_strip():
    text = "Your phone's been showing five o'clock. [pause:0.6s] Either time broke [pause: 1.0s]" \
           " [pause:3.5s] or you did."
    assert voice.split_pauses(text) == [
        ("Your phone's been showing five o'clock.", 0.6), ("Either time broke", 3.0),
        ("or you did.", 0.0)]
    assert voice.strip_pauses(text, "… ") == ("Your phone's been showing five o'clock. "
                                               "Either time broke … … or you did.")


@pytest.mark.asyncio
async def test_kokoro_speaks_segments_and_joins_with_silence(tmp_path, monkeypatch):
    calls = []

    async def tts(text, out_path, **kw):
        calls.append(text)
        out_path.write_bytes(_clip(0.5))
        return out_path

    monkeypatch.setattr(voice, "_openai_compatible_tts", tts)
    monkeypatch.setattr("ophelia.media.kokoro_mix.resolve_kokoro_voice",
                        lambda v, settings=None: v)
    out = await voice.synthesize("Either time broke. [pause:0.6s] Or you did.",
                                 tmp_path / "v.mp3", settings=_settings("kokoro"))
    assert calls == ["Either time broke.", "Or you did."]  # never the tag itself
    assert out.suffix == ".mp3"  # stays a voice note on Telegram
    data = out.read_bytes()
    assert len(data) > 2 * len(_clip(0.5))  # two clips plus the silence
    assert not list(tmp_path.glob(".*_parts"))  # temp clips cleaned up


@pytest.mark.asyncio
async def test_unjoinable_clips_fall_back_to_untagged_text(tmp_path, monkeypatch):
    calls = []

    async def tts(text, out_path, **kw):
        calls.append(text)
        out_path.write_bytes(b"not audio we can splice")
        return out_path

    monkeypatch.setattr(voice, "_openai_compatible_tts", tts)
    monkeypatch.setattr("ophelia.media.kokoro_mix.resolve_kokoro_voice",
                        lambda v, settings=None: v)
    await voice.synthesize("Hm. [pause:0.6s] Sure.", tmp_path / "v.mp3",
                           settings=_settings("kokoro"))
    assert calls[-1] == "Hm. Sure."


@pytest.mark.asyncio
async def test_other_engines_get_a_spoken_beat(tmp_path, monkeypatch):
    seen = {}

    async def eleven(text, out_path, settings):
        seen["text"] = text
        return out_path

    monkeypatch.setattr(voice, "_elevenlabs_tts", eleven)
    await voice.synthesize("Hm. [pause:0.6s] Sure.", tmp_path / "v.mp3",
                           settings=_settings("elevenlabs"))
    assert seen["text"] == "Hm. Sure."
    await voice.synthesize("Wait [pause:0.6s] what", tmp_path / "w.mp3",
                           settings=_settings("elevenlabs"))
    assert seen["text"] == "Wait … what"


def test_wav_join(tmp_path):
    import wave

    for name in ("a", "b"):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x01\x00" * 24000)
    out = voice._join_wavs([(tmp_path / "a.wav", 0.5), (tmp_path / "b.wav", 0)],
                           tmp_path / "j.mp3")
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() == 24000 * 2 + 12000


def test_silent_mp3_frames_parse_as_the_same_format():
    assert voice._mp3_format(_clip(1.0)) == _FMT
    assert len(voice._silent_mp3(_FMT, 1.0)) == 42 * 24  # 42 frames x 24 bytes
