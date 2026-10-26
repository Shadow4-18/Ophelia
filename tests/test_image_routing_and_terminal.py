"""Grok first for non-explicit images, honest fallbacks, and a terminal the
owner can read from work."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _settings(tmp_path):
    s = MagicMock()
    s.image_nsfw_allowed = True
    s.civitai_api_key = "k"
    s.image_max_attempts = 5
    s.data_dir = tmp_path
    s.image_backend_configured = lambda name: name in ("civitai", "pollinations")
    return s


@pytest.mark.asyncio
async def test_grok_failure_falls_back_to_civitai_before_pollinations(tmp_path, monkeypatch):
    from ophelia.providers import media

    settings = _settings(tmp_path)
    stack = MagicMock()
    stack.image_provider_for.return_value = "xai"
    stack.image_model_for.return_value = "grok-imagine-image-2.0"
    tried = []

    async def dispatch(provider, **kw):
        tried.append((provider, kw["nsfw"]))
        if provider == "xai":
            raise RuntimeError("400 Generated image rejected by content moderation")
        return f"Image saved to {tmp_path / (provider + '.png')}"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    out = await media.generate_image(settings, stack, "her in black lingerie",
                                     artifacts_dir=tmp_path, fallback_mature=True)
    assert tried == [("xai", False), ("civitai", True)]  # not Pollinations, mature allowed
    assert "fell back from xai because xai: 400 Generated image rejected" in out
    lines = [json.loads(x) for x in (tmp_path / "logs" / "image_generations.jsonl").read_text()
             .splitlines()]
    assert lines[0]["provider"] == "xai" and lines[0]["ok"] is False
    assert "content moderation" in lines[0]["error"]


@pytest.mark.asyncio
async def test_pollinations_is_the_last_resort(tmp_path, monkeypatch):
    from ophelia.providers import media

    settings = _settings(tmp_path)
    settings.image_backend_configured = lambda name: name == "pollinations"
    stack = MagicMock()
    stack.image_provider_for.return_value = "xai"
    stack.image_model_for.return_value = "grok"

    async def dispatch(provider, **kw):
        if provider == "xai":
            return "xAI: 503 overloaded"
        return f"Image saved to {tmp_path / 'p.png'}"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    out = await media.generate_image(settings, stack, "a cat", artifacts_dir=tmp_path)
    assert "backend: pollinations" in out and "because xai: xAI: 503 overloaded" in out
    assert "fell back from xai" in out


def _registry(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(image_nsfw_allowed=True)
    reg.stack = MagicMock()
    reg.artifacts_dir = tmp_path
    reg.set_owner(True)
    reg._current_sender_channel = "telegram:1"
    reg._finalize_media_tool_result = AsyncMock(side_effect=lambda r: r)
    return reg


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,model,expect_nsfw", [
    ("me sitting on a chessboard in a torn dress", None, False),  # was sent to Civitai
    ("her in black lace lingerie on a bed", None, False),
    ("fully nude, spread legs", None, True),
    ("explicit sex scene", None, True),
    ("gothic portrait", "urn:air:sdxl:checkpoint:civitai:1@2", True),  # her pinned choice
])
async def test_grok_first_unless_actually_explicit(tmp_path, monkeypatch, prompt, model,
                                                  expect_nsfw):
    from ophelia.tools import registry as reg_mod

    seen = {}

    async def fake_generate(settings, stack, p, **kw):
        seen.update(kw)
        return "Image saved to x.png"

    monkeypatch.setattr(reg_mod, "generate_image", fake_generate)
    reg = _registry(tmp_path)
    await reg._generate_image(prompt, nsfw=True, model=model)
    assert seen["nsfw"] is expect_nsfw
    assert seen["fallback_mature"] is True


# --- terminal ---------------------------------------------------------------


def test_tap_renders_redacts_and_filters():
    from ophelia import log_tap

    log_tap.register_secrets(["supersecretkeyvalue99"])
    seq0, _ = log_tap.since(0)
    log_tap.tap(None, "info", {"event": "image.saved", "level": "info", "provider": "xai",
                               "timestamp": "2026-10-21T18:20:01Z"})
    log_tap.tap(None, "warning", {"event": "civitai.submit", "level": "warning",
                                  "auth": "Bearer supersecretkeyvalue99"})
    log_tap.tap(None, "debug", {"event": "model_gate.acquire", "level": "debug"})
    log_tap.tap(None, "info", {"event": "terminal_mirror.post_failed", "level": "info"})
    _, lines = log_tap.since(seq0)
    assert lines[0] == "18:20:01 info image.saved provider=xai"
    assert "supersecretkeyvalue99" not in lines[1] and "[redacted]" in lines[1]
    assert len(lines) == 2  # debug filtered, the mirror's own errors skipped
    assert "civitai.submit" in log_tap.tail(1, min_level=30)[0]


@pytest.mark.asyncio
async def test_terminal_posts_code_blocks_under_discord_limit():
    from ophelia.channels.discord_log_channels import DiscordLogChannels

    ch = SimpleNamespace(send=AsyncMock())
    logs = DiscordLogChannels.__new__(DiscordLogChannels)
    logs._resolve_main_channel = AsyncMock(return_value=ch)
    lines = [f"18:20:{i:02d} info image.attempt provider=xai " + "x" * 80 for i in range(60)]
    assert await logs.post_terminal(None, lines)
    sent = [c.args[0] for c in ch.send.await_args_list]
    assert len(sent) > 1 and all(len(m) <= 2000 for m in sent)
    assert all(m.startswith("```\n") and m.endswith("\n```") for m in sent)


@pytest.mark.asyncio
async def test_telegram_logs_is_owner_only():
    from ophelia import log_tap
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw.settings = MagicMock()
    gw.settings.is_owner_channel = lambda ch: ch == "telegram:1"
    log_tap.add(20, "18:22:00 info hello world")
    reply = AsyncMock()

    def update(uid):
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                               message=SimpleNamespace(reply_text=reply))

    await gw.cmd_logs(update(2), SimpleNamespace(args=[]))
    reply.assert_not_awaited()
    await gw.cmd_logs(update(1), SimpleNamespace(args=["5"]))
    assert "hello world" in reply.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_logs_file_and_image_ledger(tmp_path):
    from ophelia import log_tap
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw.settings = MagicMock()
    gw.settings.is_owner_channel = lambda ch: ch == "telegram:1"
    gw.settings.data_dir = tmp_path
    log_tap.add(20, "18:30:00 info civitai.submit model=lustify")
    doc = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                             message=SimpleNamespace(reply_text=AsyncMock(), reply_document=doc))
    await gw.cmd_logs(update, SimpleNamespace(args=["file"]))
    sent = doc.await_args.kwargs["document"]
    assert sent.filename.startswith("ophelia-log-") and sent.filename.endswith(".txt")
    assert b"civitai.submit model=lustify" in sent.input_file_content
    ledger = tmp_path / "logs" / "image_generations.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"provider": "civitai", "auth": "Bearer supersecretkeyvalue99"}\n')
    log_tap.register_secrets(["supersecretkeyvalue99"])
    await gw.cmd_logs(update, SimpleNamespace(args=["images"]))
    sent = doc.await_args.kwargs["document"]
    assert sent.filename.endswith(".jsonl") and b"civitai" in sent.input_file_content
    assert b"supersecretkeyvalue99" not in sent.input_file_content
