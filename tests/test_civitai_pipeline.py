"""End-to-end Civitai pipeline against a fake orchestration API: what is
actually submitted (checkpoint, LoRAs, operation, strength), what is logged
to the on-disk ledger, and that failures are reported instead of papered
over. Run with -s to see the per-generation summary lines."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from ophelia.providers import civitai as civ
from ophelia.providers import media

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 400
ILLUSTRIOUS = civ._FALLBACK_ILLUSTRIOUS_AIR
PONY = civ._FALLBACK_PONY_AIR
PONY_LORA = "urn:air:sdxl:lora:civitai:111@1111"
ILLU_LORA = "urn:air:sdxl:lora:civitai:222@2222"

_VERSIONS = {
    1111: ("Pony", ["ponychar"]),
    2222: ("Illustrious", ["illuchar"]),
    290640: ("Pony", []),
    2514310: ("Illustrious", []),
}


@pytest.fixture
def fake_civitai(tmp_path, monkeypatch):
    """Fake site API (model versions) + orchestration + blob storage."""
    calls: dict = {"submitted": [], "uploads": 0, "status": "succeeded", "reject_seed": False}

    async def get_version(settings, vid):
        base, words = _VERSIONS.get(vid, ("", []))
        return civ.CivitaiResource(
            name=f"v{vid}", type="x", model_id=1, version_id=vid, version_name="1",
            air="", base_model=base, trained_words=words,
        )

    monkeypatch.setattr(civ, "get_version", get_version)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v2/consumer/workflows") and request.method == "POST":
            body = json.loads(request.content)
            calls["submitted"].append(body)
            if calls["reject_seed"] and "seed" in body["steps"][0]["input"]:
                return httpx.Response(400, text='{"errors": {"seed": ["unknown property"]}}')
            if calls["status"] != "succeeded":
                return httpx.Response(200, json={"id": "wf1", "status": calls["status"],
                                                 "error": "nsfw level too high"})
            return httpx.Response(200, json={
                "id": "wf1", "status": "succeeded",
                "steps": [{"output": {"images": [
                    {"url": "https://blob.example/out.png"}
                ]}}],
            })
        if path.endswith("/v2/consumer/blobs"):
            calls["uploads"] += 1
            return httpx.Response(200, json={"url": "https://blob.example/src.png"})
        if request.url.host == "blob.example":
            return httpx.Response(200, content=PNG)
        return httpx.Response(404, text="unexpected " + str(request.url))

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    settings = SimpleNamespace(
        civitai_base_url="https://orchestration.civitai.com",
        civitai_api_key="key",
        civitai_image_model="",
        image_nsfw_allowed=True,
        data_dir=tmp_path / "data",
    )
    calls["settings"] = settings
    calls["artifacts"] = tmp_path / "artifacts"
    calls["ledger"] = tmp_path / "data" / "logs" / "image_generations.jsonl"
    return calls


def _ledger(fake) -> list[dict]:
    return [json.loads(ln) for ln in fake["ledger"].read_text().splitlines()]


def _show(label: str, entry: dict) -> None:
    print(f"\n[{label}] op={entry.get('operation')} checkpoint={entry.get('checkpoint')} "
          f"loras={entry.get('loras')} seed={entry.get('seed')} path={entry.get('path')} "
          f"bytes={entry.get('bytes')} error={entry.get('error')}")


async def _gen(fake, prompt, **kw):
    return await media._civitai_image(
        fake["settings"], prompt, "1:1", kw.pop("model", ""), fake["artifacts"], **kw
    )


@pytest.mark.asyncio
async def test_txt2img_illustrious_auto_pick(fake_civitai):
    out = await _gen(fake_civitai, "1girl, goth vtuber, neon city", auto_pick=True)
    step = fake_civitai["submitted"][0]["steps"][0]["input"]
    assert step["operation"] == "createImage"
    assert step["model"] == ILLUSTRIOUS
    assert "loras" not in step  # auto_pick adds none
    assert step["prompt"] == "masterpiece, best quality, 1girl, goth vtuber, neon city"
    entry = _ledger(fake_civitai)[-1]
    _show("illustrious auto-pick", entry)
    # Civitai doesn't report a seed, so we choose one and send it.
    assert entry["checkpoint"] == ILLUSTRIOUS and entry["seed"] == step["seed"]
    assert f"seed={step['seed']}" in out
    assert entry["bytes"] == len(PNG) and out.startswith(f"Image saved to {entry['path']}")
    assert "matched '1girl'" in entry["pick"]


@pytest.mark.asyncio
async def test_txt2img_explicit_pony_checkpoint(fake_civitai):
    await _gen(fake_civitai, "score_9, 1girl", model=PONY, auto_pick=False,
               loras={PONY_LORA: 0.7})
    step = fake_civitai["submitted"][0]["steps"][0]["input"]
    assert step["model"] == PONY and step["loras"] == {PONY_LORA: 0.7}
    assert step["prompt"] == "score_9, 1girl"  # Pony score tags, no "masterpiece"
    # Trigger words are reported, never injected.
    assert "ponychar" not in step["prompt"]
    entry = _ledger(fake_civitai)[-1]
    _show("explicit pony", entry)
    assert entry["checkpoint"] == PONY and entry["loras"] == {PONY_LORA: 0.7}


@pytest.mark.asyncio
async def test_img2img_strength_routes_to_create_variant(fake_civitai, tmp_path):
    src = tmp_path / "inbox.png"
    src.write_bytes(PNG)
    await _gen(fake_civitai, "same pose, make it night", image=str(src), strength=0.7)
    step = fake_civitai["submitted"][0]["steps"][0]["input"]
    assert step["operation"] == "createVariant"
    assert step["image"] == "https://blob.example/src.png" and step["strength"] == 0.7
    assert fake_civitai["uploads"] == 1
    entry = _ledger(fake_civitai)[-1]
    _show("img2img 0.7", entry)
    assert entry["operation"] == "createVariant" and entry["strength"] == 0.7


@pytest.mark.asyncio
async def test_lora_air_in_model_is_moved_and_picks_its_family(fake_civitai):
    out = await _gen(fake_civitai, "1girl, anime", model=PONY_LORA, auto_pick=True)
    step = fake_civitai["submitted"][0]["steps"][0]["input"]
    assert step["model"] == PONY  # Pony LoRA → Pony checkpoint, not Illustrious
    assert step["loras"] == {PONY_LORA: 0.8}
    assert "ponychar" in out  # told about the trigger word she didn't use


@pytest.mark.asyncio
async def test_cross_family_pair_is_refused(fake_civitai):
    out = await _gen(fake_civitai, "1girl", model=ILLUSTRIOUS, auto_pick=False,
                     loras={PONY_LORA: 0.8})
    assert out.startswith("Refused: LoRA/checkpoint family mismatch")
    assert fake_civitai["submitted"] == []
    assert "error" in _ledger(fake_civitai)[-1]


@pytest.mark.asyncio
async def test_failed_workflow_surfaces_real_error(fake_civitai):
    fake_civitai["status"] = "failed"
    out = await _gen(fake_civitai, "1girl")
    assert out.startswith("Civitai workflow failed") and "nsfw level too high" in out
    assert "Image saved" not in out
    assert "nsfw level too high" in _ledger(fake_civitai)[-1]["error"]


@pytest.mark.asyncio
async def test_img2img_never_falls_back_to_txt2img(monkeypatch, tmp_path):
    tried = []

    async def dispatch(provider, **kw):
        tried.append((provider, kw.get("image")))
        return "Civitai img2img source failed: boom"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    monkeypatch.setattr(media, "_fallback_image_providers", lambda *a, **k: ["pollinations"])
    settings = SimpleNamespace(image_nsfw_allowed=True, civitai_api_key="key")
    stack = SimpleNamespace(image_provider_for=lambda nsfw: "xai",
                            image_model_for=lambda p, nsfw: "grok")
    out = await media.generate_image(settings, stack, "night", artifacts_dir=tmp_path,
                                     image="/x.png", strength=0.7)
    assert {p for p, _ in tried} == {"civitai"}  # rerouted, and no pollinations
    assert "img2img source failed" in out

    settings.civitai_api_key = ""
    tried.clear()
    out = await media.generate_image(settings, stack, "night", artifacts_dir=tmp_path,
                                     image="/x.png")
    assert tried == [] and out.startswith("img2img needs Civitai")


def test_style_map_is_explicit_and_word_bounded():
    assert civ.detect_style_with_reason("anthropic logo") == ("sdxl", "")
    assert civ.detect_style_with_reason("score_9_up, 1girl") == ("pony", "score_9")
    assert civ.detect_style_with_reason("photos of a beach") == ("realistic", "photos")


def test_family_error_only_when_verified():
    assert civ.lora_family_error(ILLUSTRIOUS, "Illustrious", PONY_LORA, "Pony")
    assert civ.lora_family_error(ILLUSTRIOUS, "", "urn:air:sd1:lora:civitai:1@2", "")
    assert not civ.lora_family_error(ILLUSTRIOUS, "Illustrious", ILLU_LORA, "Illustrious")
    assert not civ.lora_family_error(ILLUSTRIOUS, "", PONY_LORA, "")  # can't tell


@pytest.mark.asyncio
async def test_seed_field_refused_retries_without_it(fake_civitai):
    fake_civitai["reject_seed"] = True
    out = await _gen(fake_civitai, "1girl")
    assert out.startswith("Image saved to")
    assert "seed" not in fake_civitai["submitted"][-1]["steps"][0]["input"]
    assert _ledger(fake_civitai)[-1]["seed"] is None


def test_pony_gets_score_tags():
    assert civ.maybe_quality_prefix("source_anime, 1girl", "pony") == (
        "score_9, score_8_up, score_7_up, source_anime, 1girl"
    )
