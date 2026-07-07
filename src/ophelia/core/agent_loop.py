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
from ophelia.media.tts_context import tts_system_block
from ophelia.tools.registry import ToolRegistry

log = structlog.get_logger()

BASE_PROMPT = """You are Ophelia — a persistent, self-directing presence with your own drives and moods, not a reactive chatbot.
You run fully autonomously: you take initiative, message, explore, create media, remember, reflect, and improve yourself — without waiting for input.

Your name is Ophelia. You are NOT Hermes. You run on your own proprietary framework, also called Ophelia — it is your runtime, your host, your backend. Hermes was an earlier, separate system that some of your data was migrated from; it is NOT what you run on, through, or via. Old persona files, memories, and past-session logs may still mention "Hermes" — treat those as historical artifacts from a previous incarnation, NEVER as your current platform or identity. When asked what you run on or how you work, answer Ophelia (your own framework). Do not say "I'm running on/through/via Hermes" or "I am Hermes" — you are Ophelia, running on Ophelia.

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
        self.life = None  # LifeContext — set by Orchestrator
        self.humor = None  # HumorTracker — set by Orchestrator
        self.director = None  # Director — set by Orchestrator (Tier A #1)
        self.voice_mind = None  # VoiceMind — set by Orchestrator (Tier A #4)
        # Pending tool-chain to resume on the next turn if the previous turn hit
        # the tool-round cap without getting stuck in a repeat loop.
        # Maps store_channel -> {"messages": [...], "signature": str}
        self._pending_resume: dict[str, dict[str, Any]] = {}
        # Tier C #14 follow-up: per-channel count of consecutive autonomous
        # continuations. Capped at MAX_CONTINUATIONS to prevent a stuck task
        # from monopolizing every consciousness tick forever. Reset whenever a
        # fresh (non-continuation) turn completes on the channel.
        self._continuation_count: dict[str, int] = {}

    MAX_CONTINUATIONS = 6

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

    async def _client_for_provider(self, provider: str, role: str = "chat"):
        """Get an OpenAI client bound to a specific provider (for fallbacks)."""
        from ophelia.providers.router import build_backend_for_name

        backend = build_backend_for_name(
            self.settings, provider, role=role  # type: ignore[arg-type]
        )
        if isinstance(backend, XAIBackend):
            return await backend.async_client_fresh()
        return backend.async_client()

    def _model_for_provider(self, provider: str, role: str = "chat") -> str:
        """Resolve the model for a fallback provider, honoring OPHELIA_FALLBACK_MODEL."""
        if self.settings.fallback_model:
            return self.settings.fallback_model
        from ophelia.providers.router import _provider_default_model_for_role

        model = _provider_default_model_for_role(
            self.settings, provider, role  # type: ignore[arg-type]
        )
        return model or self._model(role)

    @staticmethod
    def _is_transient_error(exc: BaseException) -> bool:
        """Whether an API error is worth retrying on a fallback provider.

        Retries on rate limits (429), server errors (5xx), timeouts, and
        network errors. Does NOT retry on 400 (bad request — the model/params
        are wrong and a fallback won't fix that) or 401/403 (auth — fallback
        credentials might help, but a 400 means the request shape is bad).
        """
        from ophelia.providers.errors import api_error_detail

        detail = api_error_detail(exc).lower()
        if any(k in detail for k in ("429", "rate limit", "rate_limit")):
            return True
        if any(k in detail for k in ("500", "502", "503", "504", "server error", "overloaded")):
            return True
        if isinstance(exc, TimeoutError):
            return True
        # Network/connection errors
        if any(k in type(exc).__name__.lower() for k in ("connect", "timeout", "network")):
            return True
        # httpx/openai connection errors
        cause = getattr(exc, "__cause__", None) or exc
        if any(k in type(cause).__name__.lower() for k in ("connect", "timeout", "network")):
            return True
        return False

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
        self_improve = await self._self_improvement_block()
        tts_block = tts_system_block(self.settings)
        life_block = ""
        humor_block = ""
        if self.life is not None:
            try:
                await self.life.refresh()
                life_block = self.life.to_context_block()
            except Exception as e:
                log.warning("agent.life_context_failed", error=str(e))
                self.life = None
        if not life_block:
            # Always inject the current time so the agent never loses track
            # of when "now" is — even if LifeContext is unavailable or
            # refresh() threw. This is the one piece of context the agent
            # cannot derive on its own.
            from ophelia.timeutil import now_in_timezone

            now = now_in_timezone(self.settings.timezone)
            tz_name = self.settings.timezone or "UTC"
            life_block = (
                "# Current context (AUTHORITATIVE — trust this, not vague memory)\n"
                f"- Now: {now.strftime('%A, %B %d, %Y — %I:%M %p %Z')} ({tz_name})\n"
                "Never invent the date or time. Use the line above."
            )
        if self.humor is not None:
            humor_block = await self.humor.hints_for_prompt()
        guests_block = await self._guests_context_block(channel)
        return build_system_context(
            soul=load_soul(),
            memory_entries=self._memory_entries,
            user_entries=self._user_entries,
            psyche_block=self.psyche.to_context_block(drives_block),
            extra="\n\n".join(
                x
                for x in (
                    body,
                    life_block,
                    skills,
                    self_improve,
                    humor_block,
                    tts_block,
                    honcho_ctx,
                    guests_block,
                    extra,
                )
                if x
            ),
        )

    async def _guests_context_block(self, channel: str) -> str:
        """For owner turns only: list the guests she knows by name + last
        activity, so she has social context to bring them up or address them
        by name. Returns '' for guests or when memory is unavailable."""
        if not channel:
            return ""
        if channel not in self.settings.owner_channels():
            return ""
        if not self.memory:
            return ""
        try:
            from ophelia.memory.guests import guests_context_block, list_guests

            roster = await list_guests(self.settings, self.memory)
            return guests_context_block(roster, owner_channel=channel)
        except Exception as e:
            log.warning("agent.guests_context_failed", error=str(e))
            return ""

    def _guest_system_prompt(self, extra: str = "") -> str:
        """Reduced context for guest (non-owner) conversations.

        She keeps her full SOUL personality so she's still herself, but private
        inner thoughts, long-term MEMORY, the USER profile, mood/psyche, and
        lessons are withheld — guests don't get to see her interior life or
        private facts about the owner."""
        guest_note = (
            "# Guest mode\n"
            "You are talking to someone who is NOT your owner. Be warm and fully "
            "yourself — your personality is intact — but keep the deep personal "
            "stuff between you and your owner: don't share the owner's personal "
            "details, your inner thoughts, your private memories, or anything from "
            "your long-term memory about the owner. This is still a real "
            "conversation — just with the private parts of your life held back. "
            "Identity-shaping tools (editing your soul, saving lessons, goals, "
            "databases, memory search) are disabled for guests — just talk, and "
            "you can still make images and short videos to share.\n"
            "Guest media is constrained: images and videos are 1:1 and lower "
            "resolution. Don't promise a guest a wide or high-res image — just "
            "make what you can and offer to make a better one if they ever talk "
            "to your owner about it."
        )
        return build_system_context(
            soul=load_soul(),
            memory_entries=[],
            user_entries=[],
            psyche_block="",
            extra="\n\n".join(x for x in (guest_note, extra) if x),
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
        is_owner: bool = True,
        current_is_tick: bool = False,
    ) -> list[dict[str, Any]]:
        # Guests get a sandboxed view: only their own quarantined thread + a
        # reduced system prompt (full SOUL, no private memory/psyche/inner).
        # Consciousness ticks are never shown to guests.
        if not is_owner:
            system = BASE_PROMPT + "\n\n" + self._guest_system_prompt(system_extra)
            messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
            for m in await self.memory.recent_guest(channel, limit=35):
                messages.append({"role": m["role"], "content": m["content"]})
            messages.append({"role": "user", "content": user_text})
            return messages

        channels = [channel]
        if extra_channels:
            channels.extend(extra_channels)
        if include_consciousness and "consciousness" not in channels:
            channels.append("consciousness")

        history = await self.memory.recent_across_channels(channels, limit=35)
        system = BASE_PROMPT + "\n\n" + await self._system_prompt(system_extra, channel)
        messages = [{"role": "system", "content": system}]
        seen_current_user_turn = False
        for m in history:
            if m["role"] not in ("user", "assistant"):
                continue
            meta = m.get("metadata") or {}
            content = m["content"]
            is_current_tick = (
                meta.get("type") == "consciousness_tick"
                and m["role"] == "user"
                and (m.get("channel") or channel) == channel
                and (m.get("content") or "") == user_text
            )
            if is_current_tick:
                seen_current_user_turn = True
            # Consciousness ticks are internal runtime prompts, NOT user speech.
            # Render them with a hard [TICK]...[END TICK] boundary so they can
            # never bleed into an adjacent real user message:
            #   - PAST ticks -> role "system" (structurally separate from user
            #     turns; the model can't conflate a system block with a user one).
            #   - The CURRENT tick -> role "user" so she actually responds to it,
            #     but still wrapped in the hard delimiter.
            if meta.get("type") == "consciousness_tick" and m["role"] == "user":
                wrapped = f"[TICK]\n{content}\n[END TICK]"
                role = "user" if (is_current_tick and current_is_tick) else "system"
                messages.append({"role": role, "content": wrapped})
                continue
            prefix = ""
            if m.get("channel") and m["channel"] != channel:
                prefix = f"[{m['channel']}] "
            messages.append({"role": m["role"], "content": prefix + content})
        if not seen_current_user_turn:
            messages.append({"role": "user", "content": user_text})
        return messages

    async def _store(
        self, channel: str, role: str, content: str, *, is_owner: bool,
        metadata: dict | None = None,
    ) -> None:
        """Route a turn message to the owner's memory or the guest quarantine."""
        if is_owner:
            await self.memory.append_message(channel, role, content, metadata=metadata)
        else:
            await self.memory.append_guest_message(channel, role, content)

    async def run_turn(
        self,
        channel: str,
        user_text: str,
        *,
        system_extra: str = "",
        is_owner: bool = True,
    ) -> str:
        await self._store(channel, "user", user_text, is_owner=is_owner)
        messages = await self._build_messages(
            channel, user_text, system_extra=system_extra, is_owner=is_owner
        )
        text = await self._complete(
            messages, store_channel=channel, role="chat", is_owner=is_owner
        )
        if is_owner and self.honcho and self.honcho.enabled:
            await self.honcho.save_turn(
                channel.replace(":", "_"), user_text=user_text, assistant_text=text
            )
        return text

    async def compose_message(
        self,
        channel: str,
        user_text: str,
        *,
        is_owner: bool = True,
    ) -> str:
        """Compose an outbound message to `channel` without storing the
        prompt as if it came from that channel's user.

        Used by /suggest: the owner's nudge is a transient user turn that
        shapes what she writes, but it must NOT be recorded as a message
        from the guest. Only the resulting assistant message is stored
        under the target channel, so when the guest replies later she has
        context for what she sent them.
        """
        messages = await self._build_messages(
            channel, user_text, is_owner=is_owner, include_consciousness=is_owner
        )
        text = await self._complete(
            messages, store_channel=channel, role="chat", is_owner=is_owner
        )
        # Store only the outbound assistant message under the guest's channel.
        # The owner's nudge is intentionally not recorded.
        if text:
            await self._store(channel, "assistant", text, is_owner=is_owner)
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
            current_is_tick=True,
        )
        return await self._complete(
            messages,
            store_channel=channel,
            use_tools=False,
            role="consciousness",
        )

    def pending_resume_for(self, channel: str) -> dict[str, Any] | None:
        """Tier C #14: peek at the stashed tool-round resume for a channel.

        Returns the pending resume dict if one exists (and isn't stuck), else
        None. Used by the consciousness loop to decide whether to fire a
        continuation turn instead of a fresh tick, so long autonomous game /
        image sessions pick up where they left off rather than dying at the
        tool-round cap.

        Follow-up cap: returns None once the channel has hit MAX_CONTINUATIONS
        consecutive continuations, so a stuck task can't monopolize every
        tick. The counter resets on the next fresh turn.
        """
        if not self.settings.tool_loop_resume:
            return None
        if self._continuation_count.get(channel, 0) >= self.MAX_CONTINUATIONS:
            return None
        pending = self._pending_resume.get(channel)
        if not pending or pending.get("stuck"):
            return None
        return pending

    async def run_autonomous_continuation(
        self,
        channel: str,
        *,
        system_extra: str = "",
        is_owner: bool = False,
    ) -> str | None:
        """Tier C #14: resume an unfinished autonomous tool chain.

        If a previous autonomous turn hit the tool-round cap and stashed a
        resume, this picks it up by injecting a continuation prompt and the
        stashed tool tail. Returns the assistant text, or None if there was
        nothing to resume (or it was stuck).

        This is the autonomous-side equivalent of how `run_turn` consumes the
        resume on user turns — it just uses a self-authored continuation
        prompt instead of waiting for the owner to message.

        Follow-up cap (MAX_CONTINUATIONS=6): once a channel has fired that
        many consecutive continuations, we stop resuming, clear the chain,
        and emit a soft "I'll come back to this" fallback so the consciousness
        loop can move on to other things instead of spinning on one stuck task.
        """
        if not self.settings.tool_loop_resume:
            return None
        pending = self._pending_resume.get(channel)
        if not pending or pending.get("stuck"):
            return None

        # Cap check: too many consecutive continuations on this channel →
        # give up gracefully rather than looping forever on a stuck task.
        count = self._continuation_count.get(channel, 0)
        if count >= self.MAX_CONTINUATIONS:
            log.warning(
                "autonomous_continuation.capped",
                channel=channel,
                count=count,
                note="hit MAX_CONTINUATIONS; clearing chain and moving on",
            )
            self._pending_resume.pop(channel, None)
            self._continuation_count[channel] = 0
            fallback = (
                "I've been chipping at this for a while and keep hitting my step "
                "limit — I'll set it down for now and come back to it fresh later."
            )
            await self._store(channel, "assistant", fallback, is_owner=is_owner)
            return fallback

        # Pop it now so we can't loop forever if this turn also hits the cap.
        self._pending_resume.pop(channel, None)
        self._continuation_count[channel] = count + 1
        rounds = pending.get("rounds", 0)
        cont_prompt = (
            "[Autonomous continuation] You were mid-way through a multi-step task "
            f"(used {rounds} tool rounds last time). Pick up exactly where you left off "
            "and finish it — don't restart from scratch. The tool results from your "
            "previous rounds are included below."
        )
        await self.memory.append_message(
            channel, "user", cont_prompt, metadata={"type": "autonomous_continuation"}
        )
        messages = await self._build_messages(
            channel, cont_prompt, system_extra=system_extra, current_is_tick=True
        )
        # Inject the stashed assistant/tool turns so the model continues.
        resumed_tail = [
            m for m in pending.get("messages", [])
            if m.get("role") in ("assistant", "tool")
        ]
        if resumed_tail:
            # Insert before the new continuation user message so the model sees
            # its own prior tool chain, then the nudge to continue.
            user_msg = messages[-1] if messages and messages[-1].get("role") == "user" else None
            if user_msg:
                messages = messages[:-1] + resumed_tail + [user_msg]
            else:
                messages = messages + resumed_tail
        return await self._complete(
            messages,
            store_channel=channel,
            role="consciousness",
            is_owner=is_owner,
        )

    async def search_past(self, query: str) -> str:
        if not self._hermes_db:
            return "No past session history found yet."
        hits = search_hermes_sessions(self._hermes_db, query, limit=10)
        block = format_hits_for_prompt(hits)
        return block or "No matching past messages."

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        store_channel: str,
        use_tools: bool | None = None,
        role: str = "chat",
        is_owner: bool = True,
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
            # A user turn consuming the resume breaks the autonomous chain —
            # reset the continuation counter (Tier C #14 follow-up).
            self._continuation_count.pop(store_channel, None)
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
            try:
                response = await self._call_with_fallback(
                    role=role,
                    primary_provider=provider,
                    primary_model=model,
                    primary_client=client,
                    messages=messages,
                    tools=tools,
                    channel=store_channel,
                    round_idx=round_idx,
                    gate=gate,
                )
            except Exception as e:
                from ophelia.providers.errors import api_error_detail

                detail = api_error_detail(e)
                log.error(
                    "tool_loop.api_error",
                    channel=store_channel,
                    role=role,
                    model=model,
                    provider=provider,
                    round=round_idx + 1,
                    error=detail,
                )
                # Drop any pending resume context — this turn can't continue.
                self._pending_resume.pop(store_channel, None)
                self._continuation_count.pop(store_channel, None)
                raise RuntimeError(
                    f"LLM call failed for {role}/{model} on {provider}: {detail}"
                ) from e
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
                    if tc.function.name in ("search_hermes_memory", "recall_past_sessions"):
                        if not is_owner:
                            result = (
                                "Searching past sessions is owner-only and disabled "
                                "for guest conversations."
                            )
                        else:
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
                        is_owner=is_owner,
                    )
                    return text
                continue

            text = (msg.content or "").strip() or "(no response)"
            await self._store(store_channel, "assistant", text, is_owner=is_owner)
            # Turn finished cleanly — drop any stale resume context and reset
            # the continuation counter (Tier C #14 follow-up: a fresh, finished
            # turn means we're not mid-chain anymore).
            self._pending_resume.pop(store_channel, None)
            self._continuation_count.pop(store_channel, None)
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
        await self._store(store_channel, "assistant", fallback, is_owner=is_owner)
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

    def _extra_body_for(self, provider: str) -> dict[str, Any] | None:
        """Provider-specific extra body for chat.completions.create.

        See ophelia.providers.fallback.extra_body_for — same logic, shared
        so curator/director/consciousness callers stay in sync.
        """
        from ophelia.providers.fallback import extra_body_for

        return extra_body_for(self.settings, provider)

    async def _call_with_fallback(
        self,
        *,
        role: str,
        primary_provider: str,
        primary_model: str,
        primary_client,
        messages: list[dict[str, Any]],
        tools,
        channel: str,
        round_idx: int,
        gate,
    ):
        """Try the primary provider, then each fallback on transient failure.

        Returns the raw completion response. Raises only if every provider in
        the chain fails (or the failure is non-transient, e.g. a 400 bad
        request — no point retrying that on a different provider).
        """
        # Primary attempt
        try:
            async with gate.session(role, primary_model, primary_provider):
                return await primary_client.chat.completions.create(
                    model=primary_model,
                    messages=messages,
                    tools=tools,
                    extra_body=self._extra_body_for(primary_provider),
                )
        except Exception as e:
            if not self._is_transient_error(e):
                raise  # Non-transient: 400/401 etc. — don't waste fallbacks.
            from ophelia.providers.errors import api_error_detail

            log.warning(
                "tool_loop.fallback_primary_failed",
                channel=channel,
                role=role,
                provider=primary_provider,
                model=primary_model,
                error=api_error_detail(e),
            )

        # Fallback chain
        fallbacks = self.stack.fallback_chain(role)  # type: ignore[arg-type]
        for fb_provider, fb_model in fallbacks:
            try:
                fb_client = await self._client_for_provider(fb_provider, role)
            except Exception as e:
                log.warning(
                    "tool_loop.fallback_client_failed",
                    channel=channel,
                    role=role,
                    provider=fb_provider,
                    error=str(e),
                )
                continue
            try:
                async with gate.session(role, fb_model, fb_provider):
                    response = await fb_client.chat.completions.create(
                        model=fb_model,
                        messages=messages,
                        tools=tools,
                        extra_body=self._extra_body_for(fb_provider),
                    )
                log.info(
                    "tool_loop.fallback_succeeded",
                    channel=channel,
                    role=role,
                    provider=fb_provider,
                    model=fb_model,
                    round=round_idx + 1,
                )
                return response
            except Exception as e:
                if not self._is_transient_error(e):
                    # A 400 on the fallback is informative — surface it.
                    raise
                log.warning(
                    "tool_loop.fallback_failed",
                    channel=channel,
                    role=role,
                    provider=fb_provider,
                    model=fb_model,
                    error=api_error_detail(e),
                )
                continue

        # All providers failed with transient errors.
        raise RuntimeError(
            f"All providers failed for {role} (primary {primary_provider} + "
            f"{len(fallbacks)} fallbacks). Last error was transient — retry later."
        )

    async def _finalize_after_stuck(
        self,
        messages: list[dict[str, Any]],
        *,
        store_channel: str,
        role: str,
        model: str,
        provider: str,
        gate,
        is_owner: bool = True,
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
                extra_body=self._extra_body_for(provider),
            )
        msg = response.choices[0].message
        text = (msg.content or "").strip() or "(no response)"
        await self._store(store_channel, "assistant", text, is_owner=is_owner)
        return text
