"""DeepSeek Flash can see, and DeepSeek's Search/Fetch API backs web search.

Ophelia was written when DeepSeek's API was text-only with no search, so the
router hard-refused DeepSeek for vision. deepseek-flash (V4.1) now takes
images; deepseek-v4-pro still doesn't. Search/Fetch reuse DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import pytest

from ophelia.config import Settings
from ophelia.providers import router
from ophelia.tools import web_search


def _settings(monkeypatch, tmp_path, **env) -> Settings:
    monkeypatch.setenv("OPHELIA_HOME", str(tmp_path))
    for k in ("XAI_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY",
              "BRAVE_API_KEY", "OLLAMA_VISION_MODEL", "OPHELIA_PROVIDER_VISION",
              "DEEPSEEK_VISION_MODEL", "OPHELIA_WEB_SEARCH_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_flash_sees_pro_does_not(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k")
    assert s.deepseek_vision_resolved() == "deepseek-flash"
    assert router._provider_configured_for_role(s, "deepseek", "vision")
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k", DEEPSEEK_VISION_MODEL="deepseek-v4-pro")
    assert s.deepseek_vision_resolved() is None
    assert not router._provider_configured_for_role(s, "deepseek", "vision")


def test_vision_never_inherits_pro_chat_model(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k", DEEPSEEK_MODEL="deepseek-v4-pro")
    assert router._provider_default_model_for_role(s, "deepseek", "vision") == "deepseek-flash"


def test_auto_vision_prefers_deepseek(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k", OLLAMA_VISION_MODEL="minicpm")
    monkeypatch.setattr(router, "_ollama_reachable", lambda *a, **k: True)
    monkeypatch.setattr(router, "_xai_oauth_available", lambda *a, **k: False)
    assert router._auto_pick_provider(s, "vision") == "deepseek"


def test_explicit_ollama_vision_still_wins(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k", OLLAMA_VISION_MODEL="minicpm",
                  OPHELIA_PROVIDER_VISION="ollama")
    monkeypatch.setattr(router, "_ollama_reachable", lambda *a, **k: True)
    stack = router.build_provider_stack(s)
    assert stack.name("vision") == "ollama"
    assert stack.model("vision") == "minicpm"


def test_search_auto_uses_deepseek_key(monkeypatch, tmp_path):
    assert _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k").web_search_provider_resolved() == "deepseek"
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k", TAVILY_API_KEY="t")
    assert s.web_search_provider_resolved() == "tavily"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert _settings(monkeypatch, tmp_path).web_search_provider_resolved() == "duckduckgo"


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


@pytest.mark.asyncio
async def test_deepseek_search_and_fetch(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k")
    calls = []

    async def fake_post(self, url, headers=None, json=None, **_):
        calls.append((url, json, headers))
        if url.endswith("/search"):
            return _Resp(200, {"results": [{"title": "Balatro", "url": "https://x.y",
                                            "summary": "A poker roguelike.", "date": "2024-02-20T00:00:00Z"}]})
        return _Resp(200, {"title": "Page", "content": "# Hello\nworld"})

    monkeypatch.setattr(web_search.httpx.AsyncClient, "post", fake_post)
    out = await web_search.search_web("what is balatro", settings=s)
    assert "Web search (DeepSeek)" in out and "poker roguelike" in out and "2024-02-20" in out
    assert calls[0][0] == "https://api.deepseek.com/v1/tools/search"
    assert calls[0][2]["Authorization"] == "Bearer k"
    page = await web_search.fetch_url("https://x.y/a", settings=s)
    assert "# Hello" in page and calls[1][0].endswith("/v1/tools/fetch")


@pytest.mark.asyncio
async def test_deepseek_search_failure_falls_back(monkeypatch, tmp_path):
    s = _settings(monkeypatch, tmp_path, DEEPSEEK_API_KEY="k")

    async def fake_post(self, url, **_):
        return _Resp(402, {"error": {"message": "Insufficient balance"}})

    async def fake_ddg(query, n):
        return "ddg results"

    monkeypatch.setattr(web_search.httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(web_search, "_duckduckgo_search", fake_ddg)
    assert await web_search.search_web("x", settings=s) == "ddg results"
