"""Civitai search, prompt-style, and orchestration step building."""

from __future__ import annotations

from pathlib import Path

import pytest

from ophelia.providers.civitai import (
    base_family,
    build_step_input,
    detect_style,
    ecosystem_from_air_or_base,
    ensure_triggers_in_prompt,
    filter_loras_for_checkpoint,
    lora_compatible_with_checkpoint,
    maybe_quality_prefix,
    parse_loras,
    prompt_style_for,
    default_negative_for,
    sanitize_air,
)


def test_ecosystem_from_air():
    assert ecosystem_from_air_or_base(
        "urn:air:sdxl:checkpoint:civitai:1@2"
    ) == "sdxl"
    assert ecosystem_from_air_or_base(
        "urn:air:sd15:lora:civitai:1@2"
    ) == "sd1"
    assert ecosystem_from_air_or_base(
        "urn:air:flux1:checkpoint:civitai:1@2"
    ) == "flux1"


def test_ecosystem_from_base_model():
    assert ecosystem_from_air_or_base("", "Illustrious") == "sdxl"
    assert ecosystem_from_air_or_base("", "Pony") == "sdxl"
    assert ecosystem_from_air_or_base("", "SD 1.5") == "sd1"
    assert ecosystem_from_air_or_base("", "Flux.1 D") == "flux1"


def test_prompt_style():
    assert prompt_style_for("sdxl", "Illustrious") == "danbooru"
    assert prompt_style_for("sdxl", "Pony") == "danbooru"
    assert prompt_style_for("flux1", "Flux.1 D") == "natural"
    assert prompt_style_for("sdxl", "SDXL 1.0") == "mixed"


def test_parse_loras_formats():
    assert parse_loras({"urn:air:sdxl:lora:civitai:1@2": 0.7})[
        "urn:air:sdxl:lora:civitai:1@2"
    ] == 0.7
    d = parse_loras('{"urn:a": 0.5}')
    assert d["urn:a"] == 0.5
    d2 = parse_loras("urn:air:sdxl:lora:civitai:1@2|0.9,urn:air:sdxl:lora:civitai:3@4")
    assert d2["urn:air:sdxl:lora:civitai:1@2"] == 0.9
    assert d2["urn:air:sdxl:lora:civitai:3@4"] == 0.8


def test_ensure_triggers_default_off():
    """Auto-pick must not pollute prompts with character triggers."""
    out = ensure_triggers_in_prompt(
        "gothic AI on circuit-board throne",
        ["maddie, chubby, short ponytail", "SDPHPIXVI", "demon girl"],
    )
    assert out == "gothic AI on circuit-board throne"


def test_ensure_triggers_prepend_legacy():
    out = ensure_triggers_in_prompt(
        "1girl, solo", ["mychar", "style_v2"], mode="prepend"
    )
    assert out.startswith("mychar, style_v2")
    assert "1girl" in out
    out2 = ensure_triggers_in_prompt("mychar, 1girl", ["mychar"], mode="prepend")
    assert out2.count("mychar") == 1


def test_ensure_triggers_append_skips_soup():
    out = ensure_triggers_in_prompt(
        "pale gothic female",
        ["maddie, chubby, short ponytail, pussy", "oktrigger"],
        mode="append",
    )
    assert "pussy" not in out
    assert "maddie" not in out
    assert out.endswith("oktrigger")


def test_sanitize_air():
    assert (
        sanitize_air("urn:air:flux1:checkpoint:civitai:175853?version=3173202")
        == "urn:air:flux1:checkpoint:civitai:175853@3173202"
    )
    assert (
        sanitize_air("urn:air:sdxl:checkpoint:civitai:7937#58885618")
        == "urn:air:sdxl:checkpoint:civitai:7937"
    )


def test_detect_style_not_keyword_trap():
    # "gothic" alone must NOT force anime/illustrious search pollution
    assert detect_style("gothic AI on circuit-board throne") == "sdxl"
    assert detect_style("anime waifu 1girl") == "illustrious"
    assert detect_style("photorealistic portrait") == "realistic"
    assert detect_style("try pony with petite lora") == "pony"
    assert detect_style("gothic vtuber") == "illustrious"


def test_air_kind_and_alias():
    from ophelia.providers.civitai import air_kind, resolve_checkpoint_alias

    assert air_kind("urn:air:sdxl:lora:civitai:1@2") == "lora"
    assert air_kind("urn:air:sdxl:checkpoint:civitai:1@2") == "checkpoint"
    assert resolve_checkpoint_alias("pony").startswith("urn:air:sdxl:checkpoint:")
    assert resolve_checkpoint_alias("illustrious").startswith("urn:air:sdxl:")


def test_lora_compatibility():
    assert lora_compatible_with_checkpoint(
        "urn:air:sdxl:checkpoint:civitai:1@2",
        "Illustrious",
        "urn:air:sdxl:lora:civitai:3@4",
        "Illustrious",
    )
    assert not lora_compatible_with_checkpoint(
        "urn:air:sdxl:checkpoint:civitai:1@2",
        "SDXL 1.0",
        "urn:air:sdxl:lora:civitai:3@4",
        "Pony",
    )
    assert not lora_compatible_with_checkpoint(
        "urn:air:sdxl:checkpoint:civitai:1@2",
        "SDXL 1.0",
        "urn:air:flux1:lora:civitai:3@4",
        "Flux",
    )


def test_filter_loras_drops_pony_on_sdxl():
    kept = filter_loras_for_checkpoint(
        {
            "urn:air:sdxl:lora:civitai:1@2": 0.8,
            "urn:air:sdxl:lora:civitai:3@4": 0.7,
        },
        checkpoint_air="urn:air:sdxl:checkpoint:civitai:9@9",
        checkpoint_base="SDXL 1.0",
        lora_meta={
            "urn:air:sdxl:lora:civitai:1@2": "Pony",
            "urn:air:sdxl:lora:civitai:3@4": "SDXL 1.0",
        },
    )
    assert "urn:air:sdxl:lora:civitai:3@4" in kept
    assert "urn:air:sdxl:lora:civitai:1@2" not in kept


def test_maybe_quality_prefix():
    out = maybe_quality_prefix("1girl, solo", "illustrious")
    assert out.startswith("masterpiece, best quality")
    assert maybe_quality_prefix("a photo of a cat", "realistic") == "a photo of a cat"


@pytest.mark.asyncio
async def test_pick_best_no_loras_no_search():
    from ophelia.config import Settings
    from ophelia.providers.civitai import pick_best_resources

    settings = Settings()
    ck, loras, note = await pick_best_resources(
        settings, "gothic AI on circuit-board throne", want_lora=True
    )
    assert ck is not None
    assert ck.air.startswith("urn:air:sdxl:")
    assert loras == []
    assert "loras=none" in note
    assert ck.trained_words == []


def test_build_step_txt2img_sdxl():
    step = build_step_input(
        prompt="masterpiece, 1girl",
        width=1024,
        height=1024,
        model_air="urn:air:sdxl:checkpoint:civitai:1@2",
        ecosystem="sdxl",
        negative_prompt="worst quality",
        loras={"urn:air:sdxl:lora:civitai:3@4": 0.8},
    )
    assert step["engine"] == "sdcpp"
    assert step["ecosystem"] == "sdxl"
    assert step["operation"] == "createImage"
    assert step["model"].startswith("urn:air:sdxl")
    assert step["negativePrompt"] == "worst quality"
    assert step["loras"]["urn:air:sdxl:lora:civitai:3@4"] == 0.8
    assert "image" not in step


def test_build_step_img2img():
    step = build_step_input(
        prompt="make it night",
        width=1024,
        height=1024,
        model_air="urn:air:sdxl:checkpoint:civitai:1@2",
        ecosystem="sdxl",
        image_url="https://example.com/src.jpg",
        strength=0.65,
    )
    assert step["operation"] == "createVariant"
    assert step["image"] == "https://example.com/src.jpg"
    assert step["strength"] == 0.65


def test_build_step_flux_default_becomes_sdxl():
    """Bare 'flux' must NOT use engine:flux (workflowTemplate 400)."""
    step = build_step_input(
        prompt="a cat in space",
        width=1024,
        height=1024,
        model_air="flux",
    )
    assert step["engine"] == "sdcpp"
    assert step["ecosystem"] == "sdxl"
    assert step["model"].startswith("urn:air:sdxl:")
    assert "operation" in step


def test_build_step_empty_model_uses_sdxl_fallback():
    step = build_step_input(
        prompt="test",
        width=1024,
        height=1024,
        model_air="",
    )
    assert step["engine"] == "sdcpp"
    assert step["model"].startswith("urn:air:")


def test_image_ext_from_bytes():
    from ophelia.providers.media import _image_ext_from_bytes

    assert _image_ext_from_bytes(b"\xff\xd8\xff\xe0rest") == ".jpg"
    assert _image_ext_from_bytes(b"\x89PNG\r\n\x1a\nrest") == ".png"
    assert _image_ext_from_bytes(b"RIFF....WEBP....") == ".webp"


def test_nsfw_auto_prefers_civitai_over_pollinations():
    from ophelia.config import Settings

    assert Settings.NSFW_CAPABLE_PROVIDERS[0] == "civitai"
    assert Settings.NSFW_CAPABLE_PROVIDERS[-1] == "pollinations"


def test_default_negatives():
    assert "worst quality" in default_negative_for("sdxl")
    assert default_negative_for("flux1") == ""


def test_civitai_defaults_to_dynamic_pick() -> None:
    """Menu/env must not lock Civitai; generate_image auto-picks when model omitted."""
    import inspect

    from ophelia.providers.media import _civitai_image

    sig = inspect.signature(_civitai_image)
    assert sig.parameters["auto_pick"].default is True

    src = Path(__file__).resolve().parents[1] / "src" / "ophelia" / "providers" / "media.py"
    body = src.read_text(encoding="utf-8")
    assert "curated general checkpoint" in body or "picks a curated checkpoint" in body
    assert "should_pick = bool(auto_pick) and not explicit_pin" in body
    assert "if not agent_model:\n            auto_pick = True" in body
    assert "reroute_air_to_civitai" in body or "ignored_civitai_urn" in body


def test_base_family():
    assert base_family("Illustrious") == "illustrious"
    assert base_family("Pony") == "pony"
    assert base_family("", "urn:air:sdxl:checkpoint:civitai:1@2") == "sdxl"
