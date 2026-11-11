"""Multi-person prompts keep their cast; a style reference adds style, not content."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.media import style_reference as sr
from ophelia.providers.civitai.catalog import RECIPES
from ophelia.providers.civitai.pipeline import build_negative
from ophelia.providers.civitai.prompting import cast, ensure_subject, format_prompt


@pytest.mark.parametrize("prompt, want", [
    ("a man and a woman having sex on a bed", (1, 1)),
    ("her boyfriend fucking her from behind", (1, 1)),
    ("woman giving a blowjob, pov", (1, 1)),
    ("two women kissing in a shower", (2, 0)),
    ("lesbian couple, scissoring", (2, 0)),
    ("threesome, two men and a woman", (1, 2)),
    ("ffm threesome", (2, 1)),
    ("nude woman on a beach", (1, 0)),
    ("2girls, 1boy, sex", (2, 1)),
    ("sunset over the sea", (0, 0)),
])
def test_cast(prompt, want):
    assert cast(prompt) == want


def test_couple_gets_both_count_tags_and_no_solo():
    p = format_prompt(RECIPES["illustrious"], "a man and a woman having sex on a bed",
                      nsfw=True)
    assert "1girl, 1boy, hetero" in p and "solo" not in p
    p = format_prompt(RECIPES["pony"], "two women kissing, nude", nsfw=True)
    assert "2girls" in p and "yuri" in p and "1boy" not in p


def test_non_intimate_group_gets_no_hetero_or_yuri():
    assert ensure_subject("three girls in a hot tub") == \
        "3girls, multiple girls, three girls in a hot tub"


def test_cast_negatives_stop_gender_swaps():
    neg, _ = build_negative(RECIPES["illustrious"], nsfw=True, embeddings=False,
                            prompt="1girl, 1boy, missionary")
    assert "yuri" in neg and "futanari" in neg and "2girls" in neg
    neg, _ = build_negative(RECIPES["illustrious"], nsfw=True, embeddings=False,
                            prompt="two women in bed, kissing")
    assert "1boy" in neg and "male focus" in neg
    solo, _ = build_negative(RECIPES["illustrious"], nsfw=True, embeddings=False,
                             prompt="1girl, nude")
    assert "yuri" not in solo
    # Natural-language models get none of the tag negatives.
    natural, _ = build_negative(RECIPES["zimage-base"], nsfw=True, embeddings=False,
                                prompt="a man and a woman")
    assert "yuri" not in natural


def test_variety_leaves_couples_faces_and_poses_alone(tmp_path):
    from ophelia.providers.civitai import variety

    s = SimpleNamespace(data_dir=tmp_path)
    for _ in range(10):
        _, added = variety.add_variety(s, "1girl, 1boy, sex, bed", natural=False)
        assert not any(a in variety._BANKS["expression"][1] for a in added)


def test_parse_style_keeps_style_and_drops_subject():
    ref = sr.parse_style('```json\n{"medium": "anime", "tags": ["thick lineart", '
                         '"cel shading", "long hair", "pastel palette", "nude"], '
                         '"sentence": "flat 90s anime cel look"}\n```')
    assert ref.medium == "anime" and ref.tags == ["thick lineart", "cel shading",
                                                  "pastel palette"]
    assert ref.sentence == "flat 90s anime cel look"
    assert not sr.parse_style("I can't see an image").ok


def test_apply_style_adds_medium_only_when_prompt_has_none():
    ref = sr.StyleRef(medium="anime", tags=["cel shading", "flat colors"])
    assert sr.apply_style("a woman in a red dress", ref) == \
        "anime style, a woman in a red dress, cel shading, flat colors"
    assert sr.apply_style("photo of a woman", ref).startswith("photo of a woman")


@pytest.mark.asyncio
async def test_style_image_reaches_the_prompt_once(tmp_path, monkeypatch):
    ref_img = tmp_path / "ref.png"
    ref_img.write_bytes(b"\x89PNG fake")
    calls = []

    async def fake_describe(settings, path, *, question, stack=None):
        calls.append(path)
        return '{"medium": "photo", "tags": ["film grain", "35mm photo"], "sentence": "x"}'

    monkeypatch.setattr("ophelia.media.vision_input.describe_image_file", fake_describe)
    settings = SimpleNamespace(data_dir=tmp_path / "data")
    for _ in range(2):
        ref = await sr.analyze_style(settings, ref_img)
        assert ref.tags == ["film grain", "35mm photo"]
    assert len(calls) == 1  # cached

    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = settings
    reg.stack = None
    reg.artifacts_dir = tmp_path / "art"
    seen = {}

    async def fake_generate(settings, stack, job, artifacts_dir):
        seen["prompt"] = job.prompt
        return "Image saved to /x.png"

    monkeypatch.setattr("ophelia.tools.registry.run_image_job", fake_generate)
    reg._finalize_media_tool_result = AsyncMock(side_effect=lambda r, **k: r)
    out = await reg._generate_image("a woman on a balcony", style_image=str(ref_img))
    assert seen["prompt"] == "photo, a woman on a balcony, film grain, 35mm photo"
    assert out.startswith("Style from the reference (photo)")
