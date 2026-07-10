"""Civitai search, prompt-style, and orchestration step building."""

from __future__ import annotations

from pathlib import Path

from ophelia.providers.civitai import (
    build_step_input,
    ecosystem_from_air_or_base,
    ensure_triggers_in_prompt,
    parse_loras,
    prompt_style_for,
    default_negative_for,
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


def test_ensure_triggers_injected():
    out = ensure_triggers_in_prompt("1girl, solo", ["mychar", "style_v2"])
    assert out.startswith("mychar, style_v2")
    assert "1girl" in out
    # already present — no dup
    out2 = ensure_triggers_in_prompt("mychar, 1girl", ["mychar"])
    assert out2.count("mychar") == 1


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
    assert "picks checkpoint/LoRA per image" in body
    assert "should_pick = bool(auto_pick) and not explicit_pin" in body
    assert "if not agent_model:\n            auto_pick = True" in body
