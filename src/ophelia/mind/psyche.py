from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Mood:
    valence: float = 0.0  # -1 unpleasant .. +1 pleasant
    arousal: float = 0.3  # 0 calm .. 1 energized
    label: str = "neutral"
    # Baseline personality setpoint mood drifts toward when idle (homeostasis).
    baseline_valence: float = 0.15
    baseline_arousal: float = 0.3


@dataclass
class PsycheState:
    """Internal state driving human-like autonomy (Neuro prompter analogue).

    Mood has inertia: between ticks it drifts toward a personality baseline,
    so she returns to a resting temperament rather than staying stuck in a spike.
    """

    mood: Mood = field(default_factory=Mood)
    feelings: list[str] = field(default_factory=list)
    internal_thought: str = ""
    urges: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_context_block(self, drives_block: str = "") -> str:
        feelings = ", ".join(self.feelings) if self.feelings else "none"
        urges = ", ".join(self.urges) if self.urges else "none"
        parts = [
            f"Mood: {self.mood.label} (valence={self.mood.valence:.2f}, arousal={self.mood.arousal:.2f})",
            f"Feelings: {feelings}",
            f"Urges: {urges}",
            f"Internal thought: {self.internal_thought or '(quiet)'}",
        ]
        if drives_block:
            parts.append(drives_block)
        return "\n".join(parts)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> PsycheState:
        data = json.loads(raw)
        mood_data = data.get("mood") or {}
        return cls(
            mood=Mood(
                valence=float(mood_data.get("valence", 0)),
                arousal=float(mood_data.get("arousal", 0.3)),
                label=str(mood_data.get("label", "neutral")),
                baseline_valence=float(mood_data.get("baseline_valence", 0.15)),
                baseline_arousal=float(mood_data.get("baseline_arousal", 0.3)),
            ),
            feelings=list(data.get("feelings") or []),
            internal_thought=str(data.get("internal_thought") or ""),
            urges=list(data.get("urges") or []),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def apply_tick(self, tick: dict) -> None:
        mood = tick.get("mood") or {}
        if mood:
            # Inertia: blend new mood with previous (momentum), then clamp.
            prev_v = self.mood.valence
            prev_a = self.mood.arousal
            new_v = float(mood.get("valence", prev_v))
            new_a = float(mood.get("arousal", prev_a))
            self.mood.valence = max(-1.0, min(1.0, 0.55 * new_v + 0.45 * prev_v))
            self.mood.arousal = max(0.0, min(1.0, 0.55 * new_a + 0.45 * prev_a))
            if mood.get("label"):
                self.mood.label = str(mood["label"])
        if tick.get("feelings"):
            self.feelings = [str(x) for x in tick["feelings"]][:8]
        if tick.get("internal_thought"):
            self.internal_thought = str(tick["internal_thought"])[:2000]
        if tick.get("urges"):
            self.urges = [str(x) for x in tick["urges"]][:6]
        self.updated_at = time.time()

    def relax(self, elapsed_seconds: float) -> None:
        """Drift mood toward baseline between ticks (homeostasis)."""
        if elapsed_seconds <= 0:
            return
        # Rate: roughly 0.02 per minute toward baseline.
        rate = min(0.5, 0.02 * (elapsed_seconds / 60.0))
        self.mood.valence += (self.mood.baseline_valence - self.mood.valence) * rate
        self.mood.arousal += (self.mood.baseline_arousal - self.mood.arousal) * rate
        self.updated_at = time.time()

    def drift(self, dt_seconds: float) -> None:
        """Fine-grained continuous mood drift, no LLM call.

        Called frequently (every few seconds) by the drift loop to make mood
        flow continuously toward baseline instead of jumping only at LLM tick
        time. Adds small organic noise so the drift isn't mechanically smooth —
        mood wanders slightly even at rest, the way a real temperament does.

        This is purely numerical and cheap; it never touches the model. The
        heavier `apply_tick` (LLM-driven) still runs at the consciousness
        cadence and can push mood in new directions; `drift` just keeps it
        alive in between.
        """
        if dt_seconds <= 0:
            return
        # Decay rate: ~0.10 per minute toward baseline. Faster than relax()
        # because this runs often in small slices — we want visible but gentle
        # movement between ticks, not a snap back.
        rate = min(0.4, 0.10 * (dt_seconds / 60.0))
        self.mood.valence += (self.mood.baseline_valence - self.mood.valence) * rate
        self.mood.arousal += (self.mood.baseline_arousal - self.mood.arousal) * rate
        # Subtle organic noise: ±0.005 per call on each axis. Keeps the mood
        # from sitting perfectly still at baseline — it breathes.
        self.mood.valence = max(-1.0, min(1.0, self.mood.valence + random.uniform(-0.005, 0.005)))
        self.mood.arousal = max(0.0, min(1.0, self.mood.arousal + random.uniform(-0.005, 0.005)))
        self.updated_at = time.time()

    def tick_interval_seconds(self, base: int) -> float:
        """Higher arousal → faster inner loop (more Neuro-like restlessness).

        Floor is 8s (down from 15s) so an aroused Ophelia ticks roughly twice
        as fast as her baseline. We can't reach Neuro's sub-second cadence
        (cloud LLM latency), but the continuous drift loop already updates
        mood every 5s, so combined she feels continuously present rather than
        pulsing every 90s.
        """
        factor = 0.45 + (0.55 * (1.0 - self.mood.arousal))
        return max(8.0, base * factor)
