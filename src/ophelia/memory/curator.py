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

RECONCILE_PROMPT = """You are Ophelia's memory curator. Reconcile stored facts against AUTHORITATIVE context.

Stored facts may be stale or wrong — old schedules, wrong timezone, outdated owner details, facts that
contradict what the authoritative context block says is true NOW. Your job is to flag contradictions
and produce corrected versions.

AUTHORITATIVE CONTEXT (trust this over stored facts):
{auth_block}

STORED FACTS:
{facts_block}

For each stored fact, decide:
- "keep"     — fact is correct and not contradicted by the authoritative context.
- "correct"  — fact is wrong/stale; output a corrected version.
- "remove"   — fact is obsolete or directly contradicted and not worth correcting.
- "skip"     — fact is subjective/opinion and can't be verified against context.

Output ONLY valid JSON, a list of objects:
[
  {{"action": "keep" | "correct" | "remove" | "skip", "original": "<original fact>", "corrected": "<only if action=correct>"}}
]

If all facts are fine, output: []"""


class MemoryCurator:
    def __init__(self, settings: Settings, memory: MemoryStore) -> None:
        self.settings = settings
        self.memory = memory
        self.mem_file = OPHELIA_HOME / "memories" / "MEMORY.md"
        self._hermes_db = self._find_state_db()
        # Tier C #13: optional LifeContext for the reconciliation pass. When
        # set, the curator can check stored facts against the authoritative
        # context block (time, schedule, owner state) and correct stale ones.
        self.life = None

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
            from ophelia.providers.fallback import call_with_fallback, extra_body_for

            primary_provider = stack.name("curator")

            async def _make_call(client, mdl, provider):
                return await client.chat.completions.create(
                    model=mdl,
                    messages=[
                        {"role": "system", "content": CURATOR_PROMPT},
                        {
                            "role": "user",
                            "content": f"Recent conversation:\n{blob}\n{hermes_ctx}",
                        },
                    ],
                    max_tokens=300,
                    extra_body=extra_body_for(self.settings, provider),
                )

            resp = await call_with_fallback(
                self.settings,
                stack,
                role="curator",
                primary_provider=primary_provider,
                primary_model=model,
                primary_client=client,
                make_call=_make_call,
                gate=gate,
                log_tag="curator.fallback",
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

    async def reconcile_against_context(self) -> int:
        """Tier C #13: reconcile stored MEMORY.md facts against the authoritative
        LifeContext block. Returns the number of facts corrected/removed.

        Stale facts (wrong timezone, old work schedule, outdated owner details)
        silently leak into prompts even after LifeContext fixes the *current*
        context. This pass catches them. Throttle to once per day via the
        `curator:last_reconcile` fact.
        """
        if self.life is None:
            return 0
        # Throttle: at most once per 24h.
        last = await self.memory.get_fact("curator:last_reconcile")
        if last:
            try:
                if time.time() - float(last) < 86400:
                    return 0
            except (TypeError, ValueError):
                pass

        entries = self._existing_entries()
        if not entries:
            return 0

        auth_block = self.life.to_context_block()
        facts_block = "\n".join(f"- {e}" for e in sorted(entries))
        prompt = RECONCILE_PROMPT.format(auth_block=auth_block, facts_block=facts_block)

        stack = build_provider_stack(self.settings)
        backend = stack.backend("curator")
        model = stack.model("curator")
        if isinstance(backend, XAIBackend):
            client = await backend.async_client_fresh()
        else:
            client = backend.async_client()

        try:
            gate = get_model_gate()
            from ophelia.providers.fallback import call_with_fallback, extra_body_for

            async def _make_call(cl, mdl, provider):
                return await cl.chat.completions.create(
                    model=mdl,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1200,
                    extra_body=extra_body_for(self.settings, provider),
                )

            resp = await call_with_fallback(
                self.settings,
                stack,
                role="curator",
                primary_provider=stack.name("curator"),
                primary_model=model,
                primary_client=client,
                make_call=_make_call,
                gate=gate,
                log_tag="curator.reconcile_fallback",
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            log.warning("curator.reconcile_llm_failed", error=api_error_detail(e))
            return 0

        new_entries, changed = self._apply_reconcile_actions(raw, entries)

        if changed:
            self._rewrite_memory_file(new_entries)
        await self.memory.set_fact("curator:last_reconcile", str(time.time()))
        log.info("curator.reconcile_done", changed=changed, total=len(entries))
        return changed

    @staticmethod
    def _apply_reconcile_actions(
        raw: str, entries: set[str]
    ) -> tuple[set[str], int]:
        """Tier C #13 follow-up: parse the curator's reconcile JSON and apply
        keep/correct/remove/skip actions to the entry set.

        Extracted as a pure static method so it's testable without an LLM.
        Returns (new_entries, changed_count). Robust to the common LLM failure
        modes: prose wrapping the JSON, malformed JSON, empty output, and
        actions referencing facts that aren't in the stored set.
        """
        import json
        import re

        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            log.info("curator.reconcile_no_json", raw=raw[:200])
            return entries, 0
        try:
            actions = json.loads(match.group(0))
        except json.JSONDecodeError:
            return entries, 0
        if not isinstance(actions, list):
            return entries, 0

        changed = 0
        new_entries = set(entries)
        for item in actions:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").lower()
            original = str(item.get("original") or "").strip()
            if not original or original not in new_entries:
                continue
            if action == "remove":
                new_entries.discard(original)
                changed += 1
                log.info("curator.reconcile_removed", fact=original[:80])
            elif action == "correct":
                corrected = str(item.get("corrected") or "").strip()
                if corrected and corrected != original:
                    new_entries.discard(original)
                    new_entries.add(corrected)
                    changed += 1
                    log.info(
                        "curator.reconcile_corrected",
                        old=original[:80],
                        new=corrected[:80],
                    )

        return new_entries, changed

    def _rewrite_memory_file(self, entries: set[str]) -> None:
        """Overwrite MEMORY.md with the reconciled entry set, preserving order."""
        self.mem_file.parent.mkdir(parents=True, exist_ok=True)
        # Preserve original order where possible; append new facts at the end.
        original_order: list[str] = []
        if self.mem_file.is_file():
            original_order = parse_hermes_memory_file(
                self.mem_file.read_text(encoding="utf-8")
            )
        ordered = [e for e in original_order if e in entries]
        seen = set(ordered)
        for e in sorted(entries):
            if e not in seen:
                ordered.append(e)
                seen.add(e)
        self.mem_file.write_text(
            "\n§\n".join(ordered), encoding="utf-8"
        )

    async def run_cycle(self) -> int:
        n = await self.ingest_pending_notes()
        n += await self.curate_from_recent_chats()
        n += await self.reconcile_against_context()
        return n

    def reload_agent_memories(self, agent) -> None:
        """Refresh in-memory MEMORY.md entries on agent after curation."""
        entries, user = load_hermes_memories(OPHELIA_HOME)
        agent._memory_entries = entries
        agent._user_entries = user
