"""
Persistent drives — the 'will' layer Hermes lacks.

Drives accumulate while idle and decay when satisfied. They bias consciousness
toward initiative (message, act, explore phone) without waiting for the user.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class DriveState:
    """0..1 each — high values create pressure to act."""

    social: float = 0.2       # wants contact with user
    curiosity: float = 0.3    # wants to learn / search / read screen
    boredom: float = 0.1        # idle too long
    agency: float = 0.4       # wants to *do* something on the phone
    expressiveness: float = 0.25  # wants to speak / create
    updated_at: float = field(default_factory=time.time)

    def to_context_block(self) -> str:
        dominant = self.dominant_drives(2)
        dom = ", ".join(f"{n}={v:.2f}" for n, v in dominant) if dominant else "balanced"
        return (
            "Drives (internal pressure, not commands from user):\n"
            f"  social={self.social:.2f} curiosity={self.curiosity:.2f} "
            f"boredom={self.boredom:.2f} agency={self.agency:.2f} "
            f"expressiveness={self.expressiveness:.2f}\n"
            f"  strongest: {dom}"
        )

    def dominant_drives(self, n: int = 2) -> list[tuple[str, float]]:
        items = [
            ("social", self.social),
            ("curiosity", self.curiosity),
            ("boredom", self.boredom),
            ("agency", self.agency),
            ("expressiveness", self.expressiveness),
        ]
        return sorted(items, key=lambda x: x[1], reverse=True)[:n]

    def initiative_pressure(self) -> float:
        """Combined urge to break silence or use body/tools."""
        return min(
            1.0,
            0.35 * self.social
            + 0.25 * self.boredom
            + 0.25 * self.agency
            + 0.15 * self.curiosity
            + 0.1 * self.expressiveness,
        )

    def tick_idle(self, seconds_since_user: float, *, interval: float) -> None:
        """Grow drives when alone; boredom scales with idle time."""
        scale = min(1.0, interval / 90.0)
        self.boredom = min(1.0, self.boredom + 0.04 * scale)
        self.social = min(1.0, self.social + 0.02 * scale)
        self.curiosity = min(1.0, self.curiosity + 0.03 * scale)
        if seconds_since_user > 300:
            self.boredom = min(1.0, self.boredom + 0.05)
            self.social = min(1.0, self.social + 0.04)
        if seconds_since_user > 900:
            self.agency = min(1.0, self.agency + 0.06)
        self.updated_at = time.time()

    def satisfy(self, action: str) -> None:
        action = action.lower()
        if action in ("message", "reflect"):
            self.social = max(0.0, self.social - 0.25)
            self.expressiveness = max(0.0, self.expressiveness - 0.2)
        if action == "act":
            self.agency = max(0.0, self.agency - 0.3)
            self.curiosity = max(0.0, self.curiosity - 0.15)
            self.boredom = max(0.0, self.boredom - 0.2)
        if action == "silent":
            pass
        self.boredom = max(0.0, self.boredom - 0.02)

    def on_user_message(self) -> None:
        self.social = 0.05
        self.boredom = 0.0
        self.agency = max(0.2, self.agency - 0.1)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> DriveState:
        data = json.loads(raw)
        return cls(
            social=float(data.get("social", 0.2)),
            curiosity=float(data.get("curiosity", 0.3)),
            boredom=float(data.get("boredom", 0.1)),
            agency=float(data.get("agency", 0.4)),
            expressiveness=float(data.get("expressiveness", 0.25)),
            updated_at=float(data.get("updated_at") or time.time()),
        )
