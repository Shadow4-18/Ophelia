"""End-to-end Civitai pipeline against a fake orchestration API: what is
actually submitted per model, what is logged to the on-disk ledger, batch
and quality profiles, late (pending) delivery, and that failures are
reported instead of retried or papered over. Run with -s to see the
per-generation summary lines."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ophelia.providers import civitai as civ
from ophelia.providers import media
from ophelia.providers.civitai import pipeline, site
from ophelia.providers.civitai.pending import PendingLoop, PendingStore

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 400
ILLUSTRIOUS = civ.WAI_ILLUSTRIOUS_AIR
PONY = civ.PONY_V6_AIR
PONY_LORA = "urn:air:sdxl:lora:civitai:111@1111"
ILLU_LORA = "urn:air:sdxl:lora:civitai:222@2222"

_VERSIONS = {
    1111: ("Pony", ["ponychar"]),
    2222: ("Illustrious", ["illuchar"]),
    290640: ("Pony", []),
    2883731: ("Illustrious", []),
}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Fake site API + orchestration + blob storage. Knobs: status (what a
    submit returns), poll_status (what GET returns), reject_seed, stale."""
    calls: dict = {
        "submitted": [], "params": [], "uploads": 0, "status": "succeeded",
        "poll_status": "succeeded", "reject_seed": False, "stale": False,
    }

    async def get_version(settings, vid):
        base, words = _VERSIONS.get(vid, ("", []))
        return civ.CivitaiResource(
            name=f"v{vid}", type="x", model_id=1, version_id=vid, version_name="1",
            air="", base_model=base, trained_words=words,
        )

    monkeypatch.setattr(site, "get_version", get_version)

    def wf(status: str, quantity: int) -> dict:
        body = {"id": "wf1", "status": status, "cost": {"total": 10}}
        if status == "succeeded":
            body["steps"] = [{"output": {"images": [
                {"url": f"https://blob.example/out{i}.png"} for i in range(quantity)
            ]}}]
        elif status == "failed":
            body["steps"] = [{"status": "failed", "reason": "blocked",
                              "output": {"images": [{"blockedReason": "nsfw level too high"}]}}]
        return body

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v2/consumer/workflows") and request.method == "POST":
            body = json.loads(request.content)
            calls["submitted"].append(body)
            calls["params"].append(dict(request.url.params))
            inp = body["steps"][0]["input"]
            if calls["reject_seed"] and "seed" in inp:
                return httpx.Response(400, text='{"errors": {"seed": ["unknown property"]}}')
            if calls["stale"] and inp.get("model") == ILLUSTRIOUS:
                return httpx.Response(400, text="Model version not found")
            calls["quantity"] = inp.get("quantity", 1)
            return httpx.Response(200, json=wf(calls["status"], calls["quantity"]))
        if path.startswith("/v2/consumer/workflows/") and request.method == "GET":
            return httpx.Response(200, json=wf(calls["poll_status"], calls.get("quantity", 1)))
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

    # Fake clock: polling sleeps advance it instead of waiting.
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(pipeline, "_now", lambda: clock["t"])
    monkeypatch.setattr(pipeline.asyncio, "sleep", fake_sleep)

    settings = SimpleNamespace(
        civitai_base_url="https://orchestration.civitai.com",
        civitai_api_key="key",
        civitai_image_model="",
        civitai_quality="standard",
        civitai_default_style="anime",
        civitai_checkpoints="",
        civitai_max_count=4,
        civitai_wait_seconds=180,
        civitai_pending_hours=6,
        image_nsfw_allowed=True,
        data_dir=tmp_path / "data",
    )
    calls.update(settings=settings, artifacts=tmp_path / "artifacts",
                 ledger=tmp_path / "data" / "logs" / "image_generations.jsonl")
    return calls


def _ledger(fake) -> list[dict]:
    return [json.loads(ln) for ln in fake["ledger"].read_text().splitlines()]


def _input(fake, i: int = -1) -> dict:
    return fake["submitted"][i]["steps"][0]["input"]


def _show(label: str, entry: dict) -> None:
    print(f"\n[{label}] recipe={entry.get('recipe')} op={entry.get('operation')} "
          f"model={entry.get('model')} loras={entry.get('loras')} seed={entry.get('seed')} "
          f"est={entry.get('est_buzz')} cost={entry.get('cost')} paths={entry.get('paths')} "
          f"error={entry.get('error')}")


async def _gen(fake, prompt, **kw):
    kw.setdefault("nsfw", True)
    return await pipeline.run_generation(
        fake["settings"], pipeline.GenerationRequest(prompt=prompt, **kw), fake["artifacts"]
    )


# --- what gets sent ---------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_pick_anime_illustrious(fake):
    out = await _gen(fake, "1girl, goth vtuber, neon city")
    inp = _input(fake)
    assert inp["model"] == ILLUSTRIOUS and inp["ecosystem"] == "sdxl"
    assert inp["steps"] == 20 and inp["cfgScale"] == 6.0 and inp["quantity"] == 1
    assert inp["width"] == 1024 and inp["height"] == 1024
    assert inp["prompt"] == (
        "masterpiece, best quality, amazing quality, 1girl, goth vtuber, neon city, nsfw"
    )
    assert "censor" in inp["negativePrompt"]
    assert "loras" not in inp  # auto adds none
    assert fake["submitted"][-1]["allowMatureContent"] is True
    entry = _ledger(fake)[-1]
    _show("illustrious auto-pick", entry)
    assert entry["seed"] == inp["seed"] and entry["cost"] == {"total": 10}
    assert entry["est_buzz"] == 8.0
    assert out.startswith(f"Image saved to {entry['path']}") and "~8.0 Buzz" in out


@pytest.mark.asyncio
async def test_no_style_word_uses_default_style(fake):
    await _gen(fake, "a woman on a balcony at night")
    assert _input(fake)["model"] == ILLUSTRIOUS
    fake["settings"].civitai_default_style = "realistic"
    await _gen(fake, "a woman on a balcony at night")
    assert _input(fake)["model"] == civ.LUSTIFY_AIR


@pytest.mark.asyncio
async def test_sfw_request_does_not_force_yellow_buzz(fake):
    await _gen(fake, "1girl, portrait", nsfw=False)
    assert "allowMatureContent" not in fake["submitted"][-1]
    assert "allowMatureContent" not in fake["params"][-1]
    assert "nsfw" not in _input(fake)["prompt"]


@pytest.mark.asyncio
async def test_pinned_pony_with_lora(fake):
    await _gen(fake, "1girl, goth", model="pony", loras={PONY_LORA: 0.7})
    inp = _input(fake)
    assert inp["model"] == PONY and inp["loras"] == {PONY_LORA: 0.7}
    assert inp["steps"] == 25 and inp["cfgScale"] == 7.0
    assert inp["prompt"].startswith("score_9, score_8_up, score_7_up, 1girl")
    assert "source_anime" in inp["prompt"] and "rating_explicit" in inp["prompt"]
    assert "ponychar" not in inp["prompt"]  # triggers are reported, not injected
    _show("pinned pony", _ledger(fake)[-1])


@pytest.mark.asyncio
async def test_lora_in_model_moves_and_picks_its_family(fake):
    out = await _gen(fake, "1girl, anime", model=PONY_LORA)
    inp = _input(fake)
    assert inp["model"] == PONY and inp["loras"] == {PONY_LORA: 0.8}
    assert "ponychar" in out  # told about the trigger word she didn't use


@pytest.mark.asyncio
async def test_cross_family_pair_is_refused(fake):
    out = await _gen(fake, "1girl", model="illustrious", loras={PONY_LORA: 0.8})
    assert out.startswith("Refused: LoRA/checkpoint family mismatch")
    assert fake["submitted"] == []
    assert "error" in _ledger(fake)[-1]


@pytest.mark.asyncio
async def test_img2img_uploads_and_creates_variant(fake, tmp_path):
    src = tmp_path / "inbox.png"
    src.write_bytes(PNG)
    await _gen(fake, "1girl, same pose, night", image=str(src), strength=0.7)
    inp = _input(fake)
    assert inp["operation"] == "createVariant"
    assert inp["image"] == "https://blob.example/src.png" and inp["strength"] == 0.7
    assert fake["uploads"] == 1
    _show("img2img 0.7", _ledger(fake)[-1])


@pytest.mark.asyncio
async def test_txt2img_only_model_with_image(fake, tmp_path):
    out = await _gen(fake, "1girl", model="anima", image="https://x.example/a.png")
    assert out.startswith("Refused:") and "text-to-image only" in out
    await _gen(fake, "a photo of a woman", image="https://x.example/a.png")
    assert _input(fake)["model"] == civ.LUSTIFY_AIR  # realistic, img2img-capable


@pytest.mark.asyncio
async def test_batch_saves_every_image(fake):
    out = await _gen(fake, "1girl, beach", count=3)
    assert _input(fake)["quantity"] == 3
    entry = _ledger(fake)[-1]
    assert len(entry["paths"]) == 3 and len(set(entry["paths"])) == 3
    assert out.count("Image saved to") == 3
    assert all(Path(p).is_file() for p in entry["paths"])
    await _gen(fake, "1girl", count=9)
    assert _input(fake)["quantity"] == 4  # capped


@pytest.mark.asyncio
async def test_quality_profiles_cost_more(fake):
    await _gen(fake, "1girl", quality="high")
    high = _input(fake)
    assert high["steps"] == 24 and high["width"] * high["height"] > 1024 * 1024
    assert high["width"] % 16 == 0
    assert _ledger(fake)[-1]["est_buzz"] > 8.0


@pytest.mark.asyncio
async def test_wide_aspect_sizes_are_multiples_of_16(fake):
    await _gen(fake, "1girl", aspect_ratio="16:9")
    inp = _input(fake)
    assert inp["width"] % 16 == 0 and inp["height"] % 16 == 0
    assert inp["width"] > inp["height"]


@pytest.mark.parametrize(
    "recipe, expect, absent",
    [
        ("anima", {"ecosystem": "anima", "cfgScale": 4.0, "steps": 30}, ["model"]),
        ("zimage-turbo", {"ecosystem": "zImage", "model": "turbo", "cfgScale": 1.0,
                          "steps": 9}, ["negativePrompt"]),
        ("qwen", {"ecosystem": "qwen", "model": "20b", "version": "latest"}, []),
        ("flux1", {"ecosystem": "flux1", "diffuserModel": civ.catalog.FLUX1_DEV_DIFFUSER},
         ["model"]),
        ("flux2-klein", {"engine": "flux2", "model": "klein", "modelVersion": "4b"},
         ["ecosystem"]),
    ],
)
def test_request_shapes_per_model(recipe, expect, absent):
    r = civ.RECIPES[recipe]
    w, h, steps = civ.render_settings(r, "standard", "1:1")
    inp = civ.build_input(r, prompt="x", width=w, height=h, steps=steps, negative="bad",
                          quantity=2)
    for k, v in expect.items():
        assert inp[k] == v, (recipe, k, inp)
    for k in absent:
        assert k not in inp, (recipe, k)
    variant = civ.build_input(civ.RECIPES["qwen"], prompt="x", width=w, height=h, steps=20,
                              image_url="https://a/b.png")
    assert "width" not in variant and variant["operation"] == "createVariant"


# --- failures ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_workflow_surfaces_real_error(fake):
    fake["status"] = "failed"
    out = await _gen(fake, "1girl")
    assert out.startswith("Civitai workflow failed") and "nsfw level too high" in out
    assert "Image saved" not in out
    assert "nsfw level too high" in _ledger(fake)[-1]["error"]


@pytest.mark.asyncio
async def test_seed_field_refused_retries_without_it(fake):
    fake["reject_seed"] = True
    out = await _gen(fake, "1girl")
    assert out.startswith("Image saved to")
    assert "seed" not in _input(fake)
    assert _ledger(fake)[-1]["seed"] is None


@pytest.mark.asyncio
async def test_removed_checkpoint_version_uses_latest(fake, monkeypatch):
    fake["stale"] = True
    newer = "urn:air:sdxl:checkpoint:civitai:827184@9999999"

    async def latest(settings, mid, base=""):
        assert mid == 827184 and base == "Illustrious"
        return newer

    monkeypatch.setattr(site, "latest_version_air", latest)
    out = await _gen(fake, "1girl")
    assert _input(fake)["model"] == newer and out.startswith("Image saved to")
    assert "stored version gone" in out


# --- slow jobs --------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_job_is_delivered_later(fake):
    fake["status"] = fake["poll_status"] = "processing"
    out = await _gen(fake, "1girl", deliver_to="telegram:42")
    assert out.startswith("Pending:") and "wf1" in out
    assert _ledger(fake)[-1]["pending"] is True
    store = PendingStore(fake["settings"])
    assert [i["workflow_id"] for i in store.load()] == ["wf1"]

    delivered = []

    async def deliver(paths, caption, channel):
        delivered.append((paths, channel))
        return True

    loop = PendingLoop(fake["settings"], deliver_media=deliver)
    assert await loop.check_once() == 0  # still processing: keep waiting
    fake["poll_status"] = "succeeded"
    assert await loop.check_once() == 1
    (paths, channel), = delivered
    assert channel == "telegram:42" and paths[0].is_file()
    assert store.load() == []
    entry = _ledger(fake)[-1]
    assert entry["late"] is True and entry["path"] == str(paths[0])
    # The UI loop leaves bot jobs alone and vice versa.
    fake["status"] = fake["poll_status"] = "processing"
    await _gen(fake, "1girl", deliver_to="ui:local")
    fake["poll_status"] = "succeeded"
    assert await loop.check_once() == 0
    assert await PendingLoop(fake["settings"], ui=True).check_once() == 1


@pytest.mark.asyncio
async def test_slow_job_failure_is_reported(fake):
    fake["status"] = fake["poll_status"] = "processing"
    await _gen(fake, "1girl", deliver_to="telegram:42")
    fake["poll_status"] = "failed"
    notes = []

    async def text(msg, channel):
        notes.append((msg, channel))

    assert await PendingLoop(fake["settings"], deliver_text=text).check_once() == 1
    assert "nsfw level too high" in notes[0][0] and notes[0][1] == "telegram:42"


# --- routing and retries (generate_image) -----------------------------------


def _stack(provider="xai"):
    return SimpleNamespace(image_provider_for=lambda nsfw: "civitai" if nsfw else provider,
                           image_model_for=lambda p, nsfw: "grok-imagine-image")


@pytest.mark.asyncio
async def test_civitai_failure_not_retried_unless_asked(monkeypatch, tmp_path):
    calls = []

    async def dispatch(provider, **kw):
        calls.append(provider)
        return "Civitai workflow failed: blocked"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    settings = SimpleNamespace(image_nsfw_allowed=True, civitai_api_key="k",
                               image_max_attempts=5)
    out = await media.generate_image(settings, _stack(), "x", artifacts_dir=tmp_path, nsfw=True)
    assert calls == ["civitai"] and out.startswith("Image generation failed")
    calls.clear()
    out = await media.generate_image(settings, _stack(), "x", artifacts_dir=tmp_path,
                                     nsfw=True, attempts=3)
    assert calls == ["civitai"] * 3 and "(3 attempts)" in out
    calls.clear()
    await media.generate_image(settings, _stack(), "x", artifacts_dir=tmp_path,
                               nsfw=True, attempts=50)
    assert len(calls) == 5  # capped by OPHELIA_IMAGE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_connection_error_gets_one_free_retry(monkeypatch, tmp_path):
    calls = []

    async def dispatch(provider, **kw):
        calls.append(provider)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return f"Image saved to {tmp_path / 'a.png'}"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    settings = SimpleNamespace(image_nsfw_allowed=True, civitai_api_key="k")
    out = await media.generate_image(settings, _stack(), "x", artifacts_dir=tmp_path, nsfw=True)
    assert calls == ["civitai", "civitai"] and out.startswith("Image saved to")


@pytest.mark.asyncio
async def test_pending_is_not_a_failure(monkeypatch, tmp_path):
    async def dispatch(provider, **kw):
        return "Pending: still rendering"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    monkeypatch.setattr(media, "_fallback_image_providers", lambda *a, **k: ["pollinations"])
    settings = SimpleNamespace(image_nsfw_allowed=True, civitai_api_key="k")
    out = await media.generate_image(settings, _stack(), "x", artifacts_dir=tmp_path, nsfw=True)
    assert out == "Pending: still rendering"


@pytest.mark.asyncio
async def test_img2img_routing(monkeypatch, tmp_path):
    seen = []

    async def dispatch(provider, **kw):
        seen.append((provider, kw.get("image")))
        return "Civitai img2img source failed: boom"

    monkeypatch.setattr(media, "_dispatch_image_provider", dispatch)
    monkeypatch.setattr(media, "_fallback_image_providers", lambda *a, **k: ["pollinations"])
    settings = SimpleNamespace(image_nsfw_allowed=True, civitai_api_key="key")
    # SFW edit stays on Grok; explicit edit goes to Civitai; never a fallback.
    await media.generate_image(settings, _stack(), "night", artifacts_dir=tmp_path, image="/x.png")
    await media.generate_image(settings, _stack(), "night", artifacts_dir=tmp_path,
                               image="/x.png", nsfw=True)
    assert seen == [("xai", "/x.png"), ("civitai", "/x.png")]
    # A backend that can't edit: Civitai if there's a key, otherwise a clear no.
    seen.clear()
    await media.generate_image(settings, _stack("pollinations"), "night",
                               artifacts_dir=tmp_path, image="/x.png")
    assert seen == [("civitai", "/x.png")]
    settings.civitai_api_key = ""
    out = await media.generate_image(settings, _stack("pollinations"), "night",
                                     artifacts_dir=tmp_path, image="/x.png")
    assert out.startswith("img2img needs Grok or Civitai")


@pytest.mark.asyncio
async def test_grok_edit_request(monkeypatch, tmp_path):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"url": "https://img.x.ai/e.png"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}))

    async def save(**kw):
        return tmp_path / "edit.png"

    monkeypatch.setattr(media, "_save_image_artifact", save)

    class _Backend(media.XAIBackend):
        def __init__(self):
            pass

        async def bearer_fresh(self):
            return "tok"

    src = tmp_path / "in.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"x" * 50)
    stack = SimpleNamespace(backend=lambda role: _Backend())
    settings = SimpleNamespace(xai_base_url="https://api.x.ai/v1")
    out = await media._xai_image_edit(settings, stack, "make it night", str(src),
                                      "grok-imagine-image", tmp_path)
    assert sent["url"] == "https://api.x.ai/v1/images/edits"
    assert sent["body"]["image"]["type"] == "image_url"
    assert sent["body"]["image"]["url"].startswith("data:image/jpeg;base64,")
    assert out == f"Image saved to {tmp_path / 'edit.png'}"


@pytest.mark.asyncio
async def test_guests_get_no_batches_retries_or_pricier_quality(monkeypatch):
    from ophelia.tools import registry as reg_mod

    captured = {}

    async def fake_generate(settings, stack, prompt, **kw):
        captured.update(kw)
        return "Refused: test"

    monkeypatch.setattr(reg_mod, "generate_image", fake_generate)
    reg = reg_mod.ToolRegistry.__new__(reg_mod.ToolRegistry)
    reg.settings = SimpleNamespace(image_nsfw_allowed=True)
    reg.stack = None
    reg.artifacts_dir = Path("/tmp")
    reg.set_owner(False)
    await reg._generate_image("x", quality="max", count=4, attempts=5)
    assert (captured["quality"], captured["count"], captured["attempts"]) == ("standard", 1, 1)
    reg.set_owner(True)
    reg._current_sender_channel = "telegram:7"
    await reg._generate_image("x", quality="max", count=4, attempts=5)
    assert (captured["quality"], captured["count"], captured["attempts"]) == ("max", 4, 5)
    assert captured["deliver_to"] == "telegram:7"


# --- style rules / families / storage ---------------------------------------


def test_style_map_is_explicit_and_word_bounded():
    assert civ.detect_style_with_reason("anthropic logo") == ("sdxl", "")
    assert civ.detect_style_with_reason("score_9_up, 1girl") == ("pony", "score_9")
    assert civ.detect_style_with_reason("photos of a beach") == ("realistic", "photos")


def test_family_error_only_when_verified():
    assert civ.lora_family_error(ILLUSTRIOUS, "Illustrious", PONY_LORA, "Pony")
    assert civ.lora_family_error(ILLUSTRIOUS, "", "urn:air:sd1:lora:civitai:1@2", "")
    assert civ.lora_family_error("", "", "urn:air:sdxl:lora:civitai:1@2", "", ecosystem="anima")
    assert not civ.lora_family_error(ILLUSTRIOUS, "Illustrious", ILLU_LORA, "Illustrious")
    assert not civ.lora_family_error(ILLUSTRIOUS, "", PONY_LORA, "")  # can't tell


def test_generated_images_are_never_cleaned_up(tmp_path):
    from ophelia.storage import housekeep

    art = tmp_path / "artifacts"
    art.mkdir()
    img = art / "civitai_1_2_0.png"
    img.write_bytes(PNG)
    import os

    os.utime(img, (0, 0))  # ancient
    housekeep(tmp_path, screenshot_hours=1, chat_media_days=1)
    assert img.is_file()


def test_key_source_explains_where_the_key_comes_from(tmp_path, monkeypatch):
    from ophelia.config import Settings
    from ophelia.providers.civitai.orchestration import describe_key_source

    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    monkeypatch.delenv("CIVITAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "CIVITAI_API_KEY=oldkey123456789\nCIVITAI_API_KEY=newkey987654321\n", encoding="utf-8")
    s = Settings(_env_file=tmp_path / ".env")
    lines = "\n".join(describe_key_source(s))
    assert "newk…4321" in lines and "2 lines set it; the LAST one is used" in lines
    assert "oldkey123456789" not in lines and "newkey987654321" not in lines  # never printed
    monkeypatch.setenv("CIVITAI_API_KEY", "shellkey12345678")
    lines = "\n".join(describe_key_source(Settings(_env_file=tmp_path / ".env")))
    assert "shell environment" in lines
