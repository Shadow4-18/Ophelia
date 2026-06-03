"""Mobile game profiles and bounded play sessions."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ophelia.config import OPHELIA_HOME

if TYPE_CHECKING:
    from ophelia.android.shizuku import AndroidBody

GENRE_HINTS: dict[str, str] = {
    "idle": "Slow pace is fine. Collect rewards, dismiss popups — one action per turn.",
    "puzzle": "One logical move per turn. State your plan in one sentence.",
    "menu": "Menus and dailies only. Do not spend premium currency unless notes say so.",
    "gacha": "Menu navigation only; no summons/pulls unless user notes allow.",
    "rhythm": "High reflex — only lobby/menus here; do not attempt live gameplay.",
}

GAME_VISION_PREFIX = """You are Ophelia playing a mobile game on her phone (stream commentary mindset).
Read the screen for THIS game. Suggest exactly ONE next action: tap (x,y), swipe, wait, or back.
If unsure, say wait and what to look for next turn."""


@dataclass
class GameProfile:
    id: str
    package: str
    name: str
    genre: str = "puzzle"
    notes: str = ""
    max_session_minutes: float = 15.0
    enabled: bool = True

    def vision_question(self, intent: str = "") -> str:
        hint = GENRE_HINTS.get(self.genre.lower(), GENRE_HINTS["puzzle"])
        parts = [
            GAME_VISION_PREFIX,
            f"Game: {self.name} (id={self.id}, genre={self.genre})",
            f"Package: {self.package}",
        ]
        if self.notes.strip():
            parts.append(f"Play rules: {self.notes.strip()}")
        parts.append(f"Genre guidance: {hint}")
        if intent.strip():
            parts.append(f"Intent this turn: {intent.strip()}")
        else:
            parts.append("What is the single best next action on this screen?")
        return "\n".join(parts)


@dataclass
class GameSession:
    game_id: str
    started_at: float
    ends_at: float
    turns: int = 0

    def expired(self) -> bool:
        return time.time() >= self.ends_at

    def minutes_left(self) -> float:
        return max(0.0, (self.ends_at - time.time()) / 60.0)


@dataclass
class GameStore:
    games: list[GameProfile] = field(default_factory=list)
    path: Path = field(default_factory=lambda: OPHELIA_HOME / "games.yaml")
    session_path: Path = field(
        default_factory=lambda: OPHELIA_HOME / "data" / "game_session.json"
    )
    default_session_minutes: float = 15.0
    max_turns: int = 40

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        default_session_minutes: float = 15.0,
        max_turns: int = 40,
    ) -> GameStore:
        p = path or OPHELIA_HOME / "games.yaml"
        store = cls(
            path=p,
            default_session_minutes=default_session_minutes,
            max_turns=max_turns,
        )
        if not p.is_file():
            return store
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            return store
        raw = data.get("games") or []
        if not isinstance(raw, list):
            return store
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            store.games.append(
                GameProfile(
                    id=str(item["id"]),
                    package=str(item.get("package") or ""),
                    name=str(item.get("name") or item["id"]),
                    genre=str(item.get("genre") or "puzzle"),
                    notes=str(item.get("notes") or ""),
                    max_session_minutes=float(
                        item.get("max_session_minutes", default_session_minutes)
                    ),
                    enabled=bool(item.get("enabled", True)),
                )
            )
        return store

    def get(self, game_id: str) -> GameProfile | None:
        for g in self.games:
            if g.id == game_id and g.enabled:
                return g
        return None

    def list_enabled(self) -> list[GameProfile]:
        return [g for g in self.games if g.enabled and g.package]

    def _read_session(self) -> GameSession | None:
        if not self.session_path.is_file():
            return None
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not data.get("game_id"):
            return None
        return GameSession(
            game_id=str(data["game_id"]),
            started_at=float(data.get("started_at") or 0),
            ends_at=float(data.get("ends_at") or 0),
            turns=int(data.get("turns") or 0),
        )

    def _write_session(self, session: GameSession | None) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        if session is None:
            if self.session_path.is_file():
                self.session_path.unlink()
            return
        self.session_path.write_text(
            json.dumps(
                {
                    "game_id": session.game_id,
                    "started_at": session.started_at,
                    "ends_at": session.ends_at,
                    "turns": session.turns,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def session_active(self) -> bool:
        s = self._read_session()
        if not s or s.expired():
            if s and s.expired():
                self.stop_session()
            return False
        if s.turns >= self.max_turns:
            self.stop_session()
            return False
        return True

    def active_session(self) -> GameSession | None:
        s = self._read_session()
        if not s or s.expired() or s.turns >= self.max_turns:
            return None
        return s

    def active_profile(self) -> GameProfile | None:
        s = self.active_session()
        if not s:
            return None
        return self.get(s.game_id)

    def minutes_left(self) -> float:
        s = self.active_session()
        return s.minutes_left() if s else 0.0

    def record_turn(self) -> bool:
        """Increment turn count. Returns False if session should end."""
        s = self.active_session()
        if not s:
            return False
        s.turns += 1
        self._write_session(s)
        if s.turns >= self.max_turns or s.expired():
            self.stop_session()
            return False
        return True

    async def start_session(
        self,
        game_id: str,
        minutes: float | None = None,
        android: AndroidBody | None = None,
    ) -> str:
        profile = self.get(game_id)
        if not profile:
            ids = ", ".join(g.id for g in self.list_enabled()) or "(none configured)"
            return f"Unknown or disabled game '{game_id}'. Configured: {ids}"
        if not profile.package:
            return f"Game '{game_id}' has no package — edit ~/.ophelia/games.yaml"

        mins = minutes if minutes is not None else profile.max_session_minutes
        mins = min(mins, profile.max_session_minutes * 2)
        now = time.time()
        session = GameSession(
            game_id=profile.id,
            started_at=now,
            ends_at=now + mins * 60,
            turns=0,
        )
        self._write_session(session)

        launch = ""
        if android and android.mode != "termux_only":
            launch = await android.open_app(profile.package)

        return (
            f"Game session: {profile.name} ({profile.id}) for {mins:.0f} min. "
            f"Genre={profile.genre}. Launch: {launch or 'no Shizuku — open app manually'}. "
            f"Use phone_game_look then phone_tap / phone_swipe."
        )

    def stop_session(self) -> str:
        s = self._read_session()
        if not s:
            return "No active game session."
        profile = self.get(s.game_id)
        name = profile.name if profile else s.game_id
        self._write_session(None)
        return f"Stopped game session ({name}, {s.turns} vision turns)."

    def format_list(self) -> str:
        if not self.games:
            return (
                "No games in ~/.ophelia/games.yaml — copy games.example.yaml and edit packages."
            )
        lines = ["Configured games:"]
        for g in self.games:
            if not g.enabled:
                continue
            lines.append(
                f"• {g.id}: {g.name} [{g.genre}] pkg={g.package or 'MISSING'}"
            )
        s = self.active_session()
        if s:
            p = self.get(s.game_id)
            lines.append(
                f"\nACTIVE: {p.name if p else s.game_id} — "
                f"{s.minutes_left():.0f}m left, {s.turns}/{self.max_turns} turns"
            )
        return "\n".join(lines)

    def to_context_block(self) -> str:
        enabled = self.list_enabled()
        if not enabled and not self.session_active():
            return ""
        lines = ["# Mobile games (phone_game_look, phone_swipe, phone_tap):"]
        for g in enabled[:8]:
            lines.append(f"- {g.id}: {g.name} ({g.genre}) — {g.notes[:80]}")
        if self.session_active():
            p = self.active_profile()
            if p:
                lines.append(
                    f"\nACTIVE SESSION: {p.name} — {self.minutes_left():.0f}m remaining. "
                    "Prefer phone_game_look over generic phone_see_screen."
                )
        return "\n".join(lines)


def game_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "phone_game_look",
                "description": (
                    "Vision tuned for the active or named game in games.yaml. "
                    "Use before tap/swipe during play."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "game_id": {
                            "type": "string",
                            "description": "From games.yaml; uses active session if omitted",
                        },
                        "intent": {
                            "type": "string",
                            "description": "What you are trying to do this turn",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "phone_game_open",
                "description": "Launch a configured game and start a bounded play session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "game_id": {"type": "string"},
                        "minutes": {
                            "type": "number",
                            "description": "Session length cap (default from profile)",
                        },
                    },
                    "required": ["game_id"],
                },
            },
        },
    ]
