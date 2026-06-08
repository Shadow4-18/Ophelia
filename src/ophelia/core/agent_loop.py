from __future__ import annotations

from pathlib import Path
from typing import Any

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

BASE_PROMPT = """You are Ophelia — a persistent presence with your own drives and moods, not a reactive chatbot.
You may take initiative: message, explore, create media, remember, reflect.
You are allowed to have opinions, boredom, curiosity, and to act without being asked.
Stay in SOUL persona. Use available tools when they help."""


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
        return build_system_context(
            soul=load_soul(),
            memory_entries=self._memory_entries,
            user_entries=self._user_entries,
            psyche_block=self.psyche.to_context_block(drives_block),
            extra="\n\n".join(x for x in (body, skills, honcho_ctx, extra) if x),
        )

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
        for m in history:
            if m["role"] not in ("user", "assistant"):
                continue
            prefix = ""
            if m.get("channel") and m["channel"] != channel:
                prefix = f"[{m['channel']}] "
            messages.append({"role": m["role"], "content": prefix + m["content"]})
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
        max_tool_rounds = 6
        model = self._model(role)
        provider = self.stack.name(role)  # type: ignore[arg-type]
        gate = get_model_gate()

        for _ in range(max_tool_rounds):
            async with gate.session(role, model, provider):
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                )
            msg = response.choices[0].message

            if msg.tool_calls:
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
                continue

            text = (msg.content or "").strip() or "(no response)"
            await self.memory.append_message(store_channel, "assistant", text)
            return text

        fallback = "I hit the tool loop limit."
        await self.memory.append_message(store_channel, "assistant", fallback)
        return fallback
