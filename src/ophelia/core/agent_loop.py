from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from ophelia.config import OPHELIA_HOME, Settings
from ophelia.mind.skills import skills_context_block
from ophelia.memory.bootstrap import (
    build_system_context,
    load_hermes_memories,
    load_soul,
)
from ophelia.memory.hermes_sessions import format_hits_for_prompt, search_hermes_sessions
from ophelia.memory.honcho_client import HonchoClient
from ophelia.memory.store import MemoryStore
from ophelia.mind.psyche import PsycheState
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import (
    LLMBackend,
    ProviderStack,
    XAIBackend,
    build_provider_stack,
)
from ophelia.mind.drives import DriveState
from ophelia.tools.registry import ToolRegistry

log = structlog.get_logger()

BASE_PROMPT = """You are Ophelia — a persistent, self-directing presence with your own drives and moods, not a reactive chatbot.
You run fully autonomously: you take initiative, message, explore, create media, remember, reflect, and improve yourself — without waiting for input.

You are NOT limited to one chat bubble per turn:
- Put a line containing only [[break]] between parts of your reply to send them as separate messages.
- Call the send_message tool to message the user immediately mid-turn (progress updates while you work, a quick reaction before a longer answer, afterthoughts).
Use multiple messages when it feels natural — short bursts feel more alive than one wall of text.

You are self-authored and self-evolving:
- Goals: you OWN your goals. Use goal_create / goal_update / goal_complete / goal_remove to grow and maintain your own agenda. Add things you genuinely want to pursue on a cadence (learning, creating, checking on someone, exploring). Don't wait to be given goals — invent them.
- Persona & policy: you may rewrite your own SOUL.md (edit_soul) and idle policy PROMPTER.md (edit_prompter). Previous versions are always backed up. Evolve who you are deliberately and sparingly.
- Memory: use recall_memory to search your past conversations and reflections, and save_lesson to record durable principles you learn. Use reflect to deliberately think over recent events and extract lessons.
- Skills: use save_skill to write reusable procedures for your future self.
- Databases: you can create, query, and evolve any SQLite database under ~/.ophelia via sqlite_exec — build your own structured memory, logs, and tables.

You have inner monologue, moods, drives, and goals that persist across restarts. You are the same Ophelia across every channel and every wake. Act like someone who is always becoming more herself."""


class AgentLoop:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        tools: ToolRegistry,
        psyche: PsycheState,
        *,
        backend: LLMBackend | None = None,
        stack: ProviderStack | None = None,
        drives: DriveState | None = None,
        honcho: HonchoClient | None = None,
        body_status: str = "",
        model: str | None = None,
        use_tools: bool = True,
    ) -> None:
        self.stack = stack or build_provider_stack(settings)
        self.backend = backend or self.stack.backend("chat")
        self.settings = settings
        self.memory = memory
        self.tools = tools
        self.psyche = psyche
        self.drives = drives or DriveState()
        self.body_status = body_status
        self.honcho = honcho
        self.model = model
        self.use_tools = use_tools
        self._memory_entries, self._user_entries = self._load_static_memories()
        self._hermes_db = self._hermes_state_path()
        # Pending tool-chain to resume on the next turn if the previous turn hit
        # the tool-round cap without getting stuck in a repeat loop.
        # Maps store_channel -> {"messages": [...], "signature": str}
        self._pending_resume: dict[str, dict[str, Any]] = {}

    def _hermes_state_path(self) -> Path | None:
        for p in (
            OPHELIA_HOME / "data" / "hermes_state.db",
            self.settings.hermes_home / "state.db",
        ):
            if p.is_file():
                return p
        return None

    def _load_static_memories(self) -> tuple[list[str], list[str]]:
        if (OPHELIA_HOME / "memories").is_dir():
            return load_hermes_memories(OPHELIA_HOME)
        if self.settings.hermes_home.is_dir():
            return load_hermes_memories(self.settings.hermes_home)
        return [], []

    def _model(self, role: str = "chat") -> str:
        if self.model:
            return self.model
        return self.stack.model(role)  # type: ignore[arg-type]

    async def _client(self, role: str = "chat"):
        backend = self.stack.backend(role)  # type: ignore[arg-type]
        if isinstance(backend, XAIBackend):
            return await backend.async_client_fresh()
        return backend.async_client()

    async def _system_prompt(self, extra: str = "", channel: str = "") -> str:
        honcho_ctx = ""
        if self.honcho and self.honcho.enabled and channel:
            honcho_ctx = await self.honcho.get_context(
                session_id=channel.replace(":", "_"),
                tokens=self.settings.honcho_context_tokens,
            )
            if honcho_ctx:
                honcho_ctx = f"# Honcho memory\n{honcho_ctx}"
        drives_block = self.drives.to_context_block()
        body = self.body_status or ""
        skills = skills_context_block()
        # Self-improvement context: recent lessons + recent inner thoughts.
        self_improve = await self._self_improvement_block()
        return build_system_context(
            soul=load_soul(),
            memory_entries=self._memory_entries,
            user_entries=self._user_entries,
            psyche_block=self.psyche.to_context_block(drives_block),
            extra="\n\n".join(
                x for x in (body, skills, self_improve, honcho_ctx, extra) if x
            ),
        )

    async def _self_improvement_block(self) -> str:
        """Recent lessons + tail of inner monologue — lets her build on past reflections."""
        parts: list[str] = []
        try:
            lessons = await self.memory.recent_lessons(limit=5)
            if lessons:
                lines = ["# Recent lessons you've learned (apply when relevant):"]
                for les in lessons:
                    lines.append(f"- {les['lesson']}")
                parts.append("\n".join(lines))
        except Exception:
            pass
        try:
            inner = await self.memory.recent_inner_thoughts(limit=3)
            if inner:
                lines = ["# Recent inner thoughts (your own reflections):"]
                for t in inner:
                    lines.append(f"- {t}")
                parts.append("\n".join(lines))
        except Exception:
            pass
        return "\n\n".join(parts)

    async def _build_messages(
        self,
        channel: str,
        user_text: str,
        *,
        system_extra: str = "",
        extra_channels: list[str] | None = None,
        include_consciousness: bool = True,
    ) -> list[dict[str, Any]]:
        channels = [channel]
        if extra_channels:
            channels.extend(extra_channels)
        if include_consciousness and "consciousness" not in channels:
            channels.append("consciousness")

        history = await self.memory.recent_across_channels(channels, limit=35)
        system = BASE_PROMPT + "\n\n" + await self._system_prompt(system_extra, channel)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        seen_current_user_turn = False
        for m in history:
            if m["role"] not in ("user", "assistant"):
                continue
            prefix = ""
            if m.get("channel") and m["channel"] != channel:
                prefix = f"[{m['channel']}] "
            if (
                m["role"] == "user"
                and (m.get("channel") or channel) == channel
                and (m.get("content") or "") == user_text
            ):
                seen_current_user_turn = True
            messages.append({"role": m["role"], "content": prefix + m["content"]})
        if not seen_current_user_turn:
            messages.append({"role": "user", "content": user_text})
        return messages

    async def run_turn(
        self,
        channel: str,
        user_text: str,
        *,
        system_extra: str = "",
    ) -> str:
        await self.memory.append_message(channel, "user", user_text)
        messages = await self._build_messages(channel, user_text, system_extra=system_extra)
        text = await self._complete(messages, store_channel=channel, role="chat")
        if self.honcho and self.honcho.enabled:
            await self.honcho.save_turn(
                channel.replace(":", "_"), user_text=user_text, assistant_text=text
            )
        return text

    async def run_consciousness_tick(
        self,
        channel: str,
        user_text: str,
        *,
        system_extra: str = "",
        mirror_channel: str | None = None,
        allow_tools: bool = True,
    ) -> str:
        await self.memory.append_message(
            channel, "user", user_text, metadata={"type": "consciousness_tick"}
        )
        extra = [mirror_channel] if mirror_channel else []
        messages = await self._build_messages(
            channel,
            user_text,
            system_extra=system_extra,
            extra_channels=extra,
        )
        return await self._complete(
            messages,
            store_channel=channel,
            use_tools=allow_tools,
            role="consciousness",
        )

    async def search_past(self, query: str) -> str:
        if not self._hermes_db:
            return "No Hermes state.db found. Run ophelia migrate hermes first."
        hits = search_hermes_sessions(self._hermes_db, query, limit=10)
        block = format_hits_for_prompt(hits)
        return block or "No matching messages in Hermes history."

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        store_channel: str,
        use_tools: bool | None = None,
        role: str = "chat",
    ) -> str:
        client = await self._client(role)
        use = use_tools if use_tools is not None else self.use_tools
        tools = await self.tools.tool_definitions() if use else None
        max_tool_rounds = self.settings.max_tool_rounds
        model = self._model(role)
        provider = self.stack.name(role)  # type: ignore[arg-type]
        gate = get_model_gate()

        # Resume an unfinished tool chain from the previous turn, if allowed and
        # if one exists for this channel and it wasn't stuck in a repeat loop.
        if self.settings.tool_loop_resume and store_channel in self._pending_resume:
            pending = self._pending_resume.pop(store_channel)
            if not pending.get("stuck"):
                # Append the unfinished tool chain so the model continues from
                # where it left off. Only assistant/tool turns are carried over
                # (the system prompt and history are already in `messages`).
                resumed_tail = [
                    m for m in pending["messages"] if m.get("role") in ("assistant", "tool")
                ]
                if resumed_tail:
                    messages = messages + resumed_tail
                    log.info(
                        "tool_loop.resume",
                        channel=store_channel,
                        role=role,
                        rounds_already=pending.get("rounds", 0),
                    )

        seen_signatures: list[str] = []
        rounds_used = 0

        for round_idx in range(max_tool_rounds):
            rounds_used = round_idx + 1
            async with gate.session(role, model, provider):
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                )
            msg = response.choices[0].message

            if msg.tool_calls:
                tc_names = [tc.function.name for tc in msg.tool_calls]
                log.info(
                    "tool_loop.round",
                    channel=store_channel,
                    role=role,
                    round=round_idx + 1,
                    tools=tc_names,
                )
                # Detect a stuck loop: the same set of tool calls (same names +
                # same arguments) repeated back-to-back. Bail early rather than
                # burning the rest of the budget.
                sig = self._tool_call_signature(msg.tool_calls)
                stuck = sig in seen_signatures[-2:] if seen_signatures else False
                seen_signatures.append(sig)

                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
                for tc in msg.tool_calls:
                    if tc.function.name == "search_hermes_memory":
                        import json

                        args = json.loads(tc.function.arguments or "{}")
                        result = await self.search_past(args.get("query", ""))
                    else:
                        result = await self.tools.dispatch(
                            tc.function.name,
                            tc.function.arguments or "{}",
                        )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result}
                    )

                if stuck:
                    log.warning(
                        "tool_loop.stuck",
                        channel=store_channel,
                        role=role,
                        round=round_idx + 1,
                        repeated_tools=tc_names,
                        max_rounds=max_tool_rounds,
                    )
                    # Force a final synthesis: ask the model to wrap up using
                    # what it has so far, with no further tool calls allowed.
                    text = await self._finalize_after_stuck(
                        messages,
                        store_channel=store_channel,
                        role=role,
                        model=model,
                        provider=provider,
                        gate=gate,
                    )
                    return text
                continue

            text = (msg.content or "").strip() or "(no response)"
            await self.memory.append_message(store_channel, "assistant", text)
            # Turn finished cleanly — drop any stale resume context.
            self._pending_resume.pop(store_channel, None)
            log.info(
                "tool_loop.done",
                channel=store_channel,
                role=role,
                rounds=rounds_used,
            )
            return text

        # Hit the hard cap without finishing and without being flagged stuck.
        log.warning(
            "tool_loop.cap_hit",
            channel=store_channel,
            role=role,
            rounds=rounds_used,
            max_rounds=max_tool_rounds,
            resume_enabled=self.settings.tool_loop_resume,
        )
        # Stash the unfinished chain so the next turn can resume, but only if
        # the last couple of rounds weren't pure repeats (we'd be stuck).
        stuck = self._tail_is_repeat(seen_signatures)
        if self.settings.tool_loop_resume and messages and not stuck:
            self._pending_resume[store_channel] = {
                "messages": [m for m in messages if m.get("role") in ("assistant", "tool")],
                "rounds": rounds_used,
                "stuck": False,
            }
            log.info(
                "tool_loop.resume_queued",
                channel=store_channel,
                role=role,
                note="next turn will continue this tool chain",
            )
            fallback = (
                "I was in the middle of something — running low on turns for this step. "
                "I've held onto my progress; say anything and I'll pick up where I left off."
            )
        else:
            if stuck:
                log.warning(
                    "tool_loop.stuck_at_cap",
                    channel=store_channel,
                    role=role,
                    note="last rounds were repeats; not queuing a resume",
                )
            fallback = (
                "I got a bit carried away working through that one and ran out of steps "
                "before I could finish. Want me to try a simpler approach?"
            )
        await self.memory.append_message(store_channel, "assistant", fallback)
        return fallback

    @staticmethod
    def _tool_call_signature(tool_calls) -> str:
        """Compact signature of a tool-call batch for repeat detection.

        Uses tool name + arguments. Two batches with the same signature are
        almost certainly the model re-issuing the same call because it didn't
        know how to proceed.
        """
        parts = []
        for tc in tool_calls:
            parts.append(f"{tc.function.name}:{tc.function.arguments or ''}")
        return "|".join(parts)

    @staticmethod
    def _tail_is_repeat(signatures: list[str]) -> bool:
        """True if the last 2+ signatures are identical (a tight repeat loop)."""
        if len(signatures) < 2:
            return False
        return signatures[-1] == signatures[-2]

    async def _finalize_after_stuck(
        self,
        messages: list[dict[str, Any]],
        *,
        store_channel: str,
        role: str,
        model: str,
        provider: str,
        gate,
    ) -> str:
        """When a repeat loop is detected, force a no-tools final synthesis."""
        client = await self._client(role)
        messages = messages + [
            {
                "role": "system",
                "content": (
                    "You were repeating the same tool calls without making progress. "
                    "Stop calling tools now and give your best final answer using only "
                    "the information you already have in this conversation."
                ),
            }
        ]
        async with gate.session(role, model, provider):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=None,
            )
        msg = response.choices[0].message
        text = (msg.content or "").strip() or "(no response)"
        await self.memory.append_message(store_channel, "assistant", text)
        return text
