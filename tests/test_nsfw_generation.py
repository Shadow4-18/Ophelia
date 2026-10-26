"""Explicit images: better auto mode, prompts, checkpoint rotation, concept
LoRAs, variety, learning from his reactions, and adults only."""

from __future__ import annotations

import json
import random
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.providers.civitai import feedback, loras, selection, site, variety
from ophelia.providers.civitai.catalog import RECIPES, pool, recipe_by_name, recipe_for_air
from ophelia.providers.civitai.pipeline import GenerationRequest, plan_generation
from ophelia.providers.civitai.prompting import (
    detect_style_with_reason,
    ensure_subject,
    format_prompt,
    minor_terms,
    negative_for,
    tidy_tags,
)


def _settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, civitai_variety="on", civitai_auto_loras=True,
                civitai_default_style="anime", civitai_checkpoints="", civitai_quality="",
                civitai_max_count=4, civitai_api_key="k")
    base.update(kw)
    return SimpleNamespace(**base)


def _ledger(tmp_path, *records):
    path = tmp_path / "logs" / "image_generations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# --- auto mode: style ---------------------------------------------------------


@pytest.mark.parametrize("prompt,style", [
    ("1girl, photorealistic, nude on a bed", "realistic"),  # was anime: 1girl matched first
    ("amateur selfie, topless in a mirror", "realistic"),
    ("anime girl, selfie, lingerie", "illustrious"),
    ("furry, anthro wolf girl", "pony"),
    ("hentai, x-ray, cum", "illustrious"),
    ("gothic woman on a throne", "sdxl"),  # no style words: the default style
])
def test_style_is_scored_not_first_match(prompt, style):
    assert detect_style_with_reason(prompt)[0] == style


def test_pools_cover_each_style_with_matching_families():
    for style in ("anime", "pony", "realistic"):
        members = pool(style)
        assert len(members) >= 4
        assert all(r.ecosystem == "sdxl" and r.model.startswith("urn:air:sdxl:checkpoint")
                   for r in members)
    assert {r.family for r in pool("pony")} == {"pony"}
    assert recipe_by_name("CyberRealistic Pony").key == "cyberrealistic-pony"
    assert recipe_for_air(RECIPES["hassaku"].model).key == "hassaku"  # pinned AIR keeps settings


# --- prompts --------------------------------------------------------------------


def test_tidy_and_subject_tags():
    assert tidy_tags("nude, long hair, , long_hair, NUDE, (red eyes:1.2, glowing)") == \
        "nude, long hair, (red eyes:1.2, glowing)"
    assert ensure_subject("her, nude, on a bed") == "1girl, solo, her, nude, on a bed"
    assert ensure_subject("her riding him, sex, 1boy").startswith("her")  # has a count tag
    assert ensure_subject("woman, man, sex").startswith("1girl, woman")  # no solo with a partner
    assert ensure_subject("sunset over the sea") == "sunset over the sea"


def test_realistic_pony_merge_gets_photo_tags_and_negatives():
    r = RECIPES["cyberrealistic-pony"]
    p = format_prompt(r, "woman, nude, on a couch", nsfw=True)
    assert p.startswith("score_9, score_8_up, score_7_up, 1girl, solo, woman")
    assert "photo" in p and "source_anime" not in p and "rating_explicit" in p
    neg = negative_for(r, nsfw=True)
    assert "cartoon" in neg and neg.endswith("child, loli, underage")
    assert "child" not in negative_for(RECIPES["illustrious"], nsfw=False)


# --- adults only ------------------------------------------------------------------


@pytest.mark.parametrize("text,bad", [
    ("loli, nude", ["loli"]),
    ("a 15 year old girl, naked", ["15 year old"]),
    ("16yo, explicit", ["16yo"]),
    ("little girl in a bath", ["little girl"]),
    ("adult woman, 25 years old, nude", []),
    ("petite woman, flat chest, nude", []),
    ("my childhood friend, now 24, kidding around in bed", []),
    ("babydoll lingerie, nude", []),
])
def test_minor_terms(text, bad):
    assert minor_terms(text) == bad


@pytest.mark.asyncio
async def test_explicit_prompt_describing_a_minor_is_refused(tmp_path):
    plan = await plan_generation(_settings(tmp_path),
                                 GenerationRequest(prompt="loli, nude", nsfw=True))
    assert "adults only" in plan.error
    ok = await plan_generation(_settings(tmp_path, civitai_auto_loras=False),
                               GenerationRequest(prompt="adult woman, nude", nsfw=True))
    assert not ok.error


# --- variety ----------------------------------------------------------------------


def test_variety_fills_only_what_is_open(tmp_path):
    s = _settings(tmp_path)
    rng = random.Random(1)
    out, added = variety.add_variety(s, "1girl, nude, on a bed, soft lighting", natural=False,
                                     rng=rng)
    assert len(added) == 3  # camera, expression, pose; lighting and setting were given
    assert not any("light" in a for a in added)
    assert out.startswith("1girl, nude, on a bed, soft lighting, ")
    _, added = variety.add_variety(s, "1girl, 1boy, sex, missionary, bedroom", natural=False,
                                   rng=rng)
    assert all(a not in dict.fromkeys(variety._BANKS["pose"][1]) for a in added)  # no solo pose
    full = ", ".join(f"tag{i}" for i in range(40))
    assert variety.add_variety(s, full, natural=False) == (full, [])
    assert not variety.enabled(_settings(tmp_path, civitai_variety="off"))


def test_variety_avoids_recent_picks(tmp_path):
    s = _settings(tmp_path)
    seen = []
    for i in range(6):
        _, added = variety.add_variety(s, "1girl, nude, bedroom, smile, sitting, sunlight",
                                       natural=False, rng=random.Random(i))
        seen.append(added[0])  # only the camera is open
    assert len(set(seen)) == 6


# --- checkpoint rotation + feedback ------------------------------------------------


def test_rotation_spreads_over_the_pool(tmp_path):
    s = _settings(tmp_path)
    head = RECIPES["illustrious"]
    picks = Counter(selection.pick(s, "anime", head, rng=random.Random(i))[0].key
                    for i in range(400))
    assert len(picks) == len(pool("anime"))
    assert picks["illustrious"] == max(picks.values())  # the head keeps the biggest share
    off = _settings(tmp_path, civitai_variety="off")
    assert selection.pick(off, "anime", head) == (head, "")


def test_rotation_avoids_last_model_and_benched(tmp_path):
    s = _settings(tmp_path)
    _ledger(tmp_path, {"model": RECIPES["hassaku"].model})
    w = dict((r.key, wt) for r, wt in selection.weights(s, "anime"))
    assert w["hassaku"] == pytest.approx(RECIPES["hassaku"].weight * 0.35)
    selection.bench(s, RECIPES["nova-anime"].model, "HTTP 400 not available")
    assert "nova-anime" not in {r.key for r, _ in selection.weights(s, "anime")}
    assert {r.family for r, _ in selection.weights(s, "realistic", "pony")} == {"pony"}


def test_reactions_teach_which_checkpoints_he_likes(tmp_path):
    s = _settings(tmp_path)
    liked, disliked = RECIPES["prefect-il"].model, RECIPES["one-obsession"].model
    _ledger(tmp_path,
            {"model": liked, "paths": [str(tmp_path / "civitai_1_1_0.png")]},
            {"model": disliked, "loras": {"urn:air:sdxl:lora:civitai:9@9": 0.7},
             "path": str(tmp_path / "civitai_2_2_0.png")})
    feedback.remember_sent(s, 1, 50, "/x/civitai_1_1_0.png")
    feedback.remember_sent(s, 1, 51, "civitai_2_2_0.png")
    assert feedback.record_reaction(s, 1, 50, "❤") == "civitai_1_1_0.png"
    feedback.record_reaction(s, 1, 50, "🔥")
    feedback.record_reaction(s, 1, 51, "🤮")
    assert feedback.record_reaction(s, 1, 51, "🤔") == ""  # says nothing about the image
    assert feedback.record_reaction(s, 1, 99, "❤") == ""  # not an image she made
    scores = feedback.scores(s)
    assert scores[liked] == (2.0, 2) and scores[disliked] == (-1.0, 1)
    assert scores["urn:air:sdxl:lora:civitai:9@9"] == (-1.0, 1)
    assert feedback.multiplier(scores[liked]) == pytest.approx(1.4)
    assert feedback.multiplier(scores[disliked]) < 1
    w = dict((r.key, wt) for r, wt in selection.weights(s, "anime"))
    # (both were also used lately: 0.7x for the older, 0.35x for the last one)
    assert w["prefect-il"] == pytest.approx(RECIPES["prefect-il"].weight * 1.4 * 0.7)
    assert w["one-obsession"] == pytest.approx(
        RECIPES["one-obsession"].weight * feedback.multiplier((-1.0, 1)) * 0.35)


@pytest.mark.asyncio
async def test_telegram_reaction_on_her_image_is_feedback(tmp_path):
    from telegram import ReactionTypeEmoji

    from ophelia.channels.chat_actions import MessageIndex
    from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw.settings = _settings(tmp_path)
    gw._index = MessageIndex()
    gw.session = SimpleNamespace(note_reaction=AsyncMock())
    gw._allowed = lambda uid: True
    gw._record_sent_photo(SimpleNamespace(chat=SimpleNamespace(id=1), message_id=7),
                          "civitai_9_9_0.png")
    mr = SimpleNamespace(user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1), message_id=7,
                         old_reaction=(), new_reaction=(ReactionTypeEmoji("😍"),))
    await gw.on_reaction(SimpleNamespace(message_reaction=mr), None)
    note = gw.session.note_reaction.await_args.args[1]
    assert "generated images" in note
    line = json.loads((tmp_path / "logs" / "image_feedback.jsonl").read_text())
    assert line["file"] == "civitai_9_9_0.png" and line["score"] == 1.0


# --- concept LoRAs -----------------------------------------------------------------


def _res(name, air, base="Illustrious", dl=10000, words=()):
    return site.CivitaiResource(name=name, type="LORA", model_id=1, version_id=1,
                                version_name="1", air=air, base_model=base,
                                trained_words=list(words), download_count=dl)


@pytest.mark.asyncio
async def test_concept_loras_match_family_and_name(tmp_path, monkeypatch):
    calls = []

    async def search(settings, query, **kw):
        calls.append((query, kw["base_model"]))
        return [
            _res("X-ray glasses", "urn:air:sdxl:lora:civitai:1@1", dl=90000),
            _res("X-ray Pony", "urn:air:sdxl:lora:civitai:2@2", base="Pony", dl=80000),
            _res("Tiny X-ray", "urn:air:sdxl:lora:civitai:3@3", dl=50),
            _res("X-ray (vaginal) LoRA", "urn:air:sdxl:lora:civitai:4@4", dl=40000,
                 words=["<lora:xray:1>, vaginal xray, cross-section, internal, extra"]),
        ]

    monkeypatch.setattr(site, "search_models", search)
    s = _settings(tmp_path)
    found, trig, notes = await loras.pick_loras(s, RECIPES["illustrious"],
                                                "1girl, sex, x-ray, from side")
    assert found == {"urn:air:sdxl:lora:civitai:4@4": 0.75}
    assert trig == ["vaginal xray", "cross-section", "internal"]
    assert "auto LoRA X-ray (vaginal) LoRA" in notes[0]
    assert calls == [("x-ray", "Illustrious")]
    await loras.pick_loras(s, RECIPES["nova-anime"], "x-ray")
    assert len(calls) == 1  # cached
    assert await loras.pick_loras(s, RECIPES["zimage-turbo"], "x-ray") == ({}, [], [])
    assert await loras.pick_loras(_settings(tmp_path, civitai_auto_loras=False),
                                  RECIPES["illustrious"], "x-ray") == ({}, [], [])
    assert loras.concepts_in("facial expression, riding a horse") == []


@pytest.mark.asyncio
async def test_plan_puts_it_together(tmp_path, monkeypatch):
    async def search(settings, query, **kw):
        return [_res("Ahegao", "urn:air:sdxl:lora:civitai:5@5", words=["ahegao"])]

    monkeypatch.setattr(site, "search_models", search)
    monkeypatch.setattr(selection, "pick", lambda s, st, head, **kw: (RECIPES["hassaku"],
                                                                       "rotated in"))
    plan = await plan_generation(_settings(tmp_path), GenerationRequest(
        prompt="anime, her, nude, ahegao", nsfw=True))
    assert plan.recipe.key == "hassaku" and plan.fallback.key == "illustrious"
    assert plan.loras == {"urn:air:sdxl:lora:civitai:5@5": 0.75}
    assert plan.prompt.startswith("masterpiece, best quality, amazing quality, 1girl")
    assert plan.added and all(a in plan.prompt for a in plan.added)
    assert "ahegao" in plan.prompt
    # img2img keeps her composition: no fill-ins, no auto LoRAs.
    edit = await plan_generation(_settings(tmp_path), GenerationRequest(
        prompt="anime, nude, ahegao", nsfw=True, image="https://x/y.png"))
    assert not edit.added and not edit.loras
    # A pinned model: no rotation and no auto LoRAs.
    pinned = await plan_generation(_settings(tmp_path), GenerationRequest(
        prompt="nude, ahegao", nsfw=True, model="pony"))
    assert pinned.recipe.key == "pony" and not pinned.loras and pinned.fallback is None


def test_default_style_mix(monkeypatch):
    from ophelia.providers.civitai import pipeline

    s = MagicMock(civitai_default_style="mix")
    got = {pipeline._default_style(s) for _ in range(60)}
    assert got == {"anime", "realistic"}


def test_retarget_reshapes_for_the_fallback_model():
    from ophelia.providers.civitai.pipeline import Plan

    cr = RECIPES["cyberrealistic-pony"]
    plan = Plan(recipe=cr, prompt=format_prompt(cr, "woman, nude", nsfw=True),
                negative=negative_for(cr, nsfw=True), width=1024, height=1024, steps=30,
                quality="standard", loras={"urn:air:sdxl:lora:civitai:7@7": 0.75}, quantity=1,
                strength=0.7, auto_loras=["urn:air:sdxl:lora:civitai:7@7"],
                raw_prompt="woman, nude", aspect="2:3", nsfw=True)
    plan.retarget(RECIPES["lustify"])
    assert "score_9" not in plan.prompt and plan.loras == {}  # Pony LoRA can't go on LUSTIFY
    assert plan.steps == RECIPES["lustify"].steps and plan.height > plan.width
    assert "score_6" not in plan.negative and plan.negative.endswith("child, loli, underage")
