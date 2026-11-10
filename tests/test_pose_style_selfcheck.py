"""Pose control, style LoRAs from a reference, and the self-check before sending."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ophelia.media import self_check
from ophelia.providers import civitai as civ
from ophelia.providers.civitai import loras, site
from ophelia.providers.civitai.catalog import RECIPES
from ophelia.providers.civitai.orchestration import supports_pose, with_pose

from tests.test_civitai_pipeline import _gen, _input, _ledger, fake  # noqa: F401


# --- pose -------------------------------------------------------------------


def test_with_pose_moves_sdxl_to_comfy_with_a_dwpose_controlnet():
    r = RECIPES["lustify"]
    w, h, steps = civ.render_settings(r, "standard", "2:3")
    inp = civ.build_input(r, prompt="x", width=w, height=h, steps=steps, negative="y",
                          loras={"urn:air:sdxl:lora:civitai:1@2": 0.7}, seed=3)
    out = with_pose(inp, "https://a/pose.png")
    assert out["engine"] == "comfy" and out["checkpointModel"] == r.model
    assert "model" not in out and "sampleMethod" not in out
    assert (out["sampler"], out["scheduler"]) == ("dpmpp_2m", "karras")
    assert out["loras"] and out["seed"] == 3 and out["negativePrompt"] == "y"
    cn = out["controlNets"][0]
    assert cn["preprocessor"] == "dwpose" and cn["image"] == "https://a/pose.png"
    assert 0 <= cn["startStep"] < cn["endStep"] <= 1
    assert supports_pose(RECIPES["illustrious"]) and not supports_pose(RECIPES["chroma"])
    assert not supports_pose(RECIPES["zimage-turbo"])


@pytest.mark.asyncio
async def test_pose_image_runs_on_comfy(fake, tmp_path):  # noqa: F811
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"\x89PNG\r\n\x1a\n" + b"p" * 64)
    out = await _gen(fake, "1girl, 1boy, sex, anime", pose_image=str(pose))
    inp = _input(fake)
    assert out.startswith("Image saved to")
    assert inp["engine"] == "comfy" and inp["controlNets"][0]["image"].startswith("https://")
    assert _ledger(fake)[-1]["pose_image"] == str(pose)


@pytest.mark.asyncio
async def test_pose_on_a_family_without_it_swaps_to_sdxl(fake, tmp_path):  # noqa: F811
    from ophelia.providers.civitai import pipeline

    pose = tmp_path / "pose.png"
    pose.write_bytes(b"\x89PNG" + b"p" * 64)
    plan = await pipeline.plan_generation(
        fake["settings"], pipeline.GenerationRequest(prompt="x", model="chroma",
                                                     pose_image=str(pose), nsfw=True))
    assert "can't copy a pose" in plan.error


@pytest.mark.asyncio
async def test_refused_pose_control_falls_back_without_it(fake, tmp_path, monkeypatch):  # noqa: F811,E501
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"\x89PNG" + b"p" * 64)
    from ophelia.providers.civitai.orchestration import CivitaiClient

    # Make the fake reject comfy inputs.
    orig_submit = CivitaiClient.submit
    seen = []

    async def submit(self, body, **kw):
        inp = body["steps"][0]["input"]
        seen.append(inp.get("engine"))
        if inp.get("engine") == "comfy":
            return httpx.Response(400, text='{"errors": {"controlNets": ["unsupported"]}}')
        return await orig_submit(self, body, **kw)

    monkeypatch.setattr(CivitaiClient, "submit", submit)
    out = await _gen(fake, "1girl, anime", pose_image=str(pose))
    assert seen == ["comfy", "sdcpp"]
    assert "pose control refused" in out


# --- style LoRA -------------------------------------------------------------


def _res(name, air, base="Illustrious", dl=10000, words=()):
    return site.CivitaiResource(name=name, type="LORA", model_id=1, version_id=1,
                                version_name="1", air=air, base_model=base,
                                trained_words=list(words), download_count=dl)


def test_style_queries_skip_generic_terms():
    q = loras.style_queries(["high contrast", "soft lighting", "1990s anime style",
                             "watercolor", "thick lineart", "cel shading"])
    assert [x[0] for x in q] == ["1990s anime style", "watercolor", "thick lineart"]


@pytest.mark.asyncio
async def test_style_lora_found_for_the_family(tmp_path, monkeypatch):
    async def search(settings, query, **kw):
        return [_res("Retro 90s Anime Style", "urn:air:sdxl:lora:civitai:7@7", dl=50000,
                     words=["90s anime, retro"]),
                _res("90s anime (Pony)", "urn:air:sdxl:lora:civitai:8@8", base="Pony")]

    monkeypatch.setattr(site, "search_models", search)
    s = SimpleNamespace(data_dir=tmp_path, civitai_auto_loras=True)
    found, trig, notes = await loras.pick_style_lora(s, RECIPES["illustrious"],
                                                     ["soft lighting", "1990s anime style"])
    assert found == {"urn:air:sdxl:lora:civitai:7@7": 0.6}
    assert trig == ["90s anime", "retro"] and "style LoRA" in notes[0]
    assert await loras.pick_style_lora(s, RECIPES["zimage-turbo"], ["watercolor"]) == \
        ({}, [], [])


# --- self-check -------------------------------------------------------------


def test_self_check_parse():
    p = "1girl, 1boy, sex, missionary"
    assert self_check.parse('{"women": 1, "men": 1, "merged_bodies": false, '
                            '"action_matches": true, "problem": ""}', p).ok
    bad = self_check.parse('{"women": 2, "men": 0, "merged_bodies": false, '
                           '"action_matches": true, "problem": "man drawn as woman"}', p)
    assert not bad.ok and "2 women instead of 1" in bad.problem and "0 men" in bad.problem
    merged = self_check.parse('{"women": 1, "men": 1, "merged_bodies": true}', p)
    assert not merged.ok and "merged" in merged.problem
    assert self_check.parse("I can't help with that", p).ok  # never blocks on no answer


def test_what_gets_checked():
    s = SimpleNamespace(image_self_check="people")
    assert self_check.wants_check(s, "a man and a woman kissing")
    assert self_check.wants_check(s, "two women in a shower")
    assert not self_check.wants_check(s, "nude woman on a beach")
    assert self_check.wants_check(SimpleNamespace(image_self_check="all"), "nude woman")
    assert not self_check.wants_check(SimpleNamespace(image_self_check="off"),
                                      "a man and a woman")


def _registry(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(data_dir=tmp_path, image_self_check="people")
    reg.stack = None
    reg.artifacts_dir = tmp_path
    return reg


@pytest.mark.asyncio
async def test_wrong_picture_is_remade_and_only_the_good_one_sent(tmp_path, monkeypatch):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    for p in (a, b):
        p.write_bytes(b"\x89PNG")
    verdicts = {a: self_check.Check(ok=False, problem="2 women instead of 1"),
                b: self_check.Check(ok=True)}

    async def check(settings, path, prompt, *, stack=None):
        return verdicts[Path(path)]

    monkeypatch.setattr(self_check, "check_image", check)
    reg = _registry(tmp_path)
    again = AsyncMock(return_value=f"Image saved to {b}")
    out = await reg._self_check_image(f"Image saved to {a}", "a man and a woman, sex", again)
    again.assert_awaited_once()
    assert str(b) in out and str(a) not in out and "made again" in out


@pytest.mark.asyncio
async def test_good_picture_and_solo_are_left_alone(tmp_path, monkeypatch):
    a = tmp_path / "a.png"
    a.write_bytes(b"\x89PNG")
    check = AsyncMock(return_value=self_check.Check(ok=True))
    monkeypatch.setattr(self_check, "check_image", check)
    reg = _registry(tmp_path)
    again = AsyncMock()
    assert await reg._self_check_image(f"Image saved to {a}", "1girl, 1boy, sex", again) \
        == f"Image saved to {a}"
    assert await reg._self_check_image(f"Image saved to {a}", "nude woman", again) == \
        f"Image saved to {a}"
    assert await reg._self_check_image("Pending: still rendering", "1girl, 1boy", again) \
        == "Pending: still rendering"
    again.assert_not_awaited()
    assert check.await_count == 1
