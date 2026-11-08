"""Every base model ("family") Civitai runs: names resolve, requests carry
only the fields each engine's schema lists, and auto mode never picks them."""

from __future__ import annotations

import pytest

from ophelia.providers import civitai as civ
from ophelia.providers.civitai import catalog, pipeline
from ophelia.providers.civitai.catalog import EXTRA_RECIPES, recipe_by_name

from tests.test_civitai_pipeline import _gen, _input, fake  # noqa: F401  (fixture)


@pytest.mark.parametrize("name, key", [
    ("Krea 2", "krea2"), ("Seedream 4", "seedream-4"), ("Seedream", "seedream"),
    ("Nano Banana", "nano-banana-2"), ("HiDream", "hidream"), ("Ideogram", "ideogram"),
    ("Ming", "ming"), ("Muse", "muse"), ("MAI", "mai"), ("Mage Flow", "mageflow"),
    ("Wan Image", "wan-image"), ("Qwen 2", "qwen-2"), ("Qwen 3", "qwen-3"),
    ("Ernie", "ernie"), ("Boogu", "boogu"), ("Kontext", "flux-kontext"),
    ("Flux.2", "flux2-pro"), ("Flux.3", "flux3"), ("MOSS", "moss"), ("Chroma", "chroma"),
    ("Lens", "lens"), ("Pony V7", "pony-v7"),
])
def test_site_family_names_resolve(name, key):
    assert recipe_by_name(name).key == key


def test_grok_stays_on_xai():
    assert recipe_by_name("grok") is None


# Allowed fields per engine leaf (orchestration spec, 2026-11).
_COMFY = {"engine", "ecosystem", "model", "operation", "prompt", "negativePrompt", "steps",
          "cfgScale", "seed", "quantity", "width", "height", "variant"}
_ALLOWED = {
    "comfy": _COMFY,
    "seedream": {"engine", "version", "prompt", "width", "height", "guidanceScale", "seed",
                 "quantity"},
    "google": {"engine", "model", "prompt", "aspectRatio", "negativePrompt", "seed",
               "quantity"},
    "fal": {"engine", "model", "operation", "prompt", "aspectRatio", "imageSize",
            "negativePrompt", "seed", "quantity"},
    "wan": {"engine", "version", "provider", "operation", "prompt", "imageSize",
            "negativePrompt", "cfgScale", "seed", "quantity"},
    "qwen": {"engine", "model", "operation", "prompt", "width", "height", "negativePrompt",
             "seed", "quantity"},
    "flux2": {"engine", "model", "operation", "prompt", "width", "height", "seed", "quantity",
              "guidanceScale", "steps"},
    "flux1-pro": {"engine", "model", "prompt", "aspectRatio", "seed", "quantity"},
    "flux1-kontext": {"engine", "model", "prompt", "aspectRatio", "guidanceScale", "seed",
                      "quantity"},
    "openai": {"engine", "model", "operation", "prompt", "width", "height", "quantity"},
}


@pytest.mark.parametrize("recipe", EXTRA_RECIPES, ids=lambda r: r.key)
@pytest.mark.parametrize("aspect", ["1:1", "9:16", "16:9"])
def test_requests_only_use_the_engines_fields(recipe, aspect):
    w, h, steps = civ.render_settings(recipe, "max", aspect)
    inp = civ.build_input(recipe, prompt="a woman on a beach", width=w, height=h,
                          steps=steps, negative="blurry", quantity=9, seed=3)
    assert set(inp) <= _ALLOWED[recipe.engine], set(inp) - _ALLOWED[recipe.engine]
    assert inp["engine"] == recipe.engine and inp["prompt"] == "a woman on a beach"
    assert 1 <= inp["quantity"] <= recipe.max_quantity
    if "width" in inp:
        assert recipe.min_side <= inp["width"] <= recipe.max_side
        assert recipe.min_side <= inp["height"] <= recipe.max_side
    if "steps" in inp:
        assert inp["steps"] <= recipe.max_steps
    if not recipe.negative:
        assert "negativePrompt" not in inp
    ratio = w / h
    if "aspectRatio" in inp:
        a, b = (int(x) for x in inp["aspectRatio"].split(":"))
        assert (a > b) == (ratio > 1) and (a == b) == (ratio == 1)
    if "imageSize" in inp:
        want = "square" if ratio == 1 else ("portrait" if ratio < 1 else "landscape")
        assert inp["imageSize"].startswith(want)


def test_specific_shapes():
    def build(key):
        r = civ.RECIPES[key]
        w, h, steps = civ.render_settings(r, "standard", "1:1")
        return civ.build_input(r, prompt="x", width=w, height=h, steps=steps,
                               negative="bad", seed=1)
    assert build("krea2") == {
        "engine": "comfy", "ecosystem": "krea2", "model": "raw", "operation": "createImage",
        "prompt": "x", "width": 1024, "height": 1024, "negativePrompt": "bad",
        "cfgScale": 4.0, "steps": 20, "quantity": 1, "seed": 1}
    assert build("chroma")["model"] == catalog.CHROMA_HD_AIR
    assert build("hidream")["variant"] == "full" and "model" not in build("hidream")
    assert build("seedream-4")["version"] == "v4" and build("seedream-4")["guidanceScale"] == 2.5
    nb = build("nano-banana-pro")
    assert nb["aspectRatio"] == "1:1" and "seed" not in nb and "width" not in nb
    assert "negativePrompt" not in build("ideogram4")


def test_auto_mode_never_picks_them():
    for style in ("anime", "pony", "realistic"):
        assert not any(r in EXTRA_RECIPES for r in civ.catalog.pool(style))


def test_catalog_text_stays_compact():
    text = catalog.catalog_text(notes=False)
    assert "chroma*" in text and "seedream" in text and "krea2" in text
    assert len(text) < 2600


@pytest.mark.asyncio
async def test_named_family_runs_end_to_end(fake):  # noqa: F811
    out = await _gen(fake, "a woman reading in a sunlit cafe", model="Krea 2", nsfw=False)
    inp = _input(fake)
    assert inp["engine"] == "comfy" and inp["ecosystem"] == "krea2"
    assert "negativePrompt" in inp and "sampleMethod" not in inp
    assert "priced by Civitai" in out


@pytest.mark.asyncio
async def test_hosted_engine_warns_on_explicit(fake):  # noqa: F811
    out = await _gen(fake, "nude woman", model="seedream")
    assert "own filter" in out


@pytest.mark.asyncio
async def test_family_with_image_is_refused(fake, tmp_path):  # noqa: F811
    src = tmp_path / "s.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    plan = await pipeline.plan_generation(
        fake["settings"], pipeline.GenerationRequest(prompt="x", model="hidream",
                                                     image=str(src), nsfw=True))
    assert plan.error and "text-to-image only" in plan.error
