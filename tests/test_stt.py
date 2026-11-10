"""Speech to text goes to xAI's /v1/stt (the old route 404'd)."""

from __future__ import annotations

import httpx
import pytest

from ophelia.media import voice


def _patch(monkeypatch, handler):
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)


@pytest.mark.asyncio
async def test_xai_uses_v1_stt_with_the_file_last(tmp_path, monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), req.content))
        return httpx.Response(200, json={"text": " hello there ", "language": "en"})

    _patch(monkeypatch, handler)
    ogg = tmp_path / "in.ogg"
    ogg.write_bytes(b"OggS....")
    out = await voice.transcribe_audio(ogg, bearer="k", base_url="https://api.x.ai/v1")
    assert out == "hello there"
    assert [u for u, _ in seen] == ["https://api.x.ai/v1/stt"]
    assert b"audio/ogg" in seen[0][1] and b'name="model"' not in seen[0][1]


@pytest.mark.asyncio
async def test_silence_is_empty_and_other_servers_fall_back(tmp_path, monkeypatch):
    urls = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls.append(req.url.path)
        if req.url.path.endswith("/stt"):
            return httpx.Response(404)
        return httpx.Response(200, json={"text": ""})

    _patch(monkeypatch, handler)
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"ID3")
    assert await voice.transcribe_audio(mp3, bearer="k",
                                        base_url="http://localhost:8000/v1") == ""
    assert urls == ["/v1/stt", "/v1/audio/transcriptions"]
