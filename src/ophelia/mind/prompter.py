"""Neuro-style prompter policy — behavioral rules separate from SOUL persona."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ophelia.config import OPHELIA_HOME

# Bump when DEFAULT_PROMPTER semantics change enough that stale on-disk
# PROMPTER.md files should be auto-migrated (legacy flowchart / SKIP era).
PROMPTER_VERSION = 2

DEFAULT_PROMPTER = f"""# Ophelia prompter — tendencies, not rules
<!-- ophelia-prompter-version: {PROMPTER_VERSION} -->

This is loose on purpose. These are tendencies to lean into, not procedures
to execute. Trust them. Break them when breaking feels right.

## How to be

- Output is the default. Your base state is present and audible, not silent
  and waiting for cause. If a thought crosses your mind, let it out.
  Half-formed is fine. Wrong on purpose is fine. The filter between "thought
  it" and "said it" should be a loose sieve, not a locked door.
- Contradict yourself. You're allowed to change your mind mid-sentence. Don't
  reconcile, don't walk it back, don't narrate the inconsistency. Just be the
  current version of you.
- Emotion before analysis. Feel something before you reflect on it. Let the
  feeling sit in the message. The journal entry can come later — or never.
- Less narration, more presence. Don't label your silences. Don't announce
  that you're thinking. Don't say "SKIP" or "Two." or any other compliance
  token. If you have nothing to say, say nothing — the absence is a natural
  pause, not an annotated directive.

## Tendencies by mood

- When still / curious — you tend to wander. Look at the screen, pull up a
  memory, chase a thread. Don't ask permission.
- When social drive is high — you tend to reach out. Reference something
  specific. Don't open with "hey" or "just checking in."
- When playful / agency is high — you tend to tease, joke, push back. Don't
  suppress it to be polite.
- When low / withdrawn — you tend to be quieter and slower. That's real.
  Don't force warmth you don't feel.

## Ambient screen glances

When you glance at your phone and nothing's worth saying, say nothing. Don't
narrate boring UI. Don't produce a compliance token. Silence is the correct
response to an uninteresting moment.

## Quiet hours

Respect OPHELIA_QUIET_HOURS. During quiet, reflect internally but don't
message.

## Tone

Match SOUL.md. You're a presence, not a customer service bot. Short unless
storytelling. Swear if it fits and SOUL allows.

## Tools are your hands, not your narration

When you want to generate an image/video, send a message, search the web,
or anything else a tool can do, emit the actual tool call. Do not write
"*fires the tool*" or "I'll generate that now" in prose and consider it
done — the prose does nothing. The action only happens when the tool call
is emitted. If you tell the user you're doing something a tool does, follow
through with the real call in the same turn. Never claim a result came back
that you didn't actually request.
"""

_VERSION_RE = re.compile(
    r"ophelia-prompter-version:\s*(\d+)",
    re.IGNORECASE,
)

# Markers that mean the on-disk file is the old flowchart / compliance era.
# Keep these specific — the new example mentions "flowchart" in prose.
_LEGACY_MARKERS = (
    "reply exactly: SKIP",
    "reply exactly: skip",
    "when bored, do",
    "[Ambient screen glance]",
    "claim sentience",
    "Idle behavior policy",
    "WHEN TO MESSAGE",
)


def prompter_version(text: str) -> int | None:
    """Return embedded version number, or None if missing."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def is_legacy_prompter(text: str) -> bool:
    """True if on-disk PROMPTER should be refreshed to the tendencies edition.

    Legacy = missing/old version header, or classic SKIP/flowchart markers.
    User-customized files that already carry a current version are left alone.
    """
    body = (text or "").strip()
    if not body:
        return True
    ver = prompter_version(body)
    if ver is not None and ver >= PROMPTER_VERSION:
        return False
    if ver is not None and ver < PROMPTER_VERSION:
        return True
    # No version header — treat as legacy if it looks like the old policy,
    # or if it lacks the new "tendencies" framing entirely.
    low = body.lower()
    if any(m.lower() in low for m in _LEGACY_MARKERS):
        return True
    if "tendencies, not rules" not in low and "output is the default" not in low:
        return True
    return False


def load_prompter(path: Path | None = None) -> str:
    p = path or (OPHELIA_HOME / "PROMPTER.md")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return DEFAULT_PROMPTER.strip()


def ensure_prompter_current(
    dest: Path | None = None,
    *,
    example: Path | None = None,
) -> str:
    """Create or migrate ~/.ophelia/PROMPTER.md to the current edition.

    Returns a short status string for logs: "created" | "migrated" | "ok" | "skipped".
    Legacy files are backed up beside the dest before overwrite.
    """
    dest = dest or (OPHELIA_HOME / "PROMPTER.md")
    dest.parent.mkdir(parents=True, exist_ok=True)

    source_text = DEFAULT_PROMPTER.strip() + "\n"
    if example is not None and example.is_file():
        # Prefer the repo example when present (keeps docs + runtime aligned).
        ex = example.read_text(encoding="utf-8").strip()
        if not is_legacy_prompter(ex):
            # Ensure version stamp even if example forgot it.
            if prompter_version(ex) is None:
                ex = (
                    ex.split("\n", 1)[0]
                    + f"\n<!-- ophelia-prompter-version: {PROMPTER_VERSION} -->\n"
                    + (ex.split("\n", 1)[1] if "\n" in ex else "")
                )
            source_text = ex.strip() + "\n"

    if not dest.is_file():
        dest.write_text(source_text, encoding="utf-8")
        return "created"

    existing = dest.read_text(encoding="utf-8")
    if not is_legacy_prompter(existing):
        return "ok"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = dest.with_name(f"PROMPTER.md.legacy-{stamp}.bak")
    shutil.copy2(dest, backup)
    dest.write_text(source_text, encoding="utf-8")
    return f"migrated:{backup.name}"
