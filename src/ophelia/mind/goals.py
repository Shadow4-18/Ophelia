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

    def get(self, goal_id: str) -> Goal | None:
        gid = goal_id.strip().lower()
        for g in self.goals:
            if g.id.lower() == gid:
                return g
        return None

    def add(
        self,
        description: str,
        *,
        id: str | None = None,
        priority: float = 0.5,
        cadence_hours: float = 24.0,
        tags: list[str] | None = None,
        enabled: bool = True,
    ) -> Goal:
        """Self-author a new goal. Auto-generates a stable id if none given."""
        import re

        if id:
            gid = re.sub(r"[^a-z0-9_-]+", "-", id.strip().lower()).strip("-") or f"goal-{int(time.time())}"
        else:
            slug = re.sub(r"[^a-z0-9_-]+", "-", description.strip().lower())[:32].strip("-")
            gid = slug or f"goal-{int(time.time())}"
        # Ensure uniqueness.
        existing = {g.id.lower() for g in self.goals}
        base = gid
        n = 2
        while gid.lower() in existing:
            gid = f"{base}-{n}"
            n += 1
        goal = Goal(
            id=gid,
            description=description.strip(),
            priority=max(0.0, min(1.0, float(priority))),
            cadence_hours=max(0.1, float(cadence_hours)),
            enabled=bool(enabled),
            tags=[str(t) for t in (tags or [])],
        )
        self.goals.append(goal)
        self.save()
        return goal

    def update(
        self,
        goal_id: str,
        *,
        description: str | None = None,
        priority: float | None = None,
        cadence_hours: float | None = None,
        enabled: bool | None = None,
        add_tags: list[str] | None = None,
    ) -> Goal | None:
        g = self.get(goal_id)
        if not g:
            return None
        if description is not None and description.strip():
            g.description = description.strip()
        if priority is not None:
            g.priority = max(0.0, min(1.0, float(priority)))
        if cadence_hours is not None:
            g.cadence_hours = max(0.1, float(cadence_hours))
        if enabled is not None:
            g.enabled = bool(enabled)
        if add_tags:
            for t in add_tags:
                if t not in g.tags:
                    g.tags.append(t)
        self.save()
        return g

    def remove(self, goal_id: str) -> bool:
        g = self.get(goal_id)
        if not g:
            return False
        self.goals.remove(g)
        self.save()
        return True

    def to_context_block(self) -> str:
        if not self.goals:
            return ""
        lines = ["# Your goals (you own these — add, revise, or retire them with goal tools):"]
        for g in self.goals:
            if not g.enabled:
                continue
            status = "DUE" if g.due() else "ok"
            tags = f" [{','.join(g.tags)}]" if g.tags else ""
            lines.append(
                f"- [{status}] {g.id}: {g.description} "
                f"(every {g.cadence_hours}h, prio {g.priority:.2f}){tags}"
            )
        return "\n".join(lines)
