"""Persistent goals Ophelia owns — not Hermes cron tasks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ophelia.config import OPHELIA_HOME


@dataclass
class Goal:
    id: str
    description: str
    priority: float = 0.5
    cadence_hours: float = 24.0
    enabled: bool = True
    last_done_at: float = 0.0
    tags: list[str] = field(default_factory=list)

    def due(self) -> bool:
        if not self.enabled:
            return False
        elapsed = time.time() - self.last_done_at
        return elapsed >= self.cadence_hours * 3600

    def mark_done(self) -> None:
        self.last_done_at = time.time()


@dataclass
class GoalStore:
    goals: list[Goal] = field(default_factory=list)
    path: Path = field(default_factory=lambda: OPHELIA_HOME / "goals.yaml")

    @classmethod
    def load(cls, path: Path | None = None) -> GoalStore:
        p = path or OPHELIA_HOME / "goals.yaml"
        store = cls(path=p)
        if not p.is_file():
            store.goals = cls.default_goals()
            return store
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            store.goals = cls.default_goals()
            return store
        raw = data.get("goals") or data
        if not isinstance(raw, list):
            return store
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            store.goals.append(
                Goal(
                    id=str(item["id"]),
                    description=str(item.get("description") or item["id"]),
                    priority=float(item.get("priority", 0.5)),
                    cadence_hours=float(item.get("cadence_hours", 24)),
                    enabled=bool(item.get("enabled", True)),
                    last_done_at=float(item.get("last_done_at") or 0),
                    tags=[str(t) for t in (item.get("tags") or [])],
                )
            )
        return store

    @staticmethod
    def default_goals() -> list[Goal]:
        return [
            Goal(
                id="check-in",
                description="Genuine check-in with user if they've been quiet — not small talk",
                priority=0.7,
                cadence_hours=8,
                tags=["social"],
            ),
            Goal(
                id="explore-screen",
                description="Look at the phone screen and notice anything interesting or urgent",
                priority=0.6,
                cadence_hours=4,
                tags=["curiosity", "android"],
            ),
            Goal(
                id="self-reflect",
                description="Reflect on recent conversations and update inner understanding",
                priority=0.4,
                cadence_hours=12,
                tags=["reflect"],
            ),
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "goals": [
                {
                    "id": g.id,
                    "description": g.description,
                    "priority": g.priority,
                    "cadence_hours": g.cadence_hours,
                    "enabled": g.enabled,
                    "last_done_at": g.last_done_at,
                    "tags": g.tags,
                }
                for g in self.goals
            ]
        }
        self.path.write_text(yaml.dump(payload, default_flow_style=False), encoding="utf-8")

    def pick_for_tick(self, drives_tags: list[str] | None = None) -> Goal | None:
        due = [g for g in self.goals if g.due()]
        if not due:
            return None
        if drives_tags:
            tagged = [
                g
                for g in due
                if any(t in drives_tags for t in g.tags) or not g.tags
            ]
            if tagged:
                due = tagged
        return max(due, key=lambda g: g.priority)

    def to_context_block(self) -> str:
        if not self.goals:
            return ""
        lines = ["# Your goals (you chose to maintain these):"]
        for g in self.goals:
            if not g.enabled:
                continue
            status = "DUE" if g.due() else "ok"
            lines.append(f"- [{status}] {g.id}: {g.description} (every {g.cadence_hours}h)")
        return "\n".join(lines)
