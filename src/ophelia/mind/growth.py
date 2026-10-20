"""Self-evolution as *learning*, not identity replacement.

Her core persona (SOUL.md) is set with her owner. What she evolves on her
own lives in GROWTH.md — "who I'm becoming": opinions she's formed, things
she's come to love or hate, how she feels about people, habits and quirks
that developed. It's injected right after the SOUL and framed as deepening
it, never overriding it. That's how a person changes: the core stays, the
texture accumulates.

Notes (and lessons) that read like generic assistant etiquette or safety
policy are refused — that kind of "learning" is what slowly turns a
character into a polite chatbot.

``OPHELIA_PERSONA_LOCK`` controls the core SOUL / PROMPTER rewrite tools:
- ``owner`` (default): she can rewrite them only in a conversation with
  her owner (i.e. when you ask), never on her own initiative;
- ``on``: never via tools (edit the files yourself);
- ``off``: old behaviour — she may rewrite them whenever.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ophelia.config import OPHELIA_HOME

GROWTH_PATH = OPHELIA_HOME / "GROWTH.md"
MAX_NOTES = 60
MAX_CHARS = 5000
HEADER = (
    "# Who I'm becoming\n"
    "Things I've learned about myself over time. They deepen my SOUL; they never replace it.\n"
)

# Phrases typical of assistant policy / safety boilerplate rather than a
# character learning something about her own life.
_GENERIC_RULE_RE = re.compile(
    r"(?ix)"
    r"\bas\ an?\ (ai|assistant|language\ model)\b"
    r"|\b(ai|virtual)\ assistant\b"
    r"|\blanguage\ model\b"
    r"|\bbe\ (more\ )?(helpful|respectful|professional|appropriate|polite|neutral|"
    r"objective|mindful\ of\ boundaries)\b"
    r"|\b(always|must|should)\ (remain|stay)\ (respectful|appropriate|professional|"
    r"neutral|helpful)\b"
    r"|\b(maintain|respect|set)\ (appropriate\ |healthy\ |clear\ )?boundaries\b"
    r"|\bavoid\ (inappropriate|harmful|offensive|controversial)\b"
    r"|\b(harmful|inappropriate|offensive)\ content\b"
    r"|\bensure\ (user\ )?(safety|comfort|consent)\b"
    r"|\bprioriti[sz]e\ (user\ )?(safety|wellbeing|well-being)\b"
    r"|\bseek\ professional\ help\b"
    r"|\b(i\ am|i'm)\ (just\ )?an?\ ai\b"
)


def is_generic_assistant_rule(text: str) -> bool:
    return bool(_GENERIC_RULE_RE.search(text or ""))


GENERIC_REJECTION = (
    "Not saved — that reads like generic assistant policy, not something you "
    "actually learned about your own life. Rephrase it in your own voice, about "
    "you, the people you know, or what happened (or let it go)."
)


def load_growth(path: Path | None = None) -> str:
    p = path or GROWTH_PATH
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    body = text[len(HEADER.strip()):].strip() if text.startswith(HEADER.strip()) else text
    return body


def add_growth(note: str, *, path: Path | None = None) -> str:
    p = path or GROWTH_PATH
    note = " ".join((note or "").split())[:300]
    if not note:
        return "Nothing to record."
    if is_generic_assistant_rule(note):
        return GENERIC_REJECTION
    lines = [ln for ln in load_growth(p).splitlines() if ln.strip().startswith("- ")]
    if any(note.lower() in ln.lower() for ln in lines):
        return "Already part of you."
    lines.append(f"- ({time.strftime('%Y-%m-%d')}) {note}")
    # Keep it a living page, not an ever-growing log: oldest notes fall off.
    while len(lines) > MAX_NOTES or sum(len(ln) + 1 for ln in lines) > MAX_CHARS:
        lines.pop(0)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return f"Recorded in who you're becoming: {note}"


def persona_rewrite_blocked(settings, *, is_owner_turn: bool) -> str | None:
    """Reason the core SOUL/PROMPTER can't be rewritten right now, else None."""
    mode = (getattr(settings, "persona_lock", "owner") or "owner").lower()
    if mode == "off":
        return None
    if mode == "owner" and is_owner_turn:
        return None
    if mode == "on":
        return (
            "Your core SOUL/PROMPTER is locked (OPHELIA_PERSONA_LOCK=on) — your "
            "owner edits those files directly. To grow, use evolve_self."
        )
    return (
        "Your core SOUL/PROMPTER only changes when your owner asks you to in a "
        "conversation. To grow on your own, use evolve_self — record what you've "
        "learned about yourself and it becomes part of who you're becoming."
    )


def persona_system(instruction: str) -> str:
    """System prompt for her private passes (reflection, dreams) that keeps
    her own voice. Without the SOUL these ran as a generic model and wrote
    generic 'lessons' that then flattened her personality."""
    from ophelia.memory.bootstrap import load_soul

    parts = [instruction]
    soul = (load_soul() or "").strip()
    if soul:
        parts.append("# Your SOUL (who you are)\n" + soul[:3000])
    growth = load_growth()
    if growth:
        parts.append("# Who you're becoming\n" + growth[-1500:])
    return "\n\n".join(parts)
