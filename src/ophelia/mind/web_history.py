"""Her browsing history: every search and every page she reads or looks at.

Her log only said a web tool ran, never what she searched for or which page
she opened, so the owner had no way to see where she goes online. Each
search (the query and the top results she got back), each page read
(fetch_url) and each page looked at (look_at_web_page) is appended to
data/web_history/YYYY-MM-DD.jsonl with who it was for: the owner, a guest or
friend by name, or her own time. It's shown with /web on Telegram and posted
live to #web-history on the Discord log server.

Plain appends, no LLM, never raises into the tool that called it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

log = structlog.get_logger()

_RESULT = re.compile(r"^- (.+?)(?: \(\d{4}-\d\d-\d\d[^)]*\))?\n\s+(https?://\S+)", re.M)
_FAILED = ("fetch failed", "url must start", "couldn't render", "empty query",
           "web search failed", "no results")

Listener = Callable[[dict], Awaitable[None]]
_listeners: list[Listener] = []


def subscribe(fn: Listener) -> None:
    """Called with each new entry (the Discord mirror uses this)."""
    if fn not in _listeners:
        _listeners.append(fn)


def unsubscribe(fn: Listener) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _tz(settings: Any):
    from ophelia.timeutil import resolve_timezone

    return resolve_timezone(getattr(settings, "timezone", "system"))


def _dir(settings: Any) -> Path:
    return Path(settings.data_dir) / "web_history"


def _path(settings: Any, day: datetime) -> Path:
    return _dir(settings) / f"{day.strftime('%Y-%m-%d')}.jsonl"


def results_in(text: str, limit: int = 5) -> list[dict[str, str]]:
    """Title and link of the top results in a search tool's output."""
    out: list[dict[str, str]] = []
    for m in _RESULT.finditer(text or ""):
        url = m.group(2).rstrip(").,")
        if all(r["url"] != url for r in out):
            out.append({"title": m.group(1).strip()[:140], "url": url})
        if len(out) >= limit:
            break
    return out


def failed(result: str) -> bool:
    return (result or "").strip().lower().startswith(_FAILED)


def record(settings: Any, kind: str, *, who: str, query: str = "", url: str = "",
           results: list[dict[str, str]] | None = None, ok: bool = True,
           note: str = "") -> dict | None:
    """Append one entry and tell the listeners. ``kind`` is search | read | look."""
    if settings is None or not isinstance(getattr(settings, "data_dir", None), (str, Path)):
        return None
    now = datetime.now(tz=_tz(settings))
    entry: dict[str, Any] = {"ts": time.time(), "kind": kind, "who": who, "ok": bool(ok)}
    if query:
        entry["query"] = query[:300]
    if url:
        entry["url"] = url[:500]
    if results:
        entry["results"] = results[:5]
    if note:
        entry["note"] = note[:200]
    try:
        path = _path(settings, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug("web_history.write_failed", error=str(e))
    log.info("web_history." + kind, who=who, query=query[:120] or None, url=url[:200] or None,
             ok=ok)
    for fn in list(_listeners):
        try:
            asyncio.get_running_loop().create_task(fn(entry))
        except RuntimeError:
            break  # no running loop (tests, scripts): nothing to tell
        except Exception as e:
            log.debug("web_history.listener_failed", error=str(e))
    return entry


def load(settings: Any, day: datetime | None = None) -> list[dict]:
    day = day or datetime.now(tz=_tz(settings))
    try:
        lines = _path(settings, day).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def recent(settings: Any, n: int = 20, *, days: int = 7) -> list[dict]:
    """The last ``n`` entries, looking back up to ``days`` days."""
    today = datetime.now(tz=_tz(settings))
    out: list[dict] = []
    for back in range(days):
        out = load(settings, today - timedelta(days=back)) + out
        if len(out) >= n:
            break
    return out[-n:]


def format_entry(entry: dict, settings: Any, *, links: bool = True) -> str:
    """One entry as a few readable lines."""
    tz = _tz(settings)
    when = datetime.fromtimestamp(float(entry.get("ts", 0)), tz=tz)
    stamp = when.strftime("%b %d %I:%M %p").replace(" 0", " ")
    who = entry.get("who", "?")
    fail = "" if entry.get("ok", True) else " (failed)"
    kind = entry.get("kind")
    if kind == "search":
        head = f"🔎 {stamp} · {who}: searched \"{entry.get('query', '')}\"{fail}"
        rows = entry.get("results") or []
        body = [f"   {i}. {r['title']}" + (f"\n      {r['url']}" if links else "")
                for i, r in enumerate(rows[:3], 1)]
        if len(rows) > 3:
            body.append(f"   (+{len(rows) - 3} more)")
        return "\n".join([head, *body])
    verb = "read" if kind == "read" else "looked at"
    line = f"🌐 {stamp} · {who}: {verb} {entry.get('url', '')}{fail}"
    if entry.get("note"):
        line += f"\n   ({entry['note']})"
    return line


def render(entries: list[dict], settings: Any) -> str:
    if not entries:
        return "She hasn't searched or opened anything online yet."
    return "\n\n".join(format_entry(e, settings) for e in entries)
