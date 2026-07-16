"""Workstation model listing / selection API."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_model_setting_for_ollama_roles():
    from ophelia.ui.workstation import Workstation

    assert Workstation._model_setting_for("ollama", "chat") == (
        "OLLAMA_MODEL",
        "ollama_model",
    )
    assert Workstation._model_setting_for("ollama", "consciousness") == (
        "OLLAMA_CONSCIOUSNESS_MODEL",
        "ollama_consciousness_model",
    )
    assert Workstation._model_setting_for("ollama", "vision") == (
        "OLLAMA_VISION_MODEL",
        "ollama_vision_model",
    )


def test_model_setting_for_xai_and_openai():
    from ophelia.ui.workstation import Workstation

    assert Workstation._model_setting_for("xai", "chat")[0] == "XAI_MODEL"
    assert Workstation._model_setting_for("xai-oauth", "consciousness")[0] == (
        "XAI_CONSCIOUSNESS_MODEL"
    )
    assert Workstation._model_setting_for("openai", "chat") == (
        "OPENAI_MODEL",
        "openai_model",
    )


@pytest.mark.asyncio
async def test_select_model_updates_settings_and_env(isolated_env, monkeypatch):
    from ophelia.setup import env_io
    from ophelia.ui.workstation import Workstation

    ws = Workstation.__new__(Workstation)
    ws.settings = SimpleNamespace(
        ollama_model="llama3.2:1b",
        ollama_consciousness_model=None,
        ollama_vision_model=None,
        ollama_curator_model=None,
    )
    ws.stack = SimpleNamespace(
        name=lambda role: "ollama",
        model=lambda role: getattr(ws.settings, "ollama_model"),
        _backends={"ollama:chat": object()},
    )
    ws.bus = SimpleNamespace(broadcast=AsyncMock())
    ws.status_dict = MagicMock(
        return_value={"ready": True, "chat_model": "phi3:mini", "chat_provider": "ollama"}
    )
    ws.models_info = AsyncMock(
        return_value={
            "installed": ["phi3:mini"],
            "routing": {"chat": "phi3:mini"},
            "chat_model": "phi3:mini",
            "chat_provider": "ollama",
        }
    )

    monkeypatch.setattr(env_io, "env_path", lambda: isolated_env / ".env")
    (isolated_env / ".env").write_text("OLLAMA_MODEL=llama3.2:1b\n", encoding="utf-8")

    out = await Workstation.select_model(ws, "chat", "phi3:mini", persist=True)

    assert ws.settings.ollama_model == "phi3:mini"
    assert ws.stack._backends == {}
    assert out["selected"]["model"] == "phi3:mini"
    assert out["selected"]["persisted"] is True
    assert "OLLAMA_MODEL=phi3:mini" in (isolated_env / ".env").read_text(encoding="utf-8")
    ws.bus.broadcast.assert_awaited()


@pytest.mark.asyncio
async def test_select_model_rejects_bad_role():
    from ophelia.ui.workstation import Workstation

    ws = Workstation.__new__(Workstation)
    ws.settings = SimpleNamespace()
    ws.stack = SimpleNamespace(name=lambda role: "ollama", _backends={})
    ws.bus = SimpleNamespace(broadcast=AsyncMock())

    with pytest.raises(ValueError, match="role"):
        await Workstation.select_model(ws, "image", "flux", persist=False)


@pytest.mark.asyncio
async def test_select_model_api_endpoint(isolated_env):
    from fastapi.testclient import TestClient

    from ophelia.ui.server import create_app

    ws = MagicMock()
    ws.settings.avatar_dir = isolated_env / "avatar"
    ws.settings.avatar_enabled = False
    ws.bus = MagicMock()
    ws.bus.connect = AsyncMock()
    ws.bus.disconnect = AsyncMock()
    ws.status_dict.return_value = {"ready": True}
    ws.history = AsyncMock(return_value=[])
    ws.inner_full_tail.return_value = ""
    ws.models_info = AsyncMock(
        return_value={"installed": ["llama3.2:1b"], "chat_model": "llama3.2:1b"}
    )
    ws.select_model = AsyncMock(
        return_value={
            "selected": {
                "role": "chat",
                "model": "phi3:mini",
                "provider": "ollama",
                "env_key": "OLLAMA_MODEL",
                "persisted": True,
            },
            "chat_model": "phi3:mini",
            "installed": ["phi3:mini"],
        }
    )
    ws.compare_models = AsyncMock(return_value={"results": []})

    app = create_app(ws)
    client = TestClient(app)

    r = client.get("/api/models")
    assert r.status_code == 200
    assert "llama3.2:1b" in r.json()["installed"]

    r = client.post(
        "/api/models/select",
        json={"role": "chat", "model": "phi3:mini", "persist": True},
    )
    assert r.status_code == 200
    assert r.json()["selected"]["model"] == "phi3:mini"
    ws.select_model.assert_awaited_once()
