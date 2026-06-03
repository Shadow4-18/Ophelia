from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Mood:
    valence: float = 0.0  # -1 unpleasant .. +1 pleasant
    arousal: float = 0.3  # 0 calm .. 1 energized
    label: str = "neutral"


@dataclass
class PsycheState:
    """Internal state driving human-like autonomy (Neuro prompter analogue)."""

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
            ),
            feelings=list(data.get("feelings") or []),
            internal_thought=str(data.get("internal_thought") or ""),
            urges=list(data.get("urges") or []),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def apply_tick(self, tick: dict) -> None:
        mood = tick.get("mood") or {}
        if mood:
            self.mood.valence = max(-1.0, min(1.0, float(mood.get("valence", self.mood.valence))))
            self.mood.arousal = max(0.0, min(1.0, float(mood.get("arousal", self.mood.arousal))))
            if mood.get("label"):
                self.mood.label = str(mood["label"])
        if tick.get("feelings"):
            self.feelings = [str(x) for x in tick["feelings"]][:8]
        if tick.get("internal_thought"):
            self.internal_thought = str(tick["internal_thought"])[:2000]
        if tick.get("urges"):
            self.urges = [str(x) for x in tick["urges"]][:6]
        self.updated_at = time.time()

    def tick_interval_seconds(self, base: int) -> float:
        """Higher arousal → faster inner loop (more Neuro-like restlessness)."""
        factor = 0.45 + (0.55 * (1.0 - self.mood.arousal))
        return max(15.0, base * factor)
