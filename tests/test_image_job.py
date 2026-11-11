"""The image request/result objects that replace loose kwargs and string parsing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.providers import media
from ophelia.providers.image_job import (
    FAILED, PENDING, REFUSED, SAVED, ImageJob, ImageResult,
)


def test_job_from_kwargs_ignores_unknown_and_trims_model():
    job = ImageJob.from_kwargs("x", model="  pony ", nsfw=True, style_terms=None, bogus=1)
    assert job.model == "pony" and job.nsfw and job.style_terms == []
    assert job.but(attempts=1).attempts == 1 and job.attempts == 1


def test_result_from_text(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    r = ImageResult.from_text(f"Image saved to {img} (backend: xai/auto)")
    assert r.status == SAVED and r.paths == [img] and r.ok and not r.error
    assert ImageResult.from_text("Pending: rendering").status == PENDING
    assert ImageResult.from_text("Refused: nope").status == REFUSED
    bad = ImageResult.from_text("Civitai submit failed HTTP 500")
    assert bad.status == FAILED and bad.error and not bad.ok
    assert str(bad) == "Civitai submit failed HTTP 500"


class _Stack:
    def image_provider_for(self, nsfw=False):
        return "civitai" if nsfw else "xai"

    def image_model_for(self, provider, nsfw=False):
        return ""


@pytest.mark.asyncio
async def test_job_reaches_the_backend_whole(monkeypatch, tmp_path):
    img = tmp_path / "out.png"
    img.write_bytes(b"x")
    seen = {}

    async def dispatch(provider, *, job, nsfw, resolved_model, **kw):
        seen.update(provider=provider, job=job, nsfw=nsfw)
        return f"Image saved to {img}"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    settings = SimpleNamespace(image_nsfw_allowed=True, civitai_api_key="k",
                               image_max_attempts=5)
    job = ImageJob(prompt="1girl, 1boy", nsfw=True, pose_image="/p.png",
                   style_terms=["watercolor"], loras="urn:air:x|0.5", count=2)
    result = await media.run_image_job(settings, _Stack(), job, tmp_path)
    assert seen["provider"] == "civitai" and seen["job"].pose_image == "/p.png"
    assert seen["job"].style_terms == ["watercolor"] and seen["job"].count == 2
    assert result.ok and result.paths == [img] and result.provider == "civitai"
    assert "(backend: civitai/" in result.text
    # The string API still works for older callers.
    assert (await media.generate_image(settings, _Stack(), "x", artifacts_dir=tmp_path,
                                       nsfw=True)).startswith(f"Image saved to {img}")


@pytest.mark.asyncio
async def test_tool_delivers_the_result_paths(monkeypatch, tmp_path):
    from ophelia.tools import registry as reg_mod

    img = tmp_path / "out.png"
    img.write_bytes(b"x")

    async def fake_run(settings, stack, job, artifacts_dir):
        return ImageResult(status=SAVED, text=f"Image saved to {img}", paths=[img])

    monkeypatch.setattr(reg_mod, "run_image_job", fake_run)
    reg = reg_mod.ToolRegistry.__new__(reg_mod.ToolRegistry)
    reg.settings = SimpleNamespace(image_nsfw_allowed=False, image_self_check="off")
    reg.stack = None
    reg.artifacts_dir = tmp_path
    reg._finalize_media_tool_result = AsyncMock(side_effect=lambda r, **kw: r)
    await reg._generate_image("a cat")
    assert reg._finalize_media_tool_result.await_args.kwargs["paths"] == [img]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, fn", [
    ("fal", "_fal_image"), ("replicate", "_replicate_image"), ("a1111", "_a1111_image"),
    ("comfyui", "_comfyui_image"), ("modelslab", "_modelslab_image"),
    ("ollama", "_ollama_image"), ("openai", "_openai_image"),
])
async def test_moved_backends_still_dispatch(monkeypatch, tmp_path, provider, fn):
    from ophelia.providers import image_backends_extra as extra

    async def fake(*a, **kw):
        return f"from {provider}"

    monkeypatch.setattr(extra, fn, fake)
    out = await media._dispatch_image_provider(
        provider, settings=SimpleNamespace(), stack=None, job=ImageJob(prompt="x"),
        resolved_model="m", artifacts_dir=tmp_path, nsfw=False, auto_pick=False)
    assert out == f"from {provider}"
