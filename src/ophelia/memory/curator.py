"""Curate durable memories from chats + Hermes history search."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import structlog

from ophelia.config import OPHELIA_HOME, Settings
from ophelia.memory.bootstrap import load_hermes_memories, parse_hermes_memory_file
from ophelia.memory.hermes_sessions import search_hermes_sessions
from ophelia.memory.store import MemoryStore
from ophelia.providers.errors import api_error_detail
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import ProviderStack, XAIBackend, build_provider_stack

log = structlog.get_logger()

CURATOR_PROMPT = """Extract 0-3 durable facts worth remembering months from now.
Skip ephemeral chit-chat. Output one fact per line, no bullets, no JSON.
If nothing worth keeping, output exactly: NONE"""


class MemoryCurator:
    def __init__(self, settings: Settings, memory: MemoryStore) -> None:
        self.settings = settings
        self.memory = memory
        self.mem_file = OPHELIA_HOME / "memories" / "MEMORY.md"
        self._hermes_db = self._find_state_db()

    def _find_state_db(self) -> Path | None:
        for p in (
            OPHELIA_HOME / "data" / "hermes_state.db",
            self.settings.hermes_home / "state.db",
        ):
            if p.is_file():
                return p
        return None

    def _existing_entries(self) -> set[str]:
        if not self.mem_file.is_file():
            return set()
        return set(parse_hermes_memory_file(self.mem_file.read_text(encoding="utf-8")))

    def _append_memory(self, fact: str) -> bool:
        fact = fact.strip()
        if not fact or len(fact) < 8:
            return False
        existing = self._existing_entries()
        if fact in existing or any(fact in e or e in fact for e in existing if len(e) > 20):
            return False
        self.mem_file.parent.mkdir(parents=True, exist_ok=True)
        sep = "\n§\n" if self.mem_file.is_file() and self.mem_file.stat().st_size > 10 else ""
        with self.mem_file.open("a", encoding="utf-8") as f:
            f.write(f"{sep}{fact}")
        log.info("curator.appended", fact=fact[:80])
        return True

    async def ingest_pending_notes(self) -> int:
        """Promote consciousness memory_note facts from DB."""
        count = 0
        # Scan recent facts keys memory:*
        async with aiosqlite.connect(self.memory.db_path) as db:
            cursor = await db.execute(
                "SELECT key, value FROM facts WHERE key LIKE 'memory:%' ORDER BY updated_at DESC LIMIT 20"
            )
            rows = await cursor.fetchall()
        for key, value in rows:
            if self._append_memory(value):
                count += 1
                await self.memory.set_fact(f"curated:{key}", value)
        return count

    async def curate_from_recent_chats(self, limit: int = 30) -> int:
        messages = await self.memory.recent_global(limit=limit)
        if len(messages) < 4:
            return 0

        transcript = []
        for m in messages:
            if m["role"] not in ("user", "assistant"):
                continue
            c = m["content"][:400]
            if c.startswith("[inner]") or c.startswith("[saw]"):
                continue
            transcript.append(f"{m['role']}: {c}")
        blob = "\n".join(transcript[-25:])

        hermes_ctx = ""
        if self._hermes_db:
            hits = search_hermes_sessions(self._hermes_db, "user preferences important", limit=5)
            if hits:
                hermes_ctx = "\nOld sessions:\n" + "\n".join(h.content[:200] for h in hits)

        stack = build_provider_stack(self.settings)
        backend = stack.backend("curator")
        model = stack.model("curator")

        if isinstance(backend, XAIBackend):
            client = await backend.async_client_fresh()
        else:
            client = backend.async_client()

        try:
            gate = get_model_gate()
            async with gate.session("curator", model, stack.name("curator")):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": CURATOR_PROMPT},
                        {
                            "role": "user",
                            "content": f"Recent conversation:\n{blob}\n{hermes_ctx}",
                        },
                    ],
                    max_tokens=300,
                )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            log.warning("curator.llm_failed", error=api_error_detail(e), model=model)
            return 0

        if raw.upper() == "NONE" or not raw:
            return 0

        count = 0
        for line in raw.splitlines():
            line = line.lstrip("-•0123456789. ").strip()
            if line and self._append_memory(line):
                count += 1
        await self.memory.set_fact("curator:last_run", str(time.time()))
        return count

    async def run_cycle(self) -> int:
        n = await self.ingest_pending_notes()
        n += await self.curate_from_recent_chats()
        return n

    def reload_agent_memories(self, agent) -> None:
        """Refresh in-memory MEMORY.md entries on agent after curation."""
        entries, user = load_hermes_memories(OPHELIA_HOME)
        agent._memory_entries = entries
        agent._user_entries = user
