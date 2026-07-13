"""Avatar bridge — psyche → Live2D / VRoid / procedural expression params."""

from __future__ import annotations

from pathlib import Path

from ophelia.mind.avatar import (
    AvatarBridge,
    expression_from_mood,
    find_model3,
    find_vrm,
    mouth_envelope,
    params_from_psyche,
    resolve_model,
    vrm_weights_from_expression,
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


def test_vrm_weights_happy_and_mouth():
    weights = vrm_weights_from_expression(
        "happy",
        mouth_open=0.7,
        eye_open=1.0,
        params={"ParamMouthForm": 0.5, "ParamEyeBallX": 0.2},
        speaking=True,
    )
    assert weights["happy"] > 0.5
    assert weights["aa"] > 0.4
    assert weights["lookRight"] > 0
    assert weights["blink"] == 0.0


def test_vrm_weights_blink_when_eyes_closed():
    weights = vrm_weights_from_expression("sleepy", mouth_open=0.0, eye_open=0.2)
    assert weights["blink"] >= 0.7
    assert weights["relaxed"] > 0


def test_avatar_bridge_snapshot_and_speak(tmp_path: Path):
    bridge = AvatarBridge(enabled=True, avatar_dir=tmp_path, backend="procedural")
    idle = bridge.snapshot(label="neutral", valence=0.1, arousal=0.3)
    assert idle.backend == "procedural"
    assert idle.expression in ("neutral", "happy", "curious", "sleepy")
    assert idle.model_url is None
    assert "happy" in idle.vrm or "neutral" in idle.vrm

    bridge.begin_speak("Hello from Ophelia")
    assert bridge.is_speaking
    talking = bridge.snapshot(label="happy", valence=0.5, arousal=0.5)
    assert talking.speaking is True
    assert "ParamMouthOpenY" in talking.params
    assert talking.vrm["aa"] >= 0

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


def test_find_vrm(tmp_path: Path):
    assert find_vrm(tmp_path) is None
    named = tmp_path / "ophelia.vrm"
    named.write_bytes(b"vrm")
    assert find_vrm(tmp_path) == named
    direct = tmp_path / "model.vrm"
    direct.write_bytes(b"vrm")
    assert find_vrm(tmp_path) == direct
    nested = tmp_path / "chars" / "hero.vrm"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"vrm")
    assert find_vrm(tmp_path, "chars/hero.vrm") == nested


def test_resolve_model_prefers_vrm_on_auto(tmp_path: Path):
    (tmp_path / "model.model3.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.vrm").write_bytes(b"vrm")
    kind, path = resolve_model(tmp_path, prefer="auto")
    assert kind == "vrm"
    assert path.name == "model.vrm"
    kind2, path2 = resolve_model(tmp_path, prefer="live2d")
    assert kind2 == "model3"
    assert path2.name == "model.model3.json"


def test_avatar_bridge_live2d_backend_when_model_present(tmp_path: Path):
    (tmp_path / "model.model3.json").write_text("{}", encoding="utf-8")
    bridge = AvatarBridge(enabled=True, avatar_dir=tmp_path, backend="auto")
    state = bridge.snapshot(label="neutral", valence=0.0, arousal=0.3)
    assert state.backend == "live2d"
    assert state.model_url == "/avatar/model.model3.json"
    assert state.model_ready is True
    assert state.model_kind == "model3"


def test_avatar_bridge_vroid_backend_when_vrm_present(tmp_path: Path):
    (tmp_path / "model.vrm").write_bytes(b"vrm")
    bridge = AvatarBridge(enabled=True, avatar_dir=tmp_path, backend="auto")
    state = bridge.snapshot(label="happy", valence=0.6, arousal=0.5)
    assert state.backend == "vroid"
    assert state.model_url == "/avatar/model.vrm"
    assert state.model_ready is True
    assert state.model_kind == "vrm"
    assert state.vrm["happy"] > 0


def test_avatar_bridge_configured_vrm_wins(tmp_path: Path):
    (tmp_path / "model.model3.json").write_text("{}", encoding="utf-8")
    custom = tmp_path / "mine.vrm"
    custom.write_bytes(b"vrm")
    bridge = AvatarBridge(
        enabled=True,
        avatar_dir=tmp_path,
        model_path="mine.vrm",
        backend="vroid",
    )
    state = bridge.snapshot(label="neutral", valence=0.0, arousal=0.3)
    assert state.backend == "vroid"
    assert state.model_url == "/avatar/mine.vrm"
