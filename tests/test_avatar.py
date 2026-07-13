"""Avatar bridge — psyche → Live2D / procedural expression params."""

from __future__ import annotations

from pathlib import Path

from ophelia.mind.avatar import (
    AvatarBridge,
    expression_from_mood,
    find_model3,
    mouth_envelope,
    params_from_psyche,
)


def test_expression_happy_from_valence():
    assert expression_from_mood(label="content", valence=0.6, arousal=0.5) == "happy"


def test_expression_sad_from_valence():
    assert expression_from_mood(label="low", valence=-0.5, arousal=0.3) == "sad"


def test_expression_from_feeling_keywords():
    assert (
        expression_from_mood(
            label="neutral", valence=0.0, arousal=0.4, feelings=["shy blush"]
        )
        == "shy"
    )
    assert (
        expression_from_mood(
            label="neutral", valence=0.1, arousal=0.8, feelings=["surprised"]
        )
        == "surprised"
    )


def test_expression_sleepy_from_boredom():
    assert (
        expression_from_mood(label="idle", valence=0.0, arousal=0.2, boredom=0.8)
        == "sleepy"
    )


def test_params_include_live2d_ids():
    expr, params = params_from_psyche(
        label="happy",
        valence=0.7,
        arousal=0.6,
        speaking=True,
        mouth_open=0.5,
        t=10.0,
    )
    assert expr == "happy"
    assert "ParamMouthOpenY" in params
    assert "ParamMouthForm" in params
    assert "ParamEyeLOpen" in params
    assert params["ParamMouthOpenY"] == 0.5
    assert params["ParamMouthForm"] > 0


def test_mouth_envelope_peaks_mid_utterance():
    text = "hello there friend how are you doing today"
    mid = mouth_envelope(text, 0.6)
    start = mouth_envelope(text, 0.01)
    end = mouth_envelope(text, 50.0)
    assert mid > start
    assert end == 0.0


def test_avatar_bridge_snapshot_and_speak(tmp_path: Path):
    bridge = AvatarBridge(enabled=True, avatar_dir=tmp_path, backend="procedural")
    idle = bridge.snapshot(label="neutral", valence=0.1, arousal=0.3)
    assert idle.backend == "procedural"
    assert idle.expression in ("neutral", "happy", "curious", "sleepy")
    assert idle.model_url is None

    bridge.begin_speak("Hello from Ophelia")
    assert bridge.is_speaking
    talking = bridge.snapshot(label="happy", valence=0.5, arousal=0.5)
    assert talking.speaking is True
    assert "ParamMouthOpenY" in talking.params

    bridge.end_speak()
    assert bridge.is_speaking is False


def test_find_model3(tmp_path: Path):
    assert find_model3(tmp_path) is None
    model = tmp_path / "ophelia.model3.json"
    model.write_text("{}", encoding="utf-8")
    assert find_model3(tmp_path) == model

    nested = tmp_path / "rig" / "model.model3.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("{}", encoding="utf-8")
    direct = tmp_path / "model.model3.json"
    direct.write_text("{}", encoding="utf-8")
    assert find_model3(tmp_path) == direct
    assert find_model3(tmp_path, "rig/model.model3.json") == nested


def test_avatar_bridge_live2d_backend_when_model_present(tmp_path: Path):
    (tmp_path / "model.model3.json").write_text("{}", encoding="utf-8")
    bridge = AvatarBridge(enabled=True, avatar_dir=tmp_path, backend="auto")
    state = bridge.snapshot(label="neutral", valence=0.0, arousal=0.3)
    assert state.backend == "live2d"
    assert state.model_url == "/avatar/model.model3.json"
    assert state.model_ready is True
