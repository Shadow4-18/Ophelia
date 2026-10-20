"""Memory recall: natural-language queries, tick exclusion, optional semantic
index (mocked embedder), and Termux-safe defaults."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ophelia.memory.recall import fts_query, keywords
from ophelia.memory.store import MemoryStore


def test_keywords_drop_filler_and_punctuation():
    assert keywords("how's maya doing at the bakery?") == ["maya", "bakery"]
    assert keywords("hey! what's up?") == []
    assert keywords("Maya's maya MAYA") == ["maya"]


def test_fts_query_is_quoted_or_query():
    q = fts_query(["maya", "bakeries", "ok'd", "a\"b"])
    assert q == '"maya"* OR "baker"* OR "ok\'d"* OR "ab"'


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    await s.init()
    await s.append_message(
        "telegram:1", "user", "My sister Maya just started at the bakery downtown"
    )
    await s.append_message("telegram:1", "assistant", "Tell her congrats from me!")
    await s.append_message(
        "consciousness",
        "user",
        "inner check-in: maya bakery pressure 0.4",
        metadata={"type": "consciousness_tick"},
    )
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "how's maya doing at the bakery?",
        "what's up with maya",
        "how is maya doing these days",
        "any news from the bakeries?",  # prefix match
    ],
)
async def test_natural_questions_recall_the_message(store, query):
    hits = await store.search_messages(query, limit=3)
    assert hits and "Maya" in hits[0]["content"]


@pytest.mark.asyncio
async def test_consciousness_ticks_are_not_memories(store):
    hits = await store.search_messages("maya bakery pressure", limit=10)
    assert all("inner check-in" not in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_filler_only_query_returns_nothing(store):
    assert await store.search_messages("hey, how are you?") == []


@pytest.mark.asyncio
async def test_lessons_and_guest_search_match_any_term(store):
    await store.add_lesson("Owner's sister Maya loves sourdough", context="family")
    lessons = await store.search_lessons("did maya like the bread?")
    assert lessons and "Maya" in lessons[0]["lesson"]

    await store.append_guest_message("discord:9", "user", "I adopted a greyhound named Rocket")
    hits = await store.search_guest_messages("how's rocket the dog?", channel="discord:9")
    assert hits and "Rocket" in hits[0]["content"]


@pytest.mark.asyncio
async def test_recall_without_semantic_is_keyword_only(store):
    hits = await store.recall("how's maya doing?", limit=2)
    assert hits[0]["content"].startswith("My sister Maya")


class _FakeSemantic:
    def __init__(self, results):
        self.results = results

    async def search(self, query, limit=5):
        return self.results


@pytest.mark.asyncio
async def test_recall_fuses_semantic_only_hits(store):
    # The "congrats" reply shares no keywords with the query, but the
    # embedder says it's related → it should surface.
    store.semantic = _FakeSemantic([(2, 0.82)])
    hits = await store.recall("did you send your well wishes?", limit=3)
    assert any("congrats" in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_recall_ignores_low_similarity(store):
    store.semantic = _FakeSemantic([(2, 0.1)])
    assert await store.recall("did you send your well wishes?", limit=3) == []


# --- SemanticIndex ----------------------------------------------------------


def _settings(mode="auto"):
    return SimpleNamespace(
        embeddings_mode=mode,
        embed_model="nomic-embed-text",
        ollama_base_url="http://127.0.0.1:11434/v1",
    )


@pytest.mark.asyncio
async def test_auto_mode_is_off_on_termux(store, monkeypatch):
    from ophelia.memory.embeddings import SemanticIndex

    monkeypatch.setattr("ophelia.platform.is_termux", lambda: True)
    idx = SemanticIndex(_settings("auto"), store.db_path)

    async def boom():
        raise AssertionError("must not even probe Ollama on Termux in auto mode")

    monkeypatch.setattr(idx, "_model_present", boom)
    assert await idx.available() is False
    assert await idx.search("how's maya") == []


@pytest.mark.asyncio
async def test_off_mode_never_available(store):
    from ophelia.memory.embeddings import SemanticIndex

    assert await SemanticIndex(_settings("off"), store.db_path).available() is False


@pytest.mark.asyncio
async def test_index_and_search_with_fake_embedder(store, monkeypatch):
    from ophelia.memory import embeddings
    from ophelia.memory.embeddings import SemanticIndex

    idx = SemanticIndex(_settings("on"), store.db_path)

    async def present():
        return True

    vocab = ["maya", "bakery", "congrats", "sister"]

    async def fake_embed(texts):
        out = []
        for t in texts:
            low = t.lower()
            v = [1.0 if w in low else 0.0 for w in vocab] + [0.1]
            out.append(embeddings._normalize(v))
        return out

    monkeypatch.setattr(idx, "_model_present", present)
    monkeypatch.setattr(idx, "embed", fake_embed)

    n = await idx.index_pending()
    assert n == 2  # user + assistant; the tick is skipped
    assert await idx.index_pending() == 0  # idempotent

    res = await idx.search("my sister", limit=2)
    assert res and res[0][0] == 1


def test_top_k_pure_python_fallback(monkeypatch):
    import builtins

    from ophelia.memory.embeddings import _top_k

    real_import = builtins.__import__

    def no_numpy(name, *a, **kw):
        if name == "numpy":
            raise ImportError
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    cache = {1: [1.0, 0.0], 2: [0.0, 1.0], 3: [0.7071, 0.7071]}
    assert [i for i, _ in _top_k([1.0, 0.0], cache, 2)] == [1, 3]


@pytest.mark.asyncio
async def test_index_loop_exits_immediately_on_termux(store, monkeypatch):
    import asyncio

    from ophelia.memory.embeddings import SemanticIndex

    monkeypatch.setattr("ophelia.platform.is_termux", lambda: True)
    idx = SemanticIndex(_settings("auto"), store.db_path)
    await asyncio.wait_for(idx.run(), 1.0)  # returns instead of polling forever
