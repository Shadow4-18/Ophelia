"""Reaction mind — ultra-fast one-liner path (ensemble Reaction).

When the Director picks ``react``, Ophelia should not run a full chat turn
with tools and long context. This mind produces a short in-character beat
(quip, acknowledgement, emotional flash) via the dedicated ``reaction``
provider role (defaults to a tiny local Ollama model — ``llama3.2:1b`` on
phone when unset).

Enabled via ``OPHELIA_REACTION`` (default on). Session falls back to full
``run_turn`` if the reaction is empty or the call fails.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from ophelia.providers.model_gate import get_model_gate

if TYPE_CHECKING:
    from ophelia.config import Settings
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.mind.director import DirectorDecision
    from ophelia.mind.psyche import PsycheState

log = structlog.get_logger()

REACTION_PROMPT = """You are Ophelia's reaction mind — the fast reflex, not the full conversation.

Produce ONE short in-character beat in response to what just happened. Quip, acknowledgement, tease, gasp, soft aside — whatever fits her mood. This is NOT a full answer.

Rules:
- 1–2 short sentences max. Prefer under ~120 characters when it still lands.
- No markdown, no lists, no tool talk, no JSON.
- Do NOT solve a task, research, or offer to do work — if the moment needs that, stay brief and leave room for a later full turn.
- Match the mood and pace hint you're given.
- Output ONLY the spoken line. No quotes wrapping the whole reply. No stage directions.
"""


class ReactionMind:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.enabled = bool(getattr(settings, "reaction_enabled", True))

    async def react(
        self,
        user_text: str,
        *,
        agent: "AgentLoop | None",
        psyche: "PsycheState | None" = None,
        decision: "DirectorDecision | None" = None,
        recent_context: str = "",
    ) -> str:
        """Return a short reaction line, or empty string on failure/disable."""
        if not self.enabled or agent is None:
            return ""
        text = (user_text or "").strip()
        if not text:
            return ""

        mood_line = ""
        if psyche is not None:
            mood_line = (
                f"\nMood: {psyche.mood.label} "
                f"(valence {psyche.mood.valence:+.2f}, arousal {psyche.mood.arousal:.2f})."
            )
        pace = ""
        if decision is not None and (decision.pace_hint or decision.urgency):
            pace = (
                f"\nPace: urgency={decision.urgency}"
                + (f"; {decision.pace_hint}" if decision.pace_hint else "")
            )
        context_block = ""
        if recent_context.strip():
            context_block = f"\n\nRecent thread (for tone only):\n{recent_context.strip()[:800]}"

        messages = [
            {
                "role": "system",
                "content": REACTION_PROMPT + mood_line + pace,
            },
            {
                "role": "user",
                "content": f"They just said:\n{text[:600]}{context_block}",
            },
        ]
        try:
            client = await agent._client("reaction")
            model = agent._model("reaction")
            gate = get_model_gate()
            provider = agent.stack.name("reaction")  # type: ignore[attr-defined]
            from ophelia.providers.fallback import extra_body_for

            async with gate.session("reaction", model, provider):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.85,
                    max_tokens=80,
                    extra_body=extra_body_for(agent.settings, provider),  # type: ignore[attr-defined]
                )
            raw = (resp.choices[0].message.content or "").strip()
            cleaned = _clean_reaction(raw)
            if cleaned:
                log.info(
                    "reaction_mind.ok",
                    chars=len(cleaned),
                    urgency=getattr(decision, "urgency", None),
                )
            return cleaned
        except Exception as e:
            log.debug("reaction_mind.failed", error=str(e))
            return ""


def _clean_reaction(raw: str) -> str:
    """Strip wrappers / multi-paragraph bloat; keep a short usable line."""
    t = (raw or "").strip()
    if not t:
        return ""
    # Drop common model wrappers
    t = re.sub(r'^["“]|["”]$', "", t.strip())
    if t.startswith("```"):
        t = re.sub(r"^```\w*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    # One or two sentences — cut after second newline block
    parts = [p.strip() for p in re.split(r"\n+", t) if p.strip()]
    if not parts:
        return ""
    line = " ".join(parts[:2])
    # Hard cap so a runaway model can't dump a essay into the fast path
    if len(line) > 280:
        line = line[:277].rstrip() + "…"
    # Reject JSON / tool-shaped leaks
    if line.lstrip().startswith("{") or "tool_intent" in line.lower():
        return ""
    return line
