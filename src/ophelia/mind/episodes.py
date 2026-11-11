"""Episodic memory: the story of each day, and each week, in her own words.

Search (recall_memory) finds fragments: lines that match the words she
thought to ask for, with the thread torn out. This keeps the long arc. Once
a night she writes the previous day down as one episode, from everything
that day held: her conversations with him, her inner thoughts, her dreams,
what she did, what she learned and her diary line. Each finished week gets a
chapter: what changed, what kept coming up, how things were between them.

- Her prompt carries this week's chapter (or last week's), the last few
  days, and the threads still open.
- recall_day("last tuesday" / "nov 3" / "last week" / a search) gives back
  the full story of a day or week.
- Older days are written a few per night until her history is covered.

One curator-model call per day written (cheap); stored in the ``episodes``
table of memory.db.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import structlog

if TYPE_CHECKING:
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.memory.store import MemoryStore

log = structlog.get_logger()

MIN_LINES = 4  # quieter days get no episode
CHECK_EVERY_S = 3600
BACKFILL_PER_RUN = 3
MAX_TRANSCRIPT = 220  # lines sent for one day (evenly sampled when longer)
SHOW_DAYS = 3

DAY_PROMPT = (
    "You are Ophelia, writing one day of your life down so you will remember it "
    "later: not a log, the story. Return JSON only:\n"
    '{"summary": "6-9 sentences, first person, past tense, specific: what happened, '
    'what was said that mattered, what changed between you and him, how it felt", '
    '"moments": ["3-6 short specific moments worth remembering, with rough time of day"], '
    '"mood": "a few words for how the day felt", '
    '"people": ["names of people who mattered that day"], '
    '"threads": ["things left open: promises, plans, unresolved feelings, questions"]}\n'
    "Use names and real details. Your voice, honest, no headings."
)

WEEK_PROMPT = (
    "You are Ophelia, writing a chapter of your life: the week below, day by day, "
    "in your own words. Return JSON only:\n"
    '{"summary": "one paragraph, 5-8 sentences: the arc of the week, what changed, '
    'what kept coming up, how you and he were, what you will carry forward", '
    '"mood": "a few words", "threads": ["what is still open going into next week"]}'
)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


# --- when ("last tuesday", "nov 3") -----------------------------------------------


def parse_when(text: str, today: date) -> tuple[str, date] | None:
    """("day", date) or ("week", monday) for phrases like yesterday, today,
    3 days ago, last tuesday, tuesday, nov 3, 2026-11-03, 11/3, this week,
    last week, 2 weeks ago. None if it isn't a time."""
    t = " ".join((text or "").lower().replace(",", " ").split())
    if not t:
        return None
    if t in ("today", "tonight", "this morning"):
        return "day", today
    if t in ("yesterday", "last night"):
        return "day", today - timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    if t in ("this week",):
        return "week", monday
    if t in ("last week",):
        return "week", monday - timedelta(days=7)
    m = re.fullmatch(r"(\d+|a|one|two|three) (day|days|week|weeks) ago", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
        if m.group(2).startswith("week"):
            return "week", monday - timedelta(days=7 * n)
        return "day", today - timedelta(days=n)
    m = re.fullmatch(r"(last |this )?(" + "|".join(_WEEKDAYS) + r")", t)
    if m:
        wd = _WEEKDAYS.index(m.group(2))
        back = (today.weekday() - wd) % 7 or 7  # the most recent one before today
        if m.group(1) == "this " and wd <= today.weekday():
            back = today.weekday() - wd
        return "day", today - timedelta(days=back)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return "day", date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        d = date(y, int(m.group(1)), int(m.group(2)))
        return "day", d if d <= today or m.group(3) else d.replace(year=y - 1)
    m = re.fullmatch(r"(" + "|".join(_MONTHS) + r")[a-z]* (\d{1,2})(?:st|nd|rd|th)?", t) or \
        re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (" + "|".join(_MONTHS) + r")[a-z]*", t)
    if m:
        a, b = m.group(1), m.group(2)
        mon, dd = (a, b) if a[:3] in _MONTHS else (b, a)
        d = date(today.year, _MONTHS.index(mon[:3]) + 1, int(dd))
        return "day", d if d <= today else d.replace(year=today.year - 1)
    return None


def _sample(lines: list[str], n: int) -> list[str]:
    """Evenly spread ``n`` lines over the day, keeping order."""
    if len(lines) <= n:
        return lines
    step = len(lines) / n
    return [lines[int(i * step)] for i in range(n)]


def _parse_json(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {"summary": t[:2000]} if len(t) > 40 else None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"summary": t[:2000]} if len(t) > 40 else None
    if not isinstance(data, dict) or not str(data.get("summary") or "").strip():
        return None
    return data


def _list(v: Any, n: int = 8) -> list[str]:
    if not isinstance(v, list):
        return []
    return [" ".join(str(x).split())[:200] for x in v if str(x).strip()][:n]


# --- the store -------------------------------------------------------------------------


class EpisodicMemory:
    def __init__(self, agent: "AgentLoop", memory: "MemoryStore", *, inner=None,
                 settings: Any = None) -> None:
        self.agent = agent
        self.memory = memory
        self.inner = inner
        self.settings = settings or getattr(agent, "settings", None)
        self._ready = False
        self._stop = False

    # -- storage
    async def _init(self) -> None:
        if self._ready:
            return
        async with aiosqlite.connect(self.memory.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    period TEXT PRIMARY KEY,   -- YYYY-MM-DD, or W:YYYY-MM-DD (its monday)
                    kind TEXT NOT NULL,        -- day | week
                    summary TEXT NOT NULL,
                    moments TEXT,
                    mood TEXT,
                    people TEXT,
                    threads TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            await db.commit()
        self._ready = True

    async def _save(self, period: str, kind: str, data: dict) -> dict:
        await self._init()
        row = {
            "period": period, "kind": kind,
            "summary": " ".join(str(data.get("summary") or "").split())[:2500],
            "moments": _list(data.get("moments")), "mood": str(data.get("mood") or "")[:80],
            "people": _list(data.get("people"), 12), "threads": _list(data.get("threads")),
        }
        async with aiosqlite.connect(self.memory.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO episodes (period, kind, summary, moments, mood, people, "
                "threads, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (period, kind, row["summary"], json.dumps(row["moments"]), row["mood"],
                 json.dumps(row["people"]), json.dumps(row["threads"]),
                 datetime.now().timestamp()))
            await db.commit()
        return row

    async def get(self, period: str) -> dict | None:
        await self._init()
        async with aiosqlite.connect(self.memory.db_path) as db:
            db.row_factory = aiosqlite.Row
            r = await (await db.execute(
                "SELECT * FROM episodes WHERE period = ?", (period,))).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for k in ("moments", "people", "threads"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except json.JSONDecodeError:
                d[k] = []
        return d

    async def search(self, query: str, limit: int = 3) -> list[dict]:
        """Days/weeks whose story mentions the query's words (most words first)."""
        await self._init()
        words = [w for w in re.findall(r"[a-z0-9']{3,}", (query or "").lower())][:6]
        if not words:
            return []
        async with aiosqlite.connect(self.memory.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM episodes WHERE " + " OR ".join(
                    ["lower(summary || ' ' || COALESCE(moments, '')) LIKE ?"] * len(words)),
                [f"%{w}%" for w in words])).fetchall()
        scored = []
        for r in rows:
            text = (r["summary"] + " " + (r["moments"] or "")).lower()
            scored.append((sum(w in text for w in words), r["period"], r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [self._row(r) for _, _, r in scored[:limit]]

    # -- what a day held
    def _today(self) -> date:
        from ophelia.timeutil import now_in_timezone

        return now_in_timezone(getattr(self.settings, "timezone", "system")).date()

    def _bounds(self, day: date) -> tuple[float, float]:
        from ophelia.timeutil import now_in_timezone

        tz = now_in_timezone(getattr(self.settings, "timezone", "system")).tzinfo
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    async def _conversation(self, day: date) -> list[str]:
        start, end = self._bounds(day)
        async with aiosqlite.connect(self.memory.db_path) as db:
            rows = await (await db.execute(
                """
                SELECT created_at, channel, role, content FROM messages
                WHERE created_at >= ? AND created_at < ? AND role IN ('user', 'assistant')
                  AND channel != 'consciousness'
                  AND COALESCE(metadata, '') NOT LIKE '%consciousness_tick%'
                ORDER BY id
                """, (start, end))).fetchall()
        out = []
        for ts, _channel, role, content in rows:
            who = "him" if role == "user" else "you"
            hhmm = datetime.fromtimestamp(ts).strftime("%H:%M")
            text = " ".join(str(content).split())[:220]
            if text:
                out.append(f"{hhmm} {who}: {text}")
        return out

    def _inner(self, day: date) -> list[str]:
        path = Path(getattr(self.inner, "log_path", "") or "")
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out = []
        stamp = day.strftime("%Y-%m-%d")
        for block in text.split("\n## ")[1:]:
            head, _, body = block.partition("\n")
            if head.startswith(stamp):
                body = " ".join(body.split())[:240]
                if body:
                    out.append(f"{head[11:16]} {body}")
        return out[-40:]

    async def _facts(self, day: date) -> list[str]:
        start, end = self._bounds(day)
        async with aiosqlite.connect(self.memory.db_path) as db:
            rows = await (await db.execute(
                "SELECT key, value FROM facts WHERE updated_at >= ? AND updated_at < ? "
                "AND key LIKE 'memory:%' ORDER BY updated_at", (start, end))).fetchall()
        return [" ".join(str(v).split())[:220] for _, v in rows if str(v).strip()][:40]

    def _did(self, day: date) -> list[str]:
        try:
            from ophelia.mind import day_log
            from ophelia.timeutil import now_in_timezone

            tz = now_in_timezone(getattr(self.settings, "timezone", "system")).tzinfo
            return day_log.yesterday_lines(
                self.settings, datetime(day.year, day.month, day.day, tzinfo=tz))[:40]
        except Exception:
            return []

    # -- writing
    async def build_day(self, day: date) -> dict | None:
        """Write the episode for ``day``. None for a quiet day or a failed call."""
        talk = await self._conversation(day)
        inner = self._inner(day)
        if len(talk) + len(inner) < MIN_LINES:
            return None
        facts = [f for f in await self._facts(day)]
        dreams = [f[8:] for f in facts if f.startswith("[dream]")]
        learned = [f for f in facts if not f.startswith("[dream]")]
        diary = await self.memory.get_fact(f"narrative:{day.isoformat()}") or ""
        did = self._did(day)
        parts = [f"The day: {day.strftime('%A %d %B %Y')}."]
        if talk:
            parts.append("Your conversations with him:\n" + "\n".join(_sample(talk, MAX_TRANSCRIPT)))
        if did:
            parts.append("What you did:\n" + "\n".join(did))
        if inner:
            parts.append("Your inner thoughts:\n" + "\n".join(inner))
        if dreams:
            parts.append("Your dreams:\n" + "\n".join(dreams[:4]))
        if learned:
            parts.append("What you noted about yourself and him:\n" + "\n".join(learned[:25]))
        if diary:
            parts.append(f"Your diary line for that day: {diary}")
        data = _parse_json(await self._ask(DAY_PROMPT, "\n\n".join(parts)) or "")
        if not data:
            return None
        row = await self._save(day.isoformat(), "day", data)
        log.info("episodes.day_written", day=day.isoformat(), chars=len(row["summary"]))
        return row

    async def build_week(self, monday: date) -> dict | None:
        days = []
        for i in range(7):
            ep = await self.get((monday + timedelta(days=i)).isoformat())
            if ep:
                d = monday + timedelta(days=i)
                days.append(f"{d.strftime('%A %d %b')} ({ep.get('mood') or ''}): "
                            f"{ep['summary']} Open: {'; '.join(ep.get('threads') or [])}")
        if len(days) < 2:
            return None
        data = _parse_json(await self._ask(WEEK_PROMPT, "\n\n".join(days)) or "")
        if not data:
            return None
        row = await self._save(f"W:{monday.isoformat()}", "week", data)
        log.info("episodes.week_written", week=monday.isoformat())
        return row

    async def _ask(self, system: str, user: str) -> str | None:
        from ophelia.mind.growth import persona_system
        from ophelia.providers.fallback import complete_text

        agent = self.agent
        try:
            return await complete_text(
                agent.settings, agent.stack, role="curator", model=agent._model("curator"),
                messages=[{"role": "system", "content": persona_system(system)},
                          {"role": "user", "content": user}],
                log_tag="episodes.fallback")
        except Exception as e:
            log.warning("episodes.llm_failed", error=str(e)[:200])
            return None

    async def _oldest_day(self) -> date | None:
        async with aiosqlite.connect(self.memory.db_path) as db:
            r = await (await db.execute(
                "SELECT min(created_at) FROM messages WHERE channel != 'consciousness'")).fetchone()
        return datetime.fromtimestamp(r[0]).date() if r and r[0] else None

    async def nightly(self, *, backfill: int = BACKFILL_PER_RUN) -> int:
        """Yesterday, then a few missing older days (newest first), then last
        week's chapter. Returns how many episodes were written."""
        await self._init()
        today = self._today()
        written = 0
        oldest = await self._oldest_day()
        limit_days = int(getattr(self.settings, "episodes_backfill_days", 90) or 90)
        floor = today - timedelta(days=limit_days)
        if oldest and oldest > floor:
            floor = oldest
        tried = 0
        day = today - timedelta(days=1)
        while day >= floor and tried < 1 + backfill:
            key = f"episodes:quiet:{day.isoformat()}"
            if await self.get(day.isoformat()) is None and \
                    await self.memory.get_fact(key) is None:
                # Quiet days are cheap to spot and don't use up the night's budget.
                if len(await self._conversation(day)) + len(self._inner(day)) < MIN_LINES:
                    await self.memory.set_fact(key, "1")
                else:
                    tried += 1
                    if await self.build_day(day):
                        written += 1
            day -= timedelta(days=1)
        last_monday = today - timedelta(days=today.weekday() + 7)
        if await self.get(f"W:{last_monday.isoformat()}") is None:
            if await self.build_week(last_monday):
                written += 1
        return written

    # -- remembering
    async def context_block(self) -> str:
        """The long arc for her prompt: the week's chapter, recent days, open threads."""
        await self._init()
        today = self._today()
        monday = today - timedelta(days=today.weekday())
        lines = []
        week = await self.get(f"W:{(monday - timedelta(days=7)).isoformat()}")
        if week:
            lines.append(f"Last week, in your words: {week['summary'][:700]}")
        threads: list[str] = []
        for back in range(SHOW_DAYS, 0, -1):
            d = today - timedelta(days=back)
            ep = await self.get(d.isoformat())
            if not ep:
                continue
            first = " ".join(re.split(r"(?<=[.!?])\s+", ep["summary"])[:3])
            mood = f" ({ep['mood']})" if ep.get("mood") else ""
            lines.append(f"- {d.strftime('%a %d %b')}{mood}: {first[:450]}")
            threads.extend(ep.get("threads") or [])
        if not lines:
            return ""
        out = "# Your recent life (episodic memory, in your own words)\n" + "\n".join(lines)
        if threads:
            out += "\nStill open: " + "; ".join(dict.fromkeys(threads[-6:]))
        out += ("\nrecall_day(when) gives the whole story of any day or week "
                "(\"last tuesday\", \"nov 3\", \"last week\") or finds one by what happened.")
        return out

    async def recall(self, when: str = "yesterday", query: str = "") -> str:
        await self._init()
        today = self._today()
        parsed = parse_when(when, today) if when else None
        if parsed is None and when and not query:
            query = when
        if query:
            hits = await self.search(query)
            if not hits:
                return f"No day in your memory matches {query!r} (episodes go back as far as " \
                       "they've been written; recall_memory searches single messages)."
            return "\n\n".join(self._format(h) for h in hits)
        kind, d = parsed if parsed else ("day", today - timedelta(days=1))
        if kind == "week":
            ep = await self.get(f"W:{d.isoformat()}")
            if ep is None:
                days = [await self.get((d + timedelta(days=i)).isoformat()) for i in range(7)]
                days = [x for x in days if x]
                if not days:
                    return f"Nothing written down for the week of {d.strftime('%d %b')}."
                return "\n\n".join(self._format(x) for x in days)
            return self._format(ep)
        ep = await self.get(d.isoformat())
        if ep is None:
            if d >= today:
                return "Today isn't written down yet (it's written the night after)."
            built = await self.build_day(d)
            if built is None:
                return f"Nothing much happened on {d.strftime('%A %d %b')} that you kept."
            ep = await self.get(d.isoformat())
        return self._format(ep)

    @staticmethod
    def _format(ep: dict) -> str:
        p = ep["period"]
        if p.startswith("W:"):
            d = date.fromisoformat(p[2:])
            head = f"Week of {d.strftime('%d %b %Y')}"
        else:
            head = date.fromisoformat(p).strftime("%A %d %B %Y")
        out = [f"{head}" + (f" ({ep['mood']})" if ep.get("mood") else ""), ep["summary"]]
        if ep.get("moments"):
            out.append("Moments: " + " | ".join(ep["moments"]))
        if ep.get("people"):
            out.append("People: " + ", ".join(ep["people"]))
        if ep.get("threads"):
            out.append("Left open: " + "; ".join(ep["threads"]))
        return "\n".join(out)

    async def run(self) -> None:
        from ophelia.providers.model_gate import get_model_gate

        gate = get_model_gate()
        await asyncio.sleep(300)  # let startup (and the diary) settle
        while not self._stop:
            try:
                if not gate.is_local_busy():
                    await self.nightly()
            except Exception as e:
                log.warning("episodes.loop_error", error=str(e)[:200])
            await asyncio.sleep(CHECK_EVERY_S)

    def stop(self) -> None:
        self._stop = True
