"""Her own day, as she lived it.

People remember what they did today. Without this she found her own 4am
images "apparently" — like reading a stranger's files. Notable things she
does (makes art, messages someone, publishes, plays, sleeps and wakes, talks
with her owner) are appended to a small per-day file and shown back to her as
a short timeline, in local time. The diary is written from it at night.

Files: data/days/YYYY-MM-DD.jsonl. Plain appends, no LLM, never raises.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Merge repeats of the same kind of thing within this window ("made 4 images").
_MERGE_WINDOW_S = 15 * 60
# Owner conversations: one entry per stretch of talking.
_CHAT_GAP_S = 30 * 60


def _tz(settings: Any):
    from ophelia.timeutil import resolve_timezone

    return resolve_timezone(getattr(settings, "timezone", "system"))


def _days_dir(settings: Any) -> Path:
    return Path(settings.data_dir) / "days"


def _path_for(settings: Any, when: datetime) -> Path:
    return _days_dir(settings) / f"{when.strftime('%Y-%m-%d')}.jsonl"


def _now(settings: Any) -> datetime:
    return datetime.now(tz=_tz(settings))


def load(settings: Any, day: datetime | None = None) -> list[dict]:
    path = _path_for(settings, day or _now(settings))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _rewrite(settings: Any, when: datetime, entries: list[dict]) -> None:
    path = _path_for(settings, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                   encoding="utf-8")
    tmp.replace(path)


def record(settings: Any, kind: str, text: str, *, count: int = 1,
           detail: str = "", autonomous: bool = False) -> None:
    """Note something she did. ``kind`` groups repeats: two ``image`` entries
    a few minutes apart become one with a count."""
    if settings is None or not isinstance(getattr(settings, "data_dir", None), (str, Path)):
        return
    try:
        now = _now(settings)
        ts = now.timestamp()
        entries = load(settings, now)
        last = entries[-1] if entries else None
        if (
            last
            and last.get("kind") == kind
            and kind != "chat"
            and ts - float(last.get("end", last.get("ts", 0))) < _MERGE_WINDOW_S
        ):
            last["count"] = int(last.get("count", 1)) + count
            last["end"] = ts
            if detail and detail not in last.get("details", []):
                last.setdefault("details", []).append(detail[:80])
                last["details"] = last["details"][-3:]
            _rewrite(settings, now, entries)
            return
        entry = {"ts": ts, "kind": kind, "text": text[:160], "count": count,
                 "autonomous": autonomous}
        if detail:
            entry["details"] = [detail[:80]]
        path = _path_for(settings, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug("day_log.record_failed", error=str(e))


def record_chat(settings: Any, who: str, opener: str) -> None:
    """One entry per stretch of conversation with someone."""
    if settings is None or not isinstance(getattr(settings, "data_dir", None), (str, Path)):
        return
    try:
        now = _now(settings)
        ts = now.timestamp()
        entries = load(settings, now)
        for e in reversed(entries):
            if e.get("kind") == "chat" and e.get("who") == who:
                if ts - float(e.get("end", e["ts"])) < _CHAT_GAP_S:
                    e["end"] = ts
                    e["count"] = int(e.get("count", 1)) + 1
                    _rewrite(settings, now, entries)
                    return
                break
        opener = " ".join((opener or "").split())
        if len(opener) > 60:
            opener = opener[:57].rstrip() + "..."
        entry = {"ts": ts, "end": ts, "kind": "chat", "who": who,
                 "text": f"talked with {who}", "count": 1, "opener": opener}
        path = _path_for(settings, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug("day_log.chat_failed", error=str(e))


def _clock(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz=tz).strftime("%I:%M %p").lstrip("0").lower()


def _line(e: dict, tz) -> str:
    start = _clock(float(e["ts"]), tz)
    end = float(e.get("end", e["ts"]))
    span = f"{start}–{_clock(end, tz)}" if end - float(e["ts"]) >= 120 else start
    text = e.get("text", "")
    n = int(e.get("count", 1))
    if e.get("kind") == "chat":
        opener = e.get("opener")
        text += f' (started with "{opener}")' if opener else ""
    elif "{n}" in text:
        text = text.replace("{n}", str(n)).replace("{s}", "" if n == 1 else "s")
    elif n > 1:
        text += f" (x{n})"
    details = e.get("details") or []
    if details:
        text += ": " + "; ".join(details)
    return f"- {span} {text}"


def lines(settings: Any, *, limit: int = 14) -> list[str]:
    tz = _tz(settings)
    entries = load(settings)
    return [_line(e, tz) for e in entries[-limit:]]


def context_block(settings: Any, *, limit: int = 14) -> str:
    """'# Your day so far' for her prompts, or '' on a blank day."""
    if not isinstance(getattr(settings, "data_dir", None), (str, Path)):
        return ""
    try:
        got = lines(settings, limit=limit)
    except Exception as e:
        log.debug("day_log.block_failed", error=str(e))
        return ""
    if not got:
        return ""
    return (
        "# Your day so far (things you did, in your local time; you remember these)\n"
        + "\n".join(got)
    )


def yesterday_lines(settings: Any, day: datetime) -> list[str]:
    tz = _tz(settings)
    return [_line(e, tz) for e in load(settings, day)]


# --- what counts as notable -------------------------------------------------


def _short(s: Any, n: int = 70) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def note_tool(settings: Any, name: str, args: dict, result: str, *,
              autonomous: bool, owner: bool) -> None:
    """Called after every tool call; records the ones a person would
    remember doing. Failures are skipped (she didn't *do* it)."""
    res = (result or "").strip()
    low = res.lower()
    if not res or low.startswith(("refused", "error", "unknown tool", "image generation failed",
                                  "civitai", "that action")) and "saved to" not in low:
        return
    who = "on your own" if autonomous else ("because your owner asked" if owner else "for a guest")
    if name == "generate_image":
        if low.startswith("pending:"):
            record(settings, "image", f"started a slow image {who}", detail=_short(args.get("prompt")),
                   autonomous=autonomous)
            return
        n = res.count("Image saved to") or 1
        record(settings, "image", f"made {{n}} image{{s}} {who}", count=n,
               detail=_short(args.get("prompt")), autonomous=autonomous)
    elif name == "generate_video":
        record(settings, "video", f"made {{n}} video{{s}} {who}",
               detail=_short(args.get("prompt")), autonomous=autonomous)
    elif name == "text_to_speech":
        record(settings, "voice", f"recorded {{n}} voice note{{s}} {who}",
               detail=_short(args.get("text"), 50), autonomous=autonomous)
    elif name == "send_message" and autonomous:
        record(settings, "outreach", "messaged your owner first", detail=_short(args.get("text"), 60),
               autonomous=True)
    elif name == "send_message_to_guest":
        record(settings, "guest_msg", "messaged a guest", detail=_short(args.get("text"), 50),
               autonomous=autonomous)
    elif name == "site_deploy":
        record(settings, "site", "published your site", autonomous=autonomous)
    elif name in ("site_upsert_page", "site_write_file"):
        target = args.get("slug") or args.get("path") or args.get("title")
        record(settings, "site_edit", "worked on your site", detail=_short(target, 40),
               autonomous=autonomous)
    elif name == "phone_game_open":
        record(settings, "game", "played a game on the phone",
               detail=_short(args.get("game") or args.get("name"), 30), autonomous=autonomous)
    elif name == "apk_extract" and low.startswith(("extracted", "imported")):
        record(settings, "apps", "pulled an app apart to see how it works",
               detail=_short(args.get("package") or args.get("path"), 40), autonomous=autonomous)
    elif name == "apk_notes" and args.get("text"):
        record(settings, "apps", "worked out how an app works",
               detail=_short(args.get("package"), 40), autonomous=autonomous)
    elif name == "evolve_self":
        record(settings, "growth", "noted something about who you're becoming",
               detail=_short(args.get("note") or args.get("text"), 60), autonomous=autonomous)
    elif name == "remember_person":
        record(settings, "people", "made a note about someone",
               detail=_short(args.get("name"), 30), autonomous=autonomous)
    elif name == "goal_create":
        record(settings, "goal", "set yourself a new goal",
               detail=_short(args.get("description"), 60), autonomous=autonomous)
    elif name == "look_at_web_page":
        record(settings, "looked", "looked at a web page",
               detail=_short(args.get("url"), 60), autonomous=autonomous)
    elif name == "save_skill":
        record(settings, "skill", "saved a new skill", detail=_short(args.get("name"), 40),
               autonomous=autonomous)
