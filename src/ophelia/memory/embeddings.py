"""Optional semantic recall over owner messages, via a local Ollama embedder.

Keyword recall (memory.recall) finds "maya" when you say "maya"; this finds
"my sister's new job" when you ask "how's the bakery thing going". It's an
add-on, never a requirement:

- ``OPHELIA_EMBEDDINGS=auto`` (default): on only when NOT on Termux and the
  embedding model is already pulled in Ollama. Nothing is ever auto-pulled,
  and phones keep today's behaviour (no extra model competing for RAM).
- ``on``: use it whenever Ollama answers (including Termux, if you want it).
- ``off``: keyword recall only.

Vectors live in SQLite next to the messages (``message_embeddings``), packed
as float32 with the stdlib ``array`` module. Search uses numpy when present
and falls back to pure Python, so there are no new dependencies.

Indexing runs in the background and only while no local model call is in
flight (model gate idle), so it never delays a reply. Only the owner
``messages`` table is indexed — quarantined guest chats never are.
"""

from __future__ import annotations

import asyncio
import math
import time
from array import array
from typing import TYPE_CHECKING

import aiosqlite
import httpx
import structlog

if TYPE_CHECKING:
    from ophelia.config import Settings

log = structlog.get_logger()

MIN_CHARS = 12
QUERY_TIMEOUT_S = 4.0
INDEX_BATCH = 16
INDEX_IDLE_SLEEP_S = 30.0
AVAILABILITY_TTL_S = 300.0


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def ollama_native_base(openai_base: str) -> str:
    """``http://host:11434/v1`` → ``http://host:11434`` (native API root)."""
    base = (openai_base or "http://127.0.0.1:11434/v1").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


class SemanticIndex:
    def __init__(self, settings: "Settings", db_path) -> None:
        self.settings = settings
        self.db_path = db_path
        self.mode = (getattr(settings, "embeddings_mode", "auto") or "auto").lower()
        self.model = getattr(settings, "embed_model", "") or "nomic-embed-text"
        self.base = ollama_native_base(getattr(settings, "ollama_base_url", ""))
        self._available: bool | None = None
        self._checked_at = 0.0
        # id -> normalized vector, loaded lazily from SQLite.
        self._cache: dict[int, list[float]] | None = None
        self._stop = False

    # -- availability -----------------------------------------------------

    async def available(self) -> bool:
        if self.mode == "off":
            return False
        now = time.time()
        if self._available is not None and now - self._checked_at < AVAILABILITY_TTL_S:
            return self._available
        self._checked_at = now
        if self.mode == "auto":
            from ophelia.platform import is_termux

            if is_termux():
                self._available = False
                return False
        self._available = await self._model_present()
        return self._available

    async def _model_present(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{self.base}/api/tags")
                r.raise_for_status()
                names = {m.get("name", "") for m in r.json().get("models", [])}
        except Exception:
            return False
        want = self.model if ":" in self.model else f"{self.model}:latest"
        return self.model in names or want in names

    # -- embedding ----------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        from ophelia.providers.model_gate import get_model_gate

        try:
            async with get_model_gate().session("embed", self.model, "ollama"):
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.post(
                        f"{self.base}/api/embed",
                        json={"model": self.model, "input": texts},
                    )
                    r.raise_for_status()
                    vecs = r.json().get("embeddings") or []
        except Exception as e:
            log.debug("embeddings.embed_failed", error=str(e))
            self._available = None  # re-check soon
            return None
        if len(vecs) != len(texts):
            return None
        return [_normalize([float(x) for x in v]) for v in vecs]

    # -- storage ------------------------------------------------------------

    async def _ensure_table(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_embeddings (
                message_id INTEGER PRIMARY KEY,
                model TEXT NOT NULL,
                vec BLOB NOT NULL
            )
            """
        )

    async def _load_cache(self) -> dict[int, list[float]]:
        if self._cache is None:
            cache: dict[int, list[float]] = {}
            async with aiosqlite.connect(self.db_path) as db:
                await self._ensure_table(db)
                cursor = await db.execute(
                    "SELECT message_id, vec FROM message_embeddings WHERE model = ?",
                    (self.model,),
                )
                for mid, blob in await cursor.fetchall():
                    cache[int(mid)] = _unpack(blob)
            self._cache = cache
        return self._cache

    async def index_pending(self, limit: int = INDEX_BATCH) -> int:
        """Embed up to ``limit`` not-yet-indexed owner messages. Returns count."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                """
                SELECT m.id, m.content FROM messages m
                LEFT JOIN message_embeddings e
                  ON e.message_id = m.id AND e.model = ?
                WHERE e.message_id IS NULL
                  AND m.role IN ('user', 'assistant')
                  AND length(m.content) >= ?
                  AND COALESCE(m.metadata, '') NOT LIKE '%consciousness_tick%'
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (self.model, MIN_CHARS, limit),
            )
            rows = await cursor.fetchall()
        if not rows:
            return 0
        vecs = await self.embed([str(c)[:2000] for _, c in rows])
        if not vecs:
            return 0
        cache = await self._load_cache()
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_table(db)
            await db.executemany(
                "INSERT OR REPLACE INTO message_embeddings (message_id, model, vec) "
                "VALUES (?, ?, ?)",
                [(int(mid), self.model, _pack(v)) for (mid, _), v in zip(rows, vecs)],
            )
            await db.commit()
        for (mid, _), v in zip(rows, vecs):
            cache[int(mid)] = v
        return len(rows)

    # -- search -------------------------------------------------------------

    async def search(self, query: str, limit: int = 5) -> list[tuple[int, float]]:
        """(message_id, cosine) best-first. Empty when unavailable/slow."""
        if len(query.strip()) < 4 or not await self.available():
            return []
        cache = await self._load_cache()
        if not cache:
            return []
        try:
            qv = await asyncio.wait_for(self.embed([query[:2000]]), QUERY_TIMEOUT_S)
        except asyncio.TimeoutError:
            return []
        if not qv:
            return []
        return _top_k(qv[0], cache, limit)

    # -- background loop ----------------------------------------------------

    async def run(self) -> None:
        """Index new messages while the local model is idle."""
        from ophelia.providers.model_gate import get_model_gate

        gate = get_model_gate()
        if self.mode == "auto":
            from ophelia.platform import is_termux

            if is_termux():
                return  # auto = off on phones; don't even poll
        while not self._stop:
            try:
                if await self.available() and not gate.is_local_busy():
                    n = await self.index_pending()
                    if n:
                        log.debug("embeddings.indexed", count=n)
                        await asyncio.sleep(1.0)
                        continue
            except Exception as e:
                log.debug("embeddings.index_loop_error", error=str(e))
            await asyncio.sleep(INDEX_IDLE_SLEEP_S)

    def stop(self) -> None:
        self._stop = True


def _top_k(
    q: list[float], cache: dict[int, list[float]], limit: int
) -> list[tuple[int, float]]:
    ids = list(cache.keys())
    try:
        import numpy as np

        mat = np.asarray([cache[i] for i in ids], dtype=np.float32)
        scores = mat @ np.asarray(q, dtype=np.float32)
        order = np.argsort(-scores)[:limit]
        return [(ids[int(i)], float(scores[int(i)])) for i in order]
    except Exception:
        scored = [(i, sum(a * b for a, b in zip(q, cache[i]))) for i in ids]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]


def fuse(
    keyword_ids: list[int], semantic: list[tuple[int, float]], *, min_cosine: float = 0.45
) -> list[int]:
    """Reciprocal-rank fusion of keyword and semantic hits (ids, best first)."""
    scores: dict[int, float] = {}
    for rank, mid in enumerate(keyword_ids):
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
    for rank, (mid, cos) in enumerate(semantic):
        if cos < min_cosine:
            continue
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
    return [mid for mid, _ in sorted(scores.items(), key=lambda t: t[1], reverse=True)]

