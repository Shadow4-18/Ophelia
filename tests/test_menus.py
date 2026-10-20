"""The setup menus: engine, settings screens, setup sections, launcher.

Curses itself is exercised by hand in a real terminal; these pin the logic
around it (what gets written, what's left alone, what's rejected).
"""

from __future__ import annotations

import json
import time

import pytest

from ophelia.setup import fields as F
from ophelia.setup import interactive, launcher, tui
from ophelia.setup.env_io import read_env_key, write_env_updates


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    for mod in (interactive, F):
        monkeypatch.setattr(mod, "pause", lambda *a, **k: None, raising=False)
    return tmp_path


# --- engine ----------------------------------------------------------------


def test_numbered_fallback_skips_headers(monkeypatch):
    items = [tui.Header("— A —"), "one", "two", tui.Header("— B —"), "three"]
    monkeypatch.setattr("builtins.input", lambda *_: "3")
    assert tui._radio_numbered_fallback("t", items, 1, -1, "") == 4
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert tui._radio_numbered_fallback("t", items, 2, -1, "") == 2
    monkeypatch.setattr("builtins.input", lambda *_: "q")
    assert tui._radio_numbered_fallback("t", items, 1, -1, "") == -1


def test_checkbox_or_none_tells_back_from_saved(monkeypatch):
    monkeypatch.setattr(tui, "_use_curses", lambda: False)
    answers = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert tui.checkbox_or_none("t", ["a", "b"], selected={0}) is None
    answers = iter(["1", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert tui.checkbox_or_none("t", ["a", "b"], selected={0}) == set()


def test_prompt_text_clear_and_validate(monkeypatch):
    answers = iter(["-"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert tui.prompt_text("x", default="old", clearable=True) == ""
    answers = iter(["bad", "good"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    got = tui.prompt_text("x", validate=lambda v: None if v == "good" else "nope")
    assert got == "good"
    answers = iter([""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert tui.prompt_text("x", default="keep") == "keep"


# --- env file ---------------------------------------------------------------


def test_env_roundtrip_keeps_backslashes(home):
    value = 'C:\\Users\\me\\voices "x"'
    for _ in range(3):
        write_env_updates({"KOKORO_VOICES_DIR": value})
    assert read_env_key("KOKORO_VOICES_DIR") == value


def test_removing_absent_key_reports_nothing(home):
    assert write_env_updates({"NOT_THERE": None}) == []


# --- settings screens --------------------------------------------------------


def test_check_value_uses_config_types():
    assert F.check_value("KOKORO_TTS_SPEED", "3")  # limit is 2.0
    assert F.check_value("KOKORO_TTS_SPEED", "1.1") is None
    assert F.check_value("DISCORD_GUILD_ID", "abc")
    assert F.check_value("OPHELIA_CIVITAI_MAX_COUNT", "2") is None


def test_display_marks_defaults(home):
    assert F.display(F.Opt("OPHELIA_BODY_CLOCK", "x")) == "on (default)"
    write_env_updates({"OPHELIA_BODY_CLOCK": "false"})
    assert F.display(F.Opt("OPHELIA_BODY_CLOCK", "x")) == "off"
    assert F.display(F.Opt("CIVITAI_API_KEY", "x", secret=True)) == "not set"


def test_edit_toggles_bools_and_rejects_bad_numbers(home, monkeypatch):
    F.edit(F.Opt("OPHELIA_BODY_CLOCK", "x"))
    assert read_env_key("OPHELIA_BODY_CLOCK") == "false"
    F.edit(F.Opt("OPHELIA_BODY_CLOCK", "x"))
    assert read_env_key("OPHELIA_BODY_CLOCK") == "true"

    answers = iter(["lots", "3"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    F.edit(F.Opt("OPHELIA_CIVITAI_MAX_COUNT", "x"))
    assert read_env_key("OPHELIA_CIVITAI_MAX_COUNT") == "3"


def test_edit_choice_can_reset_to_default(home, monkeypatch):
    opt = F.Opt("OPHELIA_CIVITAI_QUALITY", "Quality",
                choices=[("standard", "standard"), ("high", "high"), ("max", "max")])
    monkeypatch.setattr(F, "radiolist", lambda title, labels, **k: 1)
    F.edit(opt)
    assert read_env_key("OPHELIA_CIVITAI_QUALITY") == "high"
    monkeypatch.setattr(F, "radiolist", lambda title, labels, **k: len(labels) - 1)
    F.edit(opt)
    assert read_env_key("OPHELIA_CIVITAI_QUALITY") == ""


def test_validators():
    assert F.hours_range("1-8") is None and F.hours_range("25-3")
    assert F.day_list("mon,Tue wed") is None and F.day_list("funday")
    assert F.clock_list("06:30,7:05") is None and F.clock_list("6pm")
    assert F.id_list("123, 456") is None and F.id_list("@me")
    assert F.owner_ids("telegram:1,discord:2") is None and F.owner_ids("tg:1")


# --- setup sections ------------------------------------------------------------


def test_repair_resets_invalid_values(home, monkeypatch):
    write_env_updates({"KOKORO_TTS_SPEED": "3", "OPHELIA_PROVIDER": "deepseek"})
    monkeypatch.setattr(interactive, "confirm", lambda *a, **k: True)
    interactive._repair_broken_settings()
    assert read_env_key("KOKORO_TTS_SPEED") == ""
    assert read_env_key("OPHELIA_PROVIDER") == "deepseek"


def test_profile_keeps_image_and_ollama_settings(home, monkeypatch):
    write_env_updates({
        "OPHELIA_PROVIDER_IMAGE": "civitai",
        "OPHELIA_PROVIDER_VIDEO": "xai",
        "OLLAMA_BASE_URL": "http://192.168.1.50:11434/v1",
        "XAI_API_KEY": "old",
    })
    picks = iter([[k for k, _ in interactive._PROFILES].index("xai")])
    monkeypatch.setattr(interactive, "radiolist", lambda *a, **k: next(picks))
    monkeypatch.setattr(interactive, "prompt_text", lambda *a, **k: "old")
    monkeypatch.setattr(interactive, "_pick_model", lambda *a, **k: True)
    interactive._pick_profile(on_phone=True)
    assert read_env_key("OPHELIA_PROVIDER") == "xai"
    assert read_env_key("OPHELIA_PROVIDER_IMAGE") == "civitai"
    assert read_env_key("OPHELIA_PROVIDER_VIDEO") == "xai"

    picks = iter([[k for k, _ in interactive._PROFILES].index("ollama")])
    monkeypatch.setattr(interactive, "radiolist", lambda *a, **k: next(picks))
    interactive._pick_profile(on_phone=True)
    assert read_env_key("OLLAMA_BASE_URL") == "http://192.168.1.50:11434/v1"


def test_vision_role_offered_with_deepseek_primary(home, monkeypatch):
    write_env_updates({"OPHELIA_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "k"})
    assert "deepseek" in interactive._ROLE_PROVIDERS["vision"]
    assert interactive._ROLE_ENV["deepseek"]["vision"] == "DEEPSEEK_VISION_MODEL"
    assert "deepseek-flash" in interactive._presets_for("deepseek", "vision")
    assert "deepseek-v4-pro" not in interactive._presets_for("deepseek", "vision")
    assert interactive._summary_vision() == "deepseek / deepseek-flash"


def test_vision_to_compat_sets_the_model_it_needs(home, monkeypatch):
    choice = [k for k, _ in interactive._VISION_CHOICES].index("compat")
    monkeypatch.setattr(interactive, "radiolist", lambda title, items, **k: (
        choice if title == "Vision provider" else items.index("qwen/qwen3-vl-30b-a3b-instruct")))
    answers = iter(["https://openrouter.ai/api/v1", "sk-or-1"])
    monkeypatch.setattr(interactive, "prompt_text", lambda *a, **k: next(answers))
    interactive._pick_vision_provider(on_phone=True)
    assert read_env_key("OPHELIA_PROVIDER_VISION") == "compat"
    assert read_env_key("OPHELIA_COMPAT_VISION_MODEL") == "qwen/qwen3-vl-30b-a3b-instruct"
    assert read_env_key("OPHELIA_COMPAT_MODEL") == "qwen/qwen3-vl-30b-a3b-instruct"


def test_fallback_can_be_turned_off(home, monkeypatch):
    write_env_updates({"OPHELIA_FALLBACK_PROVIDERS": "deepseek", "OPHELIA_FALLBACK_MODEL": "x"})
    monkeypatch.setattr(interactive, "checkbox_or_none", lambda *a, **k: set())
    interactive._configure_fallback()
    assert read_env_key("OPHELIA_FALLBACK_PROVIDERS") == ""
    assert read_env_key("OPHELIA_FALLBACK_MODEL") == ""
    write_env_updates({"OPHELIA_FALLBACK_PROVIDERS": "deepseek"})
    monkeypatch.setattr(interactive, "checkbox_or_none", lambda *a, **k: None)
    interactive._configure_fallback()  # backed out: unchanged
    assert read_env_key("OPHELIA_FALLBACK_PROVIDERS") == "deepseek"


def test_no_retired_or_stale_presets():
    flat = [m for table in interactive._PRESETS.values() for ms in table.values() for m in ms]
    for gone in ("deepseek-chat", "deepseek-reasoner", "grok-2", "gpt-4-turbo", "llava", "bakllava"):
        assert gone not in flat
    persona_src = interactive._section_persona.__code__.co_consts
    assert any("Import from Hermes" in str(c) for c in persona_src)  # kept, just not first


# --- launcher --------------------------------------------------------------------


def test_launcher_rebuilds_items_after_start(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(launcher, "is_termux", lambda: True)
    state = {"tmux": False}
    monkeypatch.setattr(launcher, "_tmux_session_active", lambda: state["tmux"])

    def started():
        state["tmux"] = True
        hb.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")

    shown: list[list[str]] = []
    picks = iter([1, -1])  # row 1 = first item under the "Run" header

    def fake_menu(title, labels, **_):
        shown.append(list(labels))
        return next(picks)

    monkeypatch.setattr(launcher, "action_start", started)
    monkeypatch.setattr(launcher.tui, "menu", fake_menu)
    launcher._run_launcher()
    assert "Start Ophelia (wake-lock + tmux + run)" in shown[0]
    assert "Stop Ophelia" in shown[1] and "Start Ophelia (wake-lock + tmux + run)" not in shown[1]


def test_launcher_headers_are_not_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(launcher, "is_termux", lambda: False)
    labels, index = launcher._render_menu_labels(launcher._menu_items())
    assert isinstance(labels[0], tui.Header) and index[0] == -1
    assert labels[-1] == "Import from Hermes"
