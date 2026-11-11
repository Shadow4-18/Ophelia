"""LoRAs chosen to work together, and prompts in the tags the models know."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ophelia.providers.civitai import feedback, loras, site
from ophelia.providers.civitai.air import explicit_lora_weights
from ophelia.providers.civitai.catalog import RECIPES
from ophelia.providers.civitai.pipeline import GenerationRequest, plan_generation
from ophelia.providers.civitai.prompting import normalize_tags
from ophelia.providers.civitai.site import lora_category, recommended_weight
from ophelia.providers.civitai.stack import BUDGET, LoraPick, plan_stack


@pytest.mark.parametrize("desc, w", [
    ("generate to missionary sex. strength 0.8~0.6", 0.7),
    ("Weight: 1.0 Trigger words:", 1.0),
    ("use at 0.65-0.85 strength", 0.75),
    ("Recommended weight 0.5 to 0.7", 0.6),
    ("Version 2.0 is out", None),
])
def test_recommended_weight(desc, w):
    assert recommended_weight(desc) == w


@pytest.mark.parametrize("tags, name, cat", [
    (["concept"], "Missionary - Concept", "pose"),
    (["style", "anime"], "Retro", "style"),
    (["character"], "Ophelia OC", "character"),
    (["clothing"], "Latex catsuit", "clothing"),
    (["concept"], "X-ray", "concept"),
    ([], "Detail Tweaker XL", "detail"),
    ([], "Something", "other"),
])
def test_lora_category(tags, name, cat):
    assert lora_category(tags, name) == cat


def test_one_per_job_and_hers_always_kept():
    kept, weights, notes = plan_stack([
        LoraPick("a", "Her style", "style", source="hers", weight=0.8),
        LoraPick("b", "Auto style", "style", source="style", rec=0.6),
        LoraPick("c", "Missionary", "pose", source="concept", group="position"),
        LoraPick("d", "Cowgirl", "pose", source="concept", group="position"),
    ])
    assert [p.air for p in kept] == ["a", "c"]
    assert weights == {"a": 0.8, "c": 0.75}
    assert any("Auto style" in n for n in notes) and any("Cowgirl" in n for n in notes)


def test_creator_weight_used_and_heavy_stacks_scaled():
    picks = [LoraPick(str(i), f"L{i}", "concept" if i < 2 else "character",
                      source="concept" if i < 2 else "hers", rec=1.0) for i in range(4)]
    kept, weights, notes = plan_stack(picks)
    assert sum(weights.values()) <= BUDGET + 0.01
    assert any("scaled" in n for n in notes)
    _, w, _ = plan_stack([LoraPick("x", rec=0.55, source="concept")])
    assert w == {"x": 0.55}


def test_her_heavy_weights_are_kept_but_warned():
    _, w, notes = plan_stack([LoraPick(str(i), source="hers", weight=1.0) for i in range(3)])
    assert w == {"0": 1.0, "1": 1.0, "2": 1.0}
    assert any("fry" in n for n in notes)


def test_explicit_weights():
    assert explicit_lora_weights("urn:a|0.7, urn:b") == {"urn:a"}
    assert explicit_lora_weights({"urn:a": 0.5}) == {"urn:a"}
    assert explicit_lora_weights('{"urn:a": 0.5, "urn:b": "auto"}') == {"urn:a"}


def test_concepts_one_per_group_first_named_wins():
    got = loras.concepts_with_groups("cowgirl, then missionary, creampie, cum on face")
    assert [(q, g) for q, _, _, g in got] == [("cowgirl", "position"), ("creampie", "fluid")]
    assert [q for q, *_ in loras.concepts_with_groups("reverse cowgirl")] == \
        ["reverse cowgirl"]
    assert loras.concepts_with_groups("smiling facial expression") == []


def test_tags_normalized_and_counts_first():
    assert normalize_tags("doggy style, big boobs, 1boy, naked, 1girl, thigh highs") == \
        "1girl, 1boy, doggystyle, large breasts, nude, thighhighs"


def _res(name, air, tags=(), desc="", dl=10000, words=()):
    return site.CivitaiResource(name=name, type="LORA", model_id=1, version_id=1,
                                version_name="1", air=air, base_model="Illustrious",
                                trained_words=list(words), download_count=dl,
                                description=desc, tags=list(tags))


@pytest.mark.asyncio
async def test_her_lora_plus_non_overlapping_auto_concepts(tmp_path, monkeypatch):
    async def search(settings, query, **kw):
        return {
            "missionary": [_res("Missionary", "urn:air:sdxl:lora:civitai:7@7", ["concept"],
                                "strength 0.8~0.6", words=["missionary_pov"])],
            "creampie": [_res("Creampie", "urn:air:sdxl:lora:civitai:8@8", ["concept"],
                              words=["cum in pussy"])],
        }.get(query, [])

    async def get_version(settings, vid):
        return site.CivitaiResource(
            name="Her Character", type="LORA", model_id=3, version_id=vid, version_name="1",
            air="", base_model="Illustrious", trained_words=["herchar, purple hair"],
            tags=["character"], description="Weight: 0.9")

    monkeypatch.setattr(site, "search_models", search)
    monkeypatch.setattr(site, "get_version", get_version)
    s = SimpleNamespace(data_dir=tmp_path, civitai_auto_loras=True, civitai_variety="off",
                        civitai_checkpoints="", civitai_default_style="anime",
                        civitai_negative_embeddings=False, civitai_max_count=4)
    her = "urn:air:sdxl:lora:civitai:3@33"
    plan = await plan_generation(s, GenerationRequest(
        prompt="anime, 1girl, 1boy, missionary, creampie", nsfw=True, model="illustrious",
        loras=her))
    # Pinned model: no auto LoRAs; hers gets its creator weight and trigger.
    assert plan.loras == {her: 0.9}
    assert "herchar" in plan.prompt
    plan = await plan_generation(s, GenerationRequest(
        prompt="anime, 1girl, 1boy, missionary, creampie", nsfw=True, loras=her))
    assert plan.loras[her] == 0.9
    assert plan.loras["urn:air:sdxl:lora:civitai:7@7"] == 0.7  # creator's 0.8~0.6
    assert "urn:air:sdxl:lora:civitai:8@8" in plan.loras
    assert "missionary_pov" in plan.prompt and "cum in pussy" in plan.prompt


def test_liked_prompts(tmp_path):
    s = SimpleNamespace(data_dir=tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "image_generations.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"recipe": "illustrious", "prompt": "good one", "path": "/a/1.png"},
        {"recipe": "illustrious", "prompt": "bad one", "path": "/a/2.png"},
        {"recipe": "pony", "prompt": "pony one", "path": "/a/3.png"},
    ]))
    (logs / "image_feedback.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"file": "1.png", "score": 1.0}, {"file": "2.png", "score": -1.0},
        {"file": "3.png", "score": 1.0},
    ]))
    assert [r["prompt"] for r in feedback.liked_prompts(s, "illustrious")] == ["good one"]
