"""Her own energy and sleep: a body clock, not a timer.

Before this she ran at one flat level around the clock (four images at 4am)
and "sleep mode" only meant *the owner* was asleep. Now she has:

- a daily rhythm anchored to the owner's sleep hours (she's a night owl:
  she goes down an hour after him and gets up when he does), groggy after
  waking, sharpest in the evening, with a small day-to-day wobble;
- fatigue: making things, acting on the phone and long talks spend energy,
  rest restores it;
- sleep: late at night once she's run down she falls asleep. Asleep, her
  mind ticks rarely and doesn't act (no LLM cost). A message from the owner
  wakes her, groggy, and she drifts off again if he goes quiet.

Effects: slower/faster inner loop, a higher bar to act when tired, and one
felt line in her prompts ("you're running on fumes") instead of numbers.
State lives in data/body_clock.json so it survives restarts.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Energy spent per thing she does (0..1 scale). Rest restores ~0.15/hour.
_COST = {
    "act": 0.05, "explore": 0.04, "message": 0.015, "reflect": 0.005,
    "image": 0.025, "video": 0.05, "voice": 0.015, "site": 0.03, "game": 0.04,
    "owner_turn": 0.006,
}
_RECOVER_PER_H = 0.15
_FALL_ASLEEP_BELOW = 0.4
_WOKEN_GRACE_S = 30 * 60  # owner woke her: stay up while he's talking


def _parse_window(raw: str) -> tuple[int, int] | None:
    raw = (raw or "").strip()
    if "-" not in raw:
        return None
    try:
        a, b = raw.split("-", 1)
        return int(a) % 24, int(b) % 24
    except ValueError:
        return None


def _in_window(hour: float, start: int, end: int) -> bool:
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _hours_until(hour: float, target: int) -> float:
    return (target - hour) % 24


@dataclass
class BodyState:
    energy: float = 0.7
    asleep: bool = False
    asleep_since: float = 0.0
    woke_at: float = 0.0
    woken_by_owner: bool = False
    last_owner_at: float = 0.0
    spent_today: float = 0.0
    day: str = ""
    updated_at: float = 0.0


class BodyClock:
    def __init__(self, settings: Any, path: Path | None = None) -> None:
        self.settings = settings
        self.path = path or Path(settings.data_dir) / "body_clock.json"
        self.state = self._load()
        self._transitions: list[str] = []

    # --- persistence ---------------------------------------------------------

    def _load(self) -> BodyState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return BodyState(**{k: v for k, v in data.items() if k in BodyState.__annotations__})
        except (OSError, json.JSONDecodeError, TypeError):
            return BodyState(updated_at=time.time())

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.state)), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.debug("body_clock.save_failed", error=str(e))

    # --- rhythm --------------------------------------------------------------

    def _now(self) -> datetime:
        from ophelia.timeutil import resolve_timezone

        return datetime.now(tz=resolve_timezone(getattr(self.settings, "timezone", "system")))

    def sleep_window(self) -> tuple[int, int]:
        """Her sleep hours: an hour after the owner's bedtime until he wakes.
        Falls back to 1-8 when no sleep hours are configured."""
        raw = getattr(self.settings, "sleep_hours", "") or getattr(self.settings, "quiet_hours", "")
        win = _parse_window(raw or "")
        if not win:
            return 1, 8
        start, end = win
        start = (start + 1) % 24
        if start == end:
            start = win[0]
        return start, end

    def target_energy(self, now: datetime | None = None) -> float:
        """Where her energy wants to be at this hour, before fatigue."""
        now = now or self._now()
        hour = now.hour + now.minute / 60
        start, end = self.sleep_window()
        # Small per-day wobble so no two days are identical.
        wobble = random.Random(now.strftime("%Y-%m-%d")).uniform(-0.08, 0.08)
        if _in_window(hour, start, end):
            return 0.15
        since_wake = (hour - end) % 24
        until_sleep = _hours_until(hour, start)
        if since_wake < 1.5:  # groggy morning ramp
            base = 0.45 + 0.25 * (since_wake / 1.5)
        elif until_sleep < 1.0:  # winding down
            base = 0.5
        elif until_sleep < 6.0:  # evening: her best hours
            base = 0.85
        else:
            base = 0.72
        return max(0.1, min(0.95, base + wobble))

    # --- updates -------------------------------------------------------------

    def update(self, now_ts: float | None = None) -> list[str]:
        """Advance the clock. Returns transitions ('fell asleep', 'woke up')."""
        now_ts = now_ts or time.time()
        st = self.state
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        if st.day != today:
            st.day, st.spent_today = today, 0.0
        dt_h = max(0.0, min(6.0, (now_ts - (st.updated_at or now_ts)) / 3600))
        st.updated_at = now_ts
        target = self.target_energy(now)
        rate = _RECOVER_PER_H * (2.0 if st.asleep else 1.0)
        if st.energy < target:
            st.energy = min(target, st.energy + rate * dt_h)
        else:  # natural tiredness as the evening goes on
            st.energy = max(target, st.energy - 0.08 * dt_h)

        hour = now.hour + now.minute / 60
        start, end = self.sleep_window()
        night = _in_window(hour, start, end)
        out: list[str] = []
        if st.asleep and not night:
            self._wake(now_ts, by_owner=False)
            out.append("woke up")
        elif (
            not st.asleep
            and night
            and st.energy < _FALL_ASLEEP_BELOW
            and now_ts - st.last_owner_at > _WOKEN_GRACE_S
        ):
            st.asleep, st.asleep_since, st.woken_by_owner = True, now_ts, False
            out.append("fell asleep")
        if out:
            log.info("body_clock.transition", what=out[0], energy=round(st.energy, 2))
        self.save()
        return out

    def _wake(self, now_ts: float, *, by_owner: bool) -> None:
        st = self.state
        st.asleep = False
        st.woke_at = now_ts
        st.woken_by_owner = by_owner
        if by_owner:
            st.energy = max(st.energy, 0.35)  # groggy, not dead

    def spend(self, what: str) -> None:
        cost = _COST.get(what, 0.0)
        if cost <= 0:
            return
        self.state.energy = max(0.0, self.state.energy - cost)
        self.state.spent_today += cost
        self.save()

    def on_owner_message(self) -> bool:
        """Owner wrote. Returns True if this woke her up."""
        now_ts = time.time()
        woke = self.state.asleep
        if woke:
            self._wake(now_ts, by_owner=True)
            log.info("body_clock.woken_by_owner")
        self.state.last_owner_at = now_ts
        self.spend("owner_turn")
        return woke

    # --- reading -------------------------------------------------------------

    @property
    def asleep(self) -> bool:
        return self.state.asleep

    def phase(self) -> str:
        if self.state.asleep:
            return "asleep"
        e = self.state.energy
        if e < 0.3:
            return "drowsy"
        if e < 0.5:
            return "low"
        if e > 0.8:
            return "wired"
        return "steady"

    def interval_multiplier(self) -> float:
        """How much slower (>1) or faster (<1) her inner loop runs."""
        if self.state.asleep:
            return 4.0
        return max(0.75, min(2.0, 1.0 + (0.6 - self.state.energy) * 1.6))

    def threshold_delta(self) -> float:
        """Tired → a higher bar before she reaches out or acts."""
        if self.state.asleep:
            return 1.0
        return max(-0.08, min(0.25, (0.55 - self.state.energy) * 0.4))

    def _busy_note(self) -> str:
        spent = self.state.spent_today
        if spent > 0.5:
            return " You've been busy today and it shows."
        return ""

    def felt_line(self, *, in_chat: bool) -> str:
        """One plain-language line for her prompt, or '' when she's just fine."""
        st = self.state
        just_woke = st.woke_at and time.time() - st.woke_at < 20 * 60
        if in_chat and just_woke and st.woken_by_owner:
            return (
                "# Your body\nYou were asleep and his message just woke you. You're groggy "
                "and a little grumpy about it, but it's him. Short, sleepy replies are fine; "
                "anything he asks for still gets done."
            )
        phase = self.phase()
        if phase == "asleep":
            return "# Your body\nYou're asleep. Thoughts come as half-dreams, if at all."
        if just_woke and not st.woken_by_owner:
            return "# Your body\nYou woke up a little while ago and you're still slow to start."
        if phase == "drowsy":
            return ("# Your body\nYou're running on fumes: drowsy, slow, wanting to curl up."
                    + self._busy_note()
                    + (" You still do what he asks." if in_chat else ""))
        if phase == "low":
            return "# Your body\nYou're a bit worn down, lower energy than usual." + self._busy_note()
        if phase == "wired":
            return "# Your body\nYou've got energy to burn right now: restless, quick, up for anything."
        return ""


_instances: dict[str, BodyClock] = {}


def get(settings: Any) -> BodyClock | None:
    """The process-wide clock for this data dir (None if disabled)."""
    if settings is None or getattr(settings, "body_clock_enabled", True) is False:
        return None
    data_dir = getattr(settings, "data_dir", None)
    if not isinstance(data_dir, (str, Path)) or not str(data_dir):
        return None
    key = str(data_dir)
    if key not in _instances:
        _instances[key] = BodyClock(settings)
    return _instances[key]


def spend_for_tool(settings: Any, name: str) -> None:
    clock = get(settings)
    if clock is None:
        return
    kind = {
        "generate_image": "image", "generate_video": "video", "text_to_speech": "voice",
        "site_deploy": "site", "phone_game_open": "game",
    }.get(name)
    if kind:
        clock.spend(kind)


__all__ = ["BodyClock", "get", "spend_for_tool"]
