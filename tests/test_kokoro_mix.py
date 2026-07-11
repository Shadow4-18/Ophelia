"""Tests for Kokoro voice-mix baking (L2 style-vector renormalization)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ophelia.media.kokoro_mix import (
    blend_style_vectors,
    is_mix_expression,
    mix_cache_name,
    parse_mix_expression,
    resolve_kokoro_voice,
    write_torch_voice_pt,
)


def test_is_mix_expression():
    assert is_mix_expression("af_bella(0.6)+bf_emma(0.4)")
    assert is_mix_expression("af_heart+af_bella")
    assert is_mix_expression("am_adam-am_michael")
    assert not is_mix_expression("af_heart")
    assert not is_mix_expression("af_bella")
    assert not is_mix_expression("")


def test_parse_mix_expression_weights():
    tokens = parse_mix_expression("af_bella(0.7)+bf_emma(0.3)")
    assert tokens == [
        ("af_bella", 0.7, "+"),
        ("bf_emma", 0.3, "+"),
    ]


def test_parse_mix_expression_subtract():
    tokens = parse_mix_expression("am_adam-am_michael")
    assert tokens[0] == ("am_adam", 1.0, "+")
    assert tokens[1] == ("am_michael", 1.0, "-")


def test_parse_mix_rejects_garbage():
    with pytest.raises(ValueError):
        parse_mix_expression("+af_heart")
    with pytest.raises(ValueError):
        parse_mix_expression("af_heart")


def test_mix_cache_name_stable():
    a = mix_cache_name("af_bella(0.7)+bf_emma(0.3)")
    b = mix_cache_name("af_bella(0.7)+bf_emma(0.3)")
    assert a == b
    assert a.startswith("ophelia_mix_af_bella_bf_emma_")
    assert "+" not in a and "(" not in a


def _fake_pack(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((510, 1, 256), dtype=np.float32)


def test_blend_restores_l2_norm():
    a = _fake_pack(1)
    b = _fake_pack(2)
    naive = 0.6 * a + 0.4 * b
    blended = blend_style_vectors([(a, 0.6, "+"), (b, 0.4, "+")])

    def row_norms(x: np.ndarray) -> np.ndarray:
        return np.linalg.norm(x.reshape(x.shape[0], -1).astype(np.float64), axis=1)

    na, nb = row_norms(a), row_norms(b)
    expected = 0.6 * na + 0.4 * nb
    actual = row_norms(blended)
    naive_n = row_norms(naive)

    # Naive mix shrinks; L2-renorm matches expected magnitude.
    assert float(np.mean(naive_n / expected)) < 0.99
    assert float(np.mean(actual / expected)) == pytest.approx(1.0, abs=1e-5)


def test_write_torch_voice_pt_roundtrip_structure(tmp_path: Path):
    import zipfile

    pack = _fake_pack(3)
    out = tmp_path / "test_voice.pt"
    write_torch_voice_pt(pack, out, archive_name="test_voice")
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert any(n.endswith("data.pkl") for n in names)
        assert any(n.endswith("data/0") for n in names)
        data = zf.read([n for n in names if n.endswith("data/0")][0])
        assert len(data) == 510 * 1 * 256 * 4
        restored = np.frombuffer(data, dtype=np.float32).reshape(510, 1, 256)
        np.testing.assert_array_equal(restored, pack)


def test_resolve_installs_into_voices_dir(tmp_path: Path, monkeypatch):
    from ophelia.config import Settings
    from ophelia.media import kokoro_mix as km

    voices_bin = tmp_path / "voices.npz"
    np.savez(voices_bin, af_bella=_fake_pack(4), bf_emma=_fake_pack(5))
    # np.savez may leave path as-is when suffix is .npz
    if not voices_bin.is_file():
        voices_bin = tmp_path / "voices.npz"

    home = tmp_path / "home"
    home.mkdir()
    install_dir = tmp_path / "fastapi_voices"

    monkeypatch.setattr(km, "_ensure_voices_bin", lambda cache_dir: voices_bin)
    monkeypatch.setattr(km, "_voices_cache_dir", lambda settings=None: tmp_path / "cache")

    import ophelia.config as cfg

    monkeypatch.setattr(cfg, "OPHELIA_HOME", home)

    settings = Settings(
        KOKORO_TTS_URL="http://127.0.0.1:8880/v1",
        KOKORO_VOICES_DIR=str(install_dir),
    )
    expr = "af_bella(0.7)+bf_emma(0.3)"
    name = resolve_kokoro_voice(expr, settings=settings)
    assert name.startswith("ophelia_mix_af_bella_bf_emma_")
    assert (install_dir / f"{name}.pt").is_file()
    assert (home / "voices" / f"{name}.pt").is_file()


def test_resolve_without_voices_dir_uses_dominant(tmp_path: Path, monkeypatch):
    from ophelia.config import Settings
    from ophelia.media import kokoro_mix as km

    voices_bin = tmp_path / "voices.npz"
    np.savez(voices_bin, af_bella=_fake_pack(4), bf_emma=_fake_pack(5))
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(km, "_ensure_voices_bin", lambda cache_dir: voices_bin)
    monkeypatch.setattr(km, "_voices_cache_dir", lambda settings=None: tmp_path / "cache")
    import ophelia.config as cfg

    monkeypatch.setattr(cfg, "OPHELIA_HOME", home)

    settings = Settings(KOKORO_TTS_URL="http://127.0.0.1:8880/v1")
    # No KOKORO_VOICES_DIR → must NOT pass raw mix through
    assert resolve_kokoro_voice("af_bella(0.7)+bf_emma(0.3)", settings=settings) == "af_bella"


def test_dominant_voice():
    from ophelia.media.kokoro_mix import dominant_voice

    assert dominant_voice("af_bella(0.7)+bf_emma(0.3)") == "af_bella"
    assert dominant_voice("af_heart(0.2)+af_bella(0.8)") == "af_bella"


def test_default_kokoro_voice_is_single_preset():
    from ophelia.config import Settings

    assert Settings.model_fields["kokoro_tts_voice"].default == "af_heart"
    assert not is_mix_expression(Settings.model_fields["kokoro_tts_voice"].default)
