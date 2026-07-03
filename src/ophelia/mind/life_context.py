"""Authoritative time, schedule, and owner-presence context for Ophelia.

The phone is stationary at home — we infer where the *owner* is from work
schedule, Telegram activity, and optional home WiFi SSID. Injected into every
system prompt so she stops guessing dates/times/locations wrong.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from ophelia.config import Settings
from ophelia.core.signals import Signals

log = structlog.get_logger()

_DAY_MAP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


class LifeContext:
    def __init__(self, settings: Settings, signals: Signals) -> None:
        self.settings = settings
        self.signals = signals
        self._wifi_ssid: str | None = None
        self._wifi_checked_at: float = 0.0
        self._owner_state: str = "unknown"
        # Tier B #6: optional learned-schedule learner. When set, sharpens
        # owner-state inference with observed Telegram activity patterns.
        self.schedule_learner = None
        # Tier B #7: optional presence signals (BT / router / last-seen).
        self.presence_signals = None

    def tz(self) -> ZoneInfo:
        raw = (self.settings.timezone or "UTC").strip()
        try:
            return ZoneInfo(raw)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def now(self) -> datetime:
        return datetime.now(tz=self.tz())

    async def refresh(self) -> None:
        """Poll WiFi SSID + learned schedule + presence signals occasionally."""
        # Tier B #6 / #7: refresh learned schedule and presence signals at the
        # same cadence as WiFi. Pre-computes sync-readable flags so the sync
        # infer_owner_state() / to_context_block() don't need to await.
        if time.time() - self._wifi_checked_at < 120:
            return
        self._wifi_checked_at = time.time()
        bin_path = shutil.which("termux-wifi-connectioninfo")
        if bin_path:
            try:
                proc = await asyncio.create_subprocess_exec(
                    bin_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                text = out.decode("utf-8", errors="replace")
                m = re.search(r'"ssid"\s*:\s*"([^"]+)"', text)
                if m:
                    self._wifi_ssid = m.group(1)
            except Exception as e:
                log.debug("life_context.wifi_skip", error=str(e))

        if self.schedule_learner is not None:
            try:
                self._last_learned_quiet = await self.schedule_learner.is_likely_quiet_now()
                self._learned_summary = await self.schedule_learner.learned_summary()
            except Exception as e:
                log.debug("life_context.schedule_skip", error=str(e))

        if self.presence_signals is not None:
            try:
                await self.presence_signals.refresh()
                self._last_presence = self.presence_signals.summary()
            except Exception as e:
                log.debug("life_context.presence_skip", error=str(e))

    def _parse_work_days(self) -> set[int]:
        raw = (self.settings.work_days or "").strip()
        if not raw:
            return set()
        days: set[int] = set()
        for part in re.split(r"[,;\s]+", raw.lower()):
            part = part.strip()
            if part in _DAY_MAP:
                days.add(_DAY_MAP[part])
        return days

    def _in_work_hours(self, dt: datetime) -> bool:
        raw = (self.settings.work_hours or "").strip()
        if not raw or "-" not in raw:
            return False
        try:
            start_s, end_s = raw.split("-", 1)
            start_h = int(start_s.strip())
            end_h = int(end_s.strip())
        except ValueError:
            return False
        h = dt.hour
        if start_h <= end_h:
            return start_h <= h < end_h
        # Crosses midnight (e.g. 18-06 warehouse shift)
        return h >= start_h or h < end_h

    def _in_sleep_hours(self, dt: datetime) -> bool:
        raw = (self.settings.sleep_hours or self.settings.quiet_hours or "").strip()
        if not raw or "-" not in raw:
            return False
        try:
            start_s, end_s = raw.split("-", 1)
            start_h = int(start_s.strip())
            end_h = int(end_s.strip())
        except ValueError:
            return False
        h = dt.hour
        if start_h <= end_h:
            return start_h <= h < end_h
        return h >= start_h or h < end_h

    def _telegram_silent_hours(self) -> float:
        """Hours since owner last messaged."""
        last = self.signals.last_user_message_at
        if not last:
            return 999.0
        return (time.time() - last) / 3600.0

    def infer_owner_state(self) -> str:
        """Where is the owner likely to be / what are they doing?"""
        dt = self.now()
        work_days = self._parse_work_days()
        on_work_day = dt.weekday() in work_days if work_days else False
        in_work_hours = self._in_work_hours(dt)
        silent_h = self._telegram_silent_hours()

        # Tier B #6: learned-quiet flag is pre-computed in refresh() (the
        # learner is async; this method is sync). When true and the owner has
        # been silent, sharpen "away" even if the static schedule doesn't.
        learned_quiet = bool(getattr(self, "_last_learned_quiet", False))

        if self._in_sleep_hours(dt):
            if silent_h > 2:
                return "asleep"
            return "likely_asleep"

        if on_work_day and in_work_hours:
            if silent_h >= 1.5:
                return "at_work"
            if silent_h < 0.25:
                return "at_work_but_messaging"
            return "probably_at_work"

        if learned_quiet and silent_h > 1.5:
            return "likely_away_quiet_hour"

        if silent_h < 0.08:
            return "active_here"
        if silent_h < 1.0:
            return "around"
        if silent_h < 4.0:
            return "away_or_busy"
        return "long_absence"

    def is_sleep_mode(self) -> bool:
        if not self.settings.sleep_mode_enabled:
            return False
        state = self.infer_owner_state()
        return state in ("asleep", "likely_asleep")

    def is_owner_at_work(self) -> bool:
        state = self.infer_owner_state()
        return state in ("at_work", "probably_at_work", "at_work_but_messaging")

    def should_minimize_outreach(self) -> bool:
        return self.is_sleep_mode() or self.is_owner_at_work()

    def commentary_allowed(self, *, last_commentary_at: float) -> bool:
        if not self.settings.ambient_commentary_enabled:
            return False
        if self.signals.autonomy_paused or self.is_sleep_mode():
            return False
        if self.is_owner_at_work() and self._telegram_silent_hours() > 0.5:
            return False
        mins = self.settings.ambient_commentary_minutes
        if time.time() - last_commentary_at < mins * 60:
            return False
        if self._telegram_silent_hours() < 0.05:
            return False  # owner mid-conversation
        return True

    def voice_speed(self, psyche=None) -> float:
        """TTS speed multiplier from owner-state + mood.

        Owner state sets the base (sleep = slow, etc). Mood composes on top
        (Tier A #5): low valence / low arousal slow further; high arousal
        speeds up. Keeps her voice and mood as one continuous signal.
        """
        base = self.settings.kokoro_tts_speed
        if self.is_sleep_mode():
            base = min(base, 0.88)
        else:
            state = self.infer_owner_state()
            if state == "active_here":
                pass
            elif state in ("around", "at_work_but_messaging"):
                base = base * 0.96
            else:
                base = base * 0.92
        if psyche is not None:
            from ophelia.mind.mood_behavior import mood_knobs

            base = mood_knobs(psyche).apply_speed(base)
        return base

    def consciousness_interval_multiplier(self) -> float:
        if self.is_sleep_mode():
            return 2.5
        if self.is_owner_at_work():
            return 1.8
        return 1.0

    def to_context_block(self) -> str:
        dt = self.now()
        state = self.infer_owner_state()
        sleep = self.is_sleep_mode()
        silent_h = self._telegram_silent_hours()
        body_loc = "home (stationary phone — Ophelia's body does not move)"
        wifi = self._wifi_ssid or "unknown"
        home_ssid = (self.settings.home_wifi_ssid or "").strip()
        wifi_note = ""
        if home_ssid and wifi != "unknown":
            wifi_note = (
                f" WiFi SSID `{wifi}` "
                + ("matches home." if wifi == home_ssid else "≠ configured home SSID.")
            )

        work_note = ""
        if self._parse_work_days():
            days = self.settings.work_days
            hours = self.settings.work_hours or "?"
            work_note = f" Work schedule: {days} {hours}."

        learned_note = getattr(self, "_learned_summary", "") or ""
        presence_note = getattr(self, "_last_presence", "") or ""

        return (
            f"# Current context (AUTHORITATIVE — trust this, not vague memory)\n"
            f"- Now: {dt.strftime('%A, %B %d, %Y — %I:%M %p %Z')} ({self.settings.timezone or 'UTC'})\n"
            f"- Ophelia body location: {body_loc}{wifi_note}\n"
            f"- Owner likely state: **{state.replace('_', ' ')}** "
            f"(last owner message {silent_h:.1f}h ago)\n"
            f"- Sleep mode: {'ON — softer, slower, dreamier; minimize outreach' if sleep else 'off'}\n"
            f"- At work (inferred): {'yes — stay quiet unless messaged' if self.is_owner_at_work() else 'no'}"
            f"{work_note}\n"
            + (f"{learned_note}\n" if learned_note else "")
            + (f"{presence_note}\n" if presence_note else "")
            + "Never invent the date, time, or where the owner is. Use the lines above."
        )
