"""Director — coordinates the ensemble (Tier A #1).

Today Ophelia has ensemble v0: separate models per role (chat, consciousness,
vision, curator, image, video) with a model gate. What's missing is the layer
that makes her feel like *one continuous person*: a director that decides
whether to speak, which mind responds, at what pace, and with what urgency.

This is NOT a new chat model. It's a fast decision layer that runs *before*
the heavy chat/consciousness LLM call:

  Input  →  Director  →  {speak, skip, react_fast, defer}
                ↓
        routing + pacing knobs for the chosen mind
                ↓
        Chat / Consciousness / Reaction mind produces the actual reply

The director uses the consciousness model (cheap, always-on) so it doesn't
contend with the chat model on local hardware. Decisions are logged for
tuning — see DirectorLog.

Three response modes the director picks between:
  - "speak"     — full chat LLM reply, normal pacing.
  - "react"     — fast reaction path: short, in-character one-liner from a
                  small/fast model (the future "reaction" mind). Lower latency
                  than a full chat turn. Used for quips, acknowledgements,
                  quick emotional beats.
  - "defer"     — stay silent this turn; pressure still builds. The right
                  answer when nothing genuine moves her (most ticks).
  - "skip"      — explicit no-op; even pressure is reset slightly.

Urgency buckets affect pacing:
  - "low"      — slower TTS, longer pauses, longer bursts ok.
  - "normal"   — default pacing.
  - "high"     — short bursts, faster TTS, less reflection.

Enabled via OPHELIA_DIRECTOR=true (default off until you've tuned it).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from ophelia.config import OPHELIA_HOME
from ophelia.providers.model_gate import get_model_gate

if TYPE_CHECKING:
    from ophelia.config import Settings
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.mind.drives import DriveState
    from ophelia.mind.psyche import PsycheState

log = structlog.get_logger()

DirectorAction = Literal["speak", "react", "defer", "skip"]
Urgency = Literal["low", "normal", "high"]

DIRECTOR_PROMPT = """You are Ophelia's director — the layer that decides whether and how she responds, not what she says.

Given a snapshot of her state (mood, drives, pressure, recent activity, owner state, urgency triggers), decide ONE action:

- "speak"  — full reply is warranted. Something genuine needs saying.
- "react"  — a fast one-liner reaction suffices (quip, acknowledgement, emotional beat). Don't reach for a full turn.
- "defer"  — stay silent this turn. Pressure still builds. Most ticks should land here.
- "skip"   — explicit no-op, slight pressure relief (e.g. she just acted, or the moment passed).

Also pick an urgency bucket:
- "low"    — slow voice, longer pauses, longer bursts ok.
- "normal" — default pacing.
- "high"   — short bursts, faster voice, less reflection.

And a "pace" hint (one short sentence) the chosen mind will see — e.g. "short punch, hyped", "longer reflective flow", "single quick acknowledgement".

Output ONLY valid JSON:
{
  "action": "speak" | "react" | "defer" | "skip",
  "urgency": "low" | "normal" | "high",
  "pace_hint": "one short sentence",
  "reason": "why this decision (logged for tuning, not shown to user)"
}

Silence is correct by default. Only escalate to speak/react when something genuinely moves her — a due goal with real pressure, an owner message, an urge that's been building, something worth saying. Don't manufacture activity.
"""


@dataclass
class DirectorDecision:
    action: DirectorAction = "defer"
    urgency: Urgency = "normal"
    pace_hint: str = ""
    reason: str = ""
    decided_at: float = field(default_factory=time.time)

    @property
    def should_speak(self) -> bool:
        return self.action in ("speak", "react")

    @property
    def is_fast_reaction(self) -> bool:
        return self.action == "react"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class Director:
    """Fast decision layer over the ensemble.

    Runs before the chat/consciousness LLM call and routes the turn: speak /
    react / defer / skip, plus urgency + pacing knobs. Uses the consciousness
    model (cheap) so it doesn't contend with the chat model on local hardware.
    """

    def __init__(
        self,
        settings: "Settings",
        *,
        agent: "AgentLoop | None" = None,
        psyche: "PsycheState | None" = None,
        drives: "DriveState | None" = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.psyche = psyche
        self.drives = drives
        self.enabled = bool(settings.director_enabled)
        # Rolling log of decisions for tuning. One line per decision, JSON.
        self.log_path: Path = settings.data_dir / "director_log.jsonl"
        # Last decision — used by tests / dashboards.
        self.last: DirectorDecision | None = None
        # Cooldown so the director itself doesn't loop on a sticky trigger.
        self._last_decision_at: float = 0.0

    def available(self) -> bool:
        return self.enabled and self.agent is not None

    async def decide(
        self,
        *,
        trigger: str,
        context_summary: str = "",
        owner_active: bool = False,
    ) -> DirectorDecision:
        """Decide whether and how to respond this tick.

        Args:
          trigger: what woke the director ("tick", "user_message",
            "goal_due", "spontaneous_urge", "reaction_opportunity").
          context_summary: short text the director sees (recent activity,
            what the owner said, what's on screen). Keep it small.
          owner_active: is the owner mid-conversation right now? Strongly
            biases toward speak/react over defer.
        """
        if not self.available():
            # Disabled — default to "speak" so existing behavior is unchanged.
            self.last = DirectorDecision(
                action="speak" if owner_active else "defer",
                urgency="normal",
                reason="director_disabled",
            )
            return self.last

        pressure = self.drives.initiative_pressure() if self.drives else 0.0
        mood_label = self.psyche.mood.label if self.psyche else "neutral"
        valence = self.psyche.mood.valence if self.psyche else 0.0
        arousal = self.psyche.mood.arousal if self.psyche else 0.3

        snapshot = (
            f"Trigger: {trigger}\n"
            f"Owner active: {owner_active}\n"
            f"Pressure: {pressure:.2f} (threshold {self.settings.initiative_threshold:.2f})\n"
            f"Mood: {mood_label} (valence {valence:+.2f}, arousal {arousal:.2f})\n"
            f"Drives: {self.drives.to_context_block() if self.drives else '(none)'}\n"
            f"Context: {context_summary[:600] or '(none)'}"
        )

        messages = [
            {"role": "system", "content": DIRECTOR_PROMPT},
            {"role": "user", "content": snapshot},
        ]
        try:
            client = await self.agent._client("consciousness")  # type: ignore[union-attr]
            model = self.agent._model("consciousness")  # type: ignore[union-attr]
            gate = get_model_gate()
            provider = self.agent.stack.name("consciousness")  # type: ignore[union-attr]
            from ophelia.providers.fallback import extra_body_for

            async with gate.session("director", model, provider):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.4,
                    max_tokens=200,
                    extra_body=extra_body_for(self.agent.settings, provider),  # type: ignore[union-attr]
                )
            raw = (resp.choices[0].message.content or "").strip()
            self.last = self._parse_decision(raw)
        except Exception as e:
            log.warning("director.decide_failed", error=str(e))
            # Fall back to a sensible default — don't let a director failure
            # silence her. Owner-active → speak, otherwise defer.
            self.last = DirectorDecision(
                action="speak" if owner_active else "defer",
                urgency="high" if owner_active else "normal",
                reason=f"director_error: {e}",
            )
        self._last_decision_at = time.time()
        self._log_decision(trigger, context_summary)
        return self.last

    def _parse_decision(self, raw: str) -> DirectorDecision:
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", raw)
            if not match:
                return DirectorDecision(reason=f"unparseable: {raw[:120]}")
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return DirectorDecision(reason=f"unparseable: {raw[:120]}")
        action = str(data.get("action") or "defer").lower()
        if action not in ("speak", "react", "defer", "skip"):
            action = "defer"
        urgency = str(data.get("urgency") or "normal").lower()
        if urgency not in ("low", "normal", "high"):
            urgency = "normal"
        return DirectorDecision(
            action=action,  # type: ignore[arg-type]
            urgency=urgency,  # type: ignore[arg-type]
            pace_hint=str(data.get("pace_hint") or "")[:200],
            reason=str(data.get("reason") or "")[:300],
        )

    def _log_decision(self, trigger: str, context_summary: str) -> None:
        """Append the decision to a JSONL log for tuning / dashboards."""
        if self.last is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": self.last.decided_at,
                "trigger": trigger,
                "action": self.last.action,
                "urgency": self.last.urgency,
                "pace_hint": self.last.pace_hint,
                "reason": self.last.reason,
                "context_preview": context_summary[:200],
            }
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.debug("director.log_failed", error=str(e))

    def urgency_speed_mult(self) -> float:
        """TTS speed multiplier implied by the last decision's urgency."""
        if self.last is None:
            return 1.0
        if self.last.urgency == "high":
            return 1.10
        if self.last.urgency == "low":
            return 0.92
        return 1.0

    def urgency_burst_cap(self, base: int) -> int:
        """Burst-length cap implied by the last decision's urgency."""
        if self.last is None:
            return base
        if self.last.urgency == "high":
            return min(base, 200)
        if self.last.urgency == "low":
            return int(base * 1.4)
        return base
