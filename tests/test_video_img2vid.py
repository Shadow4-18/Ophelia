"""Image-to-video wiring for the xAI Grok Imagine API.

The xAI REST endpoint accepts `image: {"url": ...}` to lock in the first
frame. Ophelia's `generate_video` previously only sent `prompt` + `duration`,
so img2vid never worked. These tests pin the request body shape for both
text-to-video and image-to-video, including local-file → data-URI encoding.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ophelia.providers import media as media_mod
from ophelia.providers.router import XAIBackend


def _fake_xai_backend() -> MagicMock:
    backend = MagicMock(spec=XAIBackend)
    backend.provider_name = MagicMock(return_value="xai")
    backend.bearer = MagicMock(return_value="test-bearer")
    backend.bearer_fresh = AsyncMock(return_value="test-bearer")
    return backend


def _fake_stack(backend: MagicMock) -> MagicMock:
    stack = MagicMock()
    stack.name = MagicMock(return_value="xai")
    stack.model = MagicMock(return_value="grok-imagine-video")
    stack.backend = MagicMock(return_value=backend)
    return stack


def _settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.xai_base_url = "https://api.x.ai/v1"
    s.data_dir = tmp_path
    return s


@pytest.mark.asyncio
async def test_resolve_image_url_passes_through(tmp_path):
    out = await media_mod._resolve_xai_video_image("https://example.com/x.png")
    assert out == {"url": "https://example.com/x.png"}


@pytest.mark.asyncio
async def test_resolve_image_data_uri_passes_through(tmp_path):
    s = "data:image/png;base64,abc"
    assert await media_mod._resolve_xai_video_image(s) == {"url": s}


@pytest.mark.asyncio
async def test_resolve_image_file_id(tmp_path):
    out = await media_mod._resolve_xai_video_image("file_id:file_abc123")
    assert out == {"file_id": "file_abc123"}


@pytest.mark.asyncio
async def test_resolve_image_local_file_encoded_as_data_uri(tmp_path):
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")

    out = await media_mod._resolve_xai_video_image(str(img))
    assert out is not None
    url = out["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.standard_b64decode(url.split(",", 1)[1])
    assert decoded == b"\x89PNG\r\n\x1a\nfake-png-bytes"


@pytest.mark.asyncio
async def test_resolve_image_missing_file_returns_none(tmp_path):
    out = await media_mod._resolve_xai_video_image(str(tmp_path / "nope.png"))
    assert out is None


@pytest.mark.asyncio
async def test_generate_video_text_to_video_payload(tmp_path):
    """Without an image, the request body must NOT contain `image`."""
    backend = _fake_xai_backend()
    stack = _fake_stack(backend)
    settings = _settings(tmp_path)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"request_id": "req-1"}

    async def fake_post(url, headers=None, json=None, **_):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return _FakeResp()

    with patch("ophelia.providers.media.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = client

        # Polling loop returns done + url on first GET.
        class _PollResp:
            status_code = 200

            def json(self):
                return {
                    "status": "done",
                    "video": {"url": "https://vidgen.x.ai/x.mp4"},
                }

        class _DownloadResp:
            status_code = 200
            content = b"mp4-bytes"

            def raise_for_status(self):
                pass

        client.get = AsyncMock(
            side_effect=[
                _PollResp(),
                _DownloadResp(),
            ]
        )

        with patch(
            "ophelia.providers.media.get_model_gate"
        ) as gate_cls:
            gate = MagicMock()
            sess = AsyncMock()
            sess.__aenter__ = AsyncMock(return_value=None)
            sess.__aexit__ = AsyncMock(return_value=None)
            gate.session = MagicMock(return_value=sess)
            gate_cls.return_value = gate

            result = await media_mod.generate_video(
                settings,
                stack,
                "a cat naps",
                duration_seconds=6,
                artifacts_dir=tmp_path / "out",
            )

    body = captured["body"]
    assert body["model"] == "grok-imagine-video"
    assert body["prompt"] == "a cat naps"
    assert body["duration"] == 6
    assert "image" not in body, "text-to-video must not set image"
    assert "Video saved to" in result
    assert "text-to-video" in result


@pytest.mark.asyncio
async def test_generate_video_image_to_video_payload(tmp_path):
    """With an image URL, the request body must contain `image: {"url": ...}`."""
    backend = _fake_xai_backend()
    stack = _fake_stack(backend)
    settings = _settings(tmp_path)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"request_id": "req-2"}

    async def fake_post(url, headers=None, json=None, **_):
        captured["body"] = json
        return _FakeResp()

    with patch("ophelia.providers.media.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = client

        class _PollResp:
            status_code = 200

            def json(self):
                return {
                    "status": "done",
                    "video": {"url": "https://vidgen.x.ai/y.mp4"},
                }

        class _DownloadResp:
            status_code = 200
            content = b"mp4-bytes"

            def raise_for_status(self):
                pass

        client.get = AsyncMock(
            side_effect=[
                _PollResp(),
                _DownloadResp(),
            ]
        )

        with patch("ophelia.providers.media.get_model_gate") as gate_cls:
            gate = MagicMock()
            sess = AsyncMock()
            sess.__aenter__ = AsyncMock(return_value=None)
            sess.__aexit__ = AsyncMock(return_value=None)
            gate.session = MagicMock(return_value=sess)
            gate_cls.return_value = gate

            result = await media_mod.generate_video(
                settings,
                stack,
                "the cat stretches and yawns",
                duration_seconds=8,
                artifacts_dir=tmp_path / "out",
                image="https://example.com/cat.jpg",
                aspect_ratio="16:9",
                resolution="720p",
            )

    body = captured["body"]
    assert body["image"] == {"url": "https://example.com/cat.jpg"}
    assert body["aspect_ratio"] == "16:9"
    assert body["resolution"] == "720p"
    assert body["duration"] == 8
    assert "image-to-video" in result


@pytest.mark.asyncio
async def test_generate_video_local_image_path_encoded(tmp_path):
    """A local file path must be encoded into a data URI in the request body."""
    img = tmp_path / "start.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nhello")

    backend = _fake_xai_backend()
    stack = _fake_stack(backend)
    settings = _settings(tmp_path)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"request_id": "req-3"}

    async def fake_post(url, headers=None, json=None, **_):
        captured["body"] = json
        return _FakeResp()

    with patch("ophelia.providers.media.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = client

        class _PollResp:
            status_code = 200

            def json(self):
                return {
                    "status": "done",
                    "video": {"url": "https://vidgen.x.ai/z.mp4"},
                }

        class _DownloadResp:
            status_code = 200
            content = b"mp4-bytes"

            def raise_for_status(self):
                pass

        client.get = AsyncMock(
            side_effect=[
                _PollResp(),
                _DownloadResp(),
            ]
        )

        with patch("ophelia.providers.media.get_model_gate") as gate_cls:
            gate = MagicMock()
            sess = AsyncMock()
            sess.__aenter__ = AsyncMock(return_value=None)
            sess.__aexit__ = AsyncMock(return_value=None)
            gate.session = MagicMock(return_value=sess)
            gate_cls.return_value = gate

            await media_mod.generate_video(
                settings,
                stack,
                "the scene comes alive",
                duration_seconds=5,
                artifacts_dir=tmp_path / "out",
                image=str(img),
            )

    body = captured["body"]
    url = body["image"]["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.standard_b64decode(url.split(",", 1)[1])
    assert decoded == b"\x89PNG\r\n\x1a\nhello"


@pytest.mark.asyncio
async def test_generate_video_image_not_found_falls_back_to_txt2vid(tmp_path):
    """A missing image file should not 400 the whole request — drop image
    and proceed as text-to-video so the user still gets something back."""
    backend = _fake_xai_backend()
    stack = _fake_stack(backend)
    settings = _settings(tmp_path)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"request_id": "req-4"}

    async def fake_post(url, headers=None, json=None, **_):
        captured["body"] = json
        return _FakeResp()

    with patch("ophelia.providers.media.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = client

        class _PollResp:
            status_code = 200

            def json(self):
                return {
                    "status": "done",
                    "video": {"url": "https://vidgen.x.ai/w.mp4"},
                }

        class _DownloadResp:
            status_code = 200
            content = b"mp4-bytes"

            def raise_for_status(self):
                pass

        client.get = AsyncMock(
            side_effect=[
                _PollResp(),
                _DownloadResp(),
            ]
        )

        with patch("ophelia.providers.media.get_model_gate") as gate_cls:
            gate = MagicMock()
            sess = AsyncMock()
            sess.__aenter__ = AsyncMock(return_value=None)
            sess.__aexit__ = AsyncMock(return_value=None)
            gate.session = MagicMock(return_value=sess)
            gate_cls.return_value = gate

            await media_mod.generate_video(
                settings,
                stack,
                "a dog runs",
                duration_seconds=4,
                artifacts_dir=tmp_path / "out",
                image=str(tmp_path / "missing.png"),
            )

    body = captured["body"]
    assert "image" not in body, "missing image must be dropped, not sent"


@pytest.mark.asyncio
async def test_invalid_resolution_falls_back_to_480p(tmp_path):
    """An invalid resolution value (e.g. 'low', 'high', '1080p') must fall
    back to '480p' rather than sending the bad value to xAI and getting a 400."""
    backend = _fake_xai_backend()
    stack = _fake_stack(backend)
    settings = _settings(tmp_path)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"request_id": "req-1"}

    async def fake_post(url, headers=None, json=None, **_):
        captured["body"] = json
        return _FakeResp()

    with patch("ophelia.providers.media.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = client

        class _PollResp:
            status_code = 200

            def json(self):
                return {"status": "done", "video": {"url": "https://x.ai/v.mp4"}}

        class _DownloadResp:
            status_code = 200
            content = b"mp4"

            def raise_for_status(self):
                pass

        client.get = AsyncMock(side_effect=[_PollResp(), _DownloadResp()])

        with patch("ophelia.providers.media.get_model_gate") as gate_cls:
            gate = MagicMock()
            sess = AsyncMock()
            sess.__aenter__ = AsyncMock(return_value=None)
            sess.__aexit__ = AsyncMock(return_value=None)
            gate.session = MagicMock(return_value=sess)
            gate_cls.return_value = gate

            await media_mod.generate_video(
                settings,
                stack,
                "test",
                duration_seconds=6,
                artifacts_dir=tmp_path / "out",
                resolution="low",  # invalid — must fall back to 480p
            )

    body = captured["body"]
    assert body["resolution"] == "480p", "invalid resolution must fall back to 480p"


@pytest.mark.asyncio
async def test_valid_480p_resolution_passes_through(tmp_path):
    """A valid '480p' resolution must be passed through unchanged."""
    backend = _fake_xai_backend()
    stack = _fake_stack(backend)
    settings = _settings(tmp_path)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"request_id": "req-1"}

    async def fake_post(url, headers=None, json=None, **_):
        captured["body"] = json
        return _FakeResp()

    with patch("ophelia.providers.media.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value = client

        class _PollResp:
            status_code = 200

            def json(self):
                return {"status": "done", "video": {"url": "https://x.ai/v.mp4"}}

        class _DownloadResp:
            status_code = 200
            content = b"mp4"

            def raise_for_status(self):
                pass

        client.get = AsyncMock(side_effect=[_PollResp(), _DownloadResp()])

        with patch("ophelia.providers.media.get_model_gate") as gate_cls:
            gate = MagicMock()
            sess = AsyncMock()
            sess.__aenter__ = AsyncMock(return_value=None)
            sess.__aexit__ = AsyncMock(return_value=None)
            gate.session = MagicMock(return_value=sess)
            gate_cls.return_value = gate

            await media_mod.generate_video(
                settings,
                stack,
                "test",
                duration_seconds=6,
                artifacts_dir=tmp_path / "out",
                resolution="480p",
            )

    body = captured["body"]
    assert body["resolution"] == "480p"
