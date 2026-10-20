"""`ophelia persona` — see (and undo) how her personality drifted.

Reports self-rewrites of SOUL.md (with the reasons she gave), PROMPTER.md
backups left by startup migrations, her growth notes, and lessons/memory
entries that read like generic assistant policy — the usual ways a
character slowly turns into a polite chatbot. Can restore an older SOUL /
PROMPTER and delete generic lessons.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

from ophelia.config import OPHELIA_HOME
from ophelia.memory.bootstrap import load_hermes_memories
from ophelia.mind.growth import GROWTH_PATH, is_generic_assistant_rule, load_growth


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _rewrite_log(home: Path) -> list[dict]:
    path = home / "data" / "self_rewrite_log.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def backups(target: str, home: Path = OPHELIA_HOME) -> list[Path]:
    """Newest first. target = 'soul' | 'prompter'."""
    stem = "SOUL" if target == "soul" else "PROMPTER"
    found: list[Path] = []
    vdir = home / "versions"
    if vdir.is_dir():
        found += [p for p in vdir.iterdir() if p.name.upper().startswith(stem)]
    if target == "prompter":
        found += list(home.glob("PROMPTER.md.legacy-*.bak"))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def lessons(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        return []
    with sqlite3.connect(db_path) as db:
        try:
            rows = db.execute(
                "SELECT id, lesson, context, created_at FROM lessons ORDER BY id DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [
        {"id": r[0], "lesson": r[1], "context": r[2] or "", "created_at": r[3]} for r in rows
    ]


def report(db_path: Path, home: Path = OPHELIA_HOME) -> str:
    out: list[str] = ["Ophelia persona report", "=" * 22, ""]

    soul = home / "SOUL.md"
    if soul.is_file():
        out.append(f"SOUL.md: {len(soul.read_text(encoding='utf-8'))} chars, "
                   f"last changed {_fmt_ts(soul.stat().st_mtime)}")
    else:
        out.append("SOUL.md: MISSING — she has no persona file (create ~/.ophelia/SOUL.md)")
    rewrites = [r for r in _rewrite_log(home) if r.get("target") == "SOUL.md"]
    if rewrites:
        out.append(f"  She rewrote her own SOUL {len(rewrites)} time(s). Most recent:")
        for r in rewrites[-5:]:
            out.append(f"   - {_fmt_ts(r.get('ts', 0))}: {r.get('reason') or '(no reason given)'}")
    soul_baks = backups("soul", home)
    if soul_baks:
        out.append("  Older versions (restore with `ophelia persona restore soul <file>`):")
        out += [f"   - {p.name}" for p in soul_baks[:8]]
    out.append("")

    prompter = home / "PROMPTER.md"
    p_rewrites = [r for r in _rewrite_log(home) if r.get("target") == "PROMPTER.md"]
    legacy = list(home.glob("PROMPTER.md.legacy-*.bak"))
    out.append(f"PROMPTER.md: {'present' if prompter.is_file() else 'missing (default used)'}"
               f"; self-rewrites: {len(p_rewrites)}")
    if legacy:
        out.append(
            f"  ⚠ Startup migration replaced PROMPTER.md {len(legacy)} time(s) "
            "(older versions overwrote custom files). If you'd customized it:"
        )
        out += [f"   - ophelia persona restore prompter {p.name}"
                for p in sorted(legacy, reverse=True)[:5]]
    out.append("")

    growth = [ln for ln in load_growth(home / GROWTH_PATH.name).splitlines() if ln.startswith("- ")]
    out.append(f"Growth (who she's becoming): {len(growth)} note(s)")
    out += [f"   {ln}" for ln in growth[-5:]]
    out.append("")

    all_lessons = lessons(db_path)
    generic = [le for le in all_lessons if is_generic_assistant_rule(le["lesson"])]
    out.append(f"Lessons: {len(all_lessons)} total (only ones relevant to a conversation are shown to her). Newest:")
    for le in all_lessons[:5]:
        flag = "  ⚠ generic" if le in generic else ""
        out.append(f"   #{le['id']}: {le['lesson'][:110]}{flag}")
    if generic:
        out.append(f"  ⚠ {len(generic)} lesson(s) read like assistant policy. "
                   "Remove with `ophelia persona forget --generic` (or by id).")
    out.append("")

    mem, user = load_hermes_memories(home) if (home / "memories").is_dir() else ([], [])
    gen_mem = [m for m in mem + user if is_generic_assistant_rule(m)]
    out.append(f"Long-term memory entries: {len(mem)} (+{len(user)} profile)")
    if gen_mem:
        out.append(f"  ⚠ {len(gen_mem)} entr(y/ies) read like assistant policy "
                   f"(edit {home / 'memories'}):")
        out += [f"   - {m[:110]}" for m in gen_mem[:5]]
    return "\n".join(out)


def restore(target: str, name: str, home: Path = OPHELIA_HOME) -> str:
    candidates = {p.name: p for p in backups(target, home)}
    src = candidates.get(name)
    if src is None:
        return f"No {target} backup named {name!r}. Run `ophelia persona` to list them."
    dest = home / ("SOUL.md" if target == "soul" else "PROMPTER.md")
    note = ""
    if dest.is_file():
        vdir = home / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        saved = vdir / f"{dest.stem}-{time.strftime('%Y%m%d-%H%M%S')}{dest.suffix}"
        shutil.copy2(dest, saved)
        note = f" (current version saved as {saved.name})"
    shutil.copy2(src, dest)
    return f"Restored {dest.name} from {src.name}{note}. Restart Ophelia to apply."


def forget(
    db_path: Path,
    ids: list[int],
    *,
    generic: bool = False,
    everything: bool = False,
    matching: list[str] | None = None,
) -> str:
    """Delete lessons by id, all generic-policy ones, all lessons containing
    any of `matching` words, or every lesson."""
    current = lessons(db_path)
    targets = set(ids)
    if everything:
        targets |= {le["id"] for le in current}
    if generic:
        targets |= {le["id"] for le in current if is_generic_assistant_rule(le["lesson"])}
    words = [w.lower() for w in (matching or []) if w.strip()]
    if words:
        targets |= {
            le["id"] for le in current if any(w in le["lesson"].lower() for w in words)
        }
    if not targets:
        return "Nothing to remove."
    with sqlite3.connect(db_path) as db:
        db.executemany("DELETE FROM lessons WHERE id = ?", [(i,) for i in sorted(targets)])
        db.commit()
    shown = ", ".join(f"#{i}" for i in sorted(targets)[:20])
    more = f" (+{len(targets) - 20} more)" if len(targets) > 20 else ""
    return f"Removed {len(targets)} lesson(s): {shown}{more}"