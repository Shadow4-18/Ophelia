"""Prompt rewritten for the picked model; LoRA pairings and weights he liked."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ophelia.providers.civitai import feedback, loras, rewrite, site
from ophelia.providers.civitai.catalog import RECIPES
from ophelia.providers.civitai.pipeline import GenerationRequest, plan_generation
from ophelia.providers.civitai.stack import LoraPick, plan_stack


def _settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, civitai_auto_loras=True, civitai_variety="off",
                civitai_checkpoints="", civitai_default_style="anime",
                civitai_negative_embeddings=False, civitai_max_count=4,
                civitai_prompt_rewrite="auto")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("prompt, shape", [
    ("1girl, nude, long hair, on bed, from above", "tags"),
    ("A woman with long red hair is kneeling on the bed while her boyfriend watches", "sentences"),
    ("nude", "short"),
])
def test_shape(prompt, shape):
    assert rewrite.shape(prompt) == shape


def test_rewrite_only_when_the_shape_does_not_fit(tmp_path):
    rewrite.set_llm(lambda s, u: None)
    s = _settings(tmp_path)
    sentence = "A woman with long red hair is kneeling on the bed in soft light"
    tags = "1girl, red hair, kneeling, on bed, soft lighting, nude"
    assert rewrite.needs_rewrite(s, RECIPES["illustrious"], sentence)
    assert not rewrite.needs_rewrite(s, RECIPES["illustrious"], tags)
    assert rewrite.needs_rewrite(s, RECIPES["chroma"], tags)
    assert not rewrite.needs_rewrite(s, RECIPES["chroma"], sentence)
    assert not rewrite.needs_rewrite(s, RECIPES["lustify"], sentence)  # reads both
    assert rewrite.needs_rewrite(_settings(tmp_path, civitai_prompt_rewrite="always"),
                                 RECIPES["lustify"], tags)
    assert not rewrite.needs_rewrite(_settings(tmp_path, civitai_prompt_rewrite="off"),
                                     RECIPES["illustrious"], sentence)
    rewrite.set_llm(None)
    assert not rewrite.needs_rewrite(s, RECIPES["illustrious"], sentence)


@pytest.mark.parametrize("out, why", [
    ("", "empty"),
    ("I'm sorry, I can't help with that request.", "refused"),
    ("2girls, yuri, kissing, nude", "changed who's in it (1w1m → 2w0m)"),
    ("1girl, 1boy, hetero, sex, missionary, red hair", ""),
])
def test_rewrite_checks(out, why):
    assert rewrite.accept("a man and a woman having sex, she has red hair", out) == why


@pytest.mark.asyncio
async def test_sentence_becomes_tags_for_an_anime_model(tmp_path):
    seen = {}

    async def llm(system, user):
        seen.update(system=system, user=user)
        return "Prompt: 1girl, 1boy, hetero, sex from behind, red hair, bedroom"

    rewrite.set_llm(llm)
    plan = await plan_generation(_settings(tmp_path), GenerationRequest(
        prompt="anime style, a woman with red hair is being taken from behind by a man "
               "in her bedroom", nsfw=True, model="illustrious"))
    assert "danbooru" in seen["system"] and "explicit" in seen["user"]
    assert "sex from behind" in plan.prompt and "being taken" not in plan.prompt
    assert any("rewrote the prompt as tags" in n for n in plan.notes)


@pytest.mark.asyncio
async def test_bad_rewrite_keeps_her_prompt(tmp_path):
    async def llm(system, user):
        return "2girls, yuri"

    rewrite.set_llm(llm)
    prompt = "anime, a man and a woman are kissing passionately on the couch tonight"
    plan = await plan_generation(_settings(tmp_path), GenerationRequest(
        prompt=prompt, nsfw=True, model="illustrious"))
    assert "kissing passionately" in plan.prompt
    assert any("rewrite skipped" in n for n in plan.notes)


def _write(tmp_path, ledger, fb):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "image_generations.jsonl").write_text("\n".join(json.dumps(r) for r in ledger))
    (logs / "image_feedback.jsonl").write_text("\n".join(json.dumps(r) for r in fb))


def test_pairs_and_liked_weights(tmp_path):
    ck = RECIPES["illustrious"].model
    _write(tmp_path, [
        {"model": ck, "loras": {"A": 0.6, "B": 0.5}, "path": "/x/1.png"},
        {"model": ck, "loras": {"A": 0.8, "C": 0.7}, "path": "/x/2.png"},
        {"model": "other", "loras": {"A": 1.0}, "path": "/x/3.png"},
    ], [{"file": "1.png", "score": 1}, {"file": "2.png", "score": -1},
        {"file": "3.png", "score": 1}])
    s = SimpleNamespace(data_dir=tmp_path)
    pairs = feedback.pair_scores(s)
    assert pairs[("A", "B")] == (1.0, 1) and pairs[("A", "C")] == (-1.0, 1)
    assert feedback.pair_multiplier(pairs, "B", ["A"]) > 1 > \
        feedback.pair_multiplier(pairs, "C", ["A"])
    assert feedback.liked_weight(s, "A", ck) == 0.6  # same checkpoint first
    assert feedback.liked_weight(s, "A", "nope") == 0.8  # any liked image
    assert feedback.liked_weight(s, "C", ck) is None


def test_liked_weight_beats_creator_weight():
    _, w, _ = plan_stack([LoraPick("x", rec=0.9, liked=0.55, source="concept")])
    assert w == {"x": 0.55}
    _, w, _ = plan_stack([LoraPick("x", rec=0.9, liked=0.55, weight=1.0, source="hers")])
    assert w == {"x": 1.0}  # her explicit weight still wins


def _res(name, air, dl):
    return site.CivitaiResource(name=name, type="LORA", model_id=1, version_id=1,
                                version_name="1", air=air, base_model="Illustrious",
                                download_count=dl, tags=["concept"])


@pytest.mark.asyncio
async def test_concept_lora_that_went_better_with_her_lora_wins(tmp_path, monkeypatch):
    async def search(settings, query, **kw):
        return [_res("Missionary A", "urn:air:sdxl:lora:civitai:1@1", 90000),
                _res("Missionary B", "urn:air:sdxl:lora:civitai:2@2", 50000)]

    monkeypatch.setattr(site, "search_models", search)
    hers = "urn:air:sdxl:lora:civitai:9@9"
    _write(tmp_path, [
        {"model": "m", "loras": {hers: 0.8, "urn:air:sdxl:lora:civitai:1@1": 0.7},
         "path": "/x/1.png"},
        {"model": "m", "loras": {hers: 0.8, "urn:air:sdxl:lora:civitai:2@2": 0.7},
         "path": "/x/2.png"},
    ], [{"file": "1.png", "score": -1}, {"file": "2.png", "score": 1},
        {"file": "2.png", "score": 1}])
    s = _settings(tmp_path)
    picks, notes = await loras.concept_picks(s, RECIPES["illustrious"], "missionary",
                                             others=(hers,))
    assert picks[0].air == "urn:air:sdxl:lora:civitai:2@2"
    assert "went better with this before" in notes[0]
    # With no reactions at all, the most downloaded one stays first.
    (tmp_path / "logs" / "image_feedback.jsonl").write_text("")
    picks, _ = await loras.concept_picks(s, RECIPES["illustrious"], "missionary")
    assert picks[0].air == "urn:air:sdxl:lora:civitai:1@1"
