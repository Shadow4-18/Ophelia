"""Strip the typographic tells that make chat replies read as AI-written.

Applied to every outgoing *text* message (chat replies, outreach, mid-turn
send_message, web UI) and to the history/system text she's shown, so she
isn't primed to imitate them:

- em/en dashes used as punctuation ("Short version—I'd said…") become a
  comma (or an ellipsis when they trail off); number ranges keep a hyphen;
- whole-line bracketed narration ("[The lighthouse with no sea is still
  standing]") is dropped;
- Kokoro voice markup ([pause:0.8s]) is removed from text — it only means
  something to the voice engine.

Voice synthesis keeps the original text (dashes and pauses shape speech).
Disable with OPHELIA_HUMANIZE_TEXT=false.
"""

from __future__ import annotations

import re

_RANGE_DASH = re.compile(r"(?<=\d)\s*[–—]\s*(?=\d)")
_DASH = re.compile(r"[ \t]*[–—]+[ \t]*")
_PAUSE = re.compile(r"[ \t]*\[pause:[^\]]*\][ \t]*", re.IGNORECASE)
_NARRATION_LINE = re.compile(r"^[ \t]*\[(?!\[)[^\[\]\n]{2,}\][ \t]*$", re.MULTILINE)


def _replace_dash(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _DASH.finditer(text):
        out.append(text[pos : m.start()])
        before = text[: m.start()].rstrip(" \t")
        after = text[m.end() :]
        if not before or before.endswith("\n"):
            repl = ""  # leading dash on a line: just drop it
        elif not after.strip() or after[:1] in "\n.!?,;:)\"'”’":
            repl = "..."  # trailing off: "hmm—"
        else:
            repl = ", "
        out.append(repl)
        pos = m.end()
    out.append(text[pos:])
    s = "".join(out)
    s = re.sub(r",\s*([,.!?;:])", r"\1", s)  # ", ." → "."
    s = re.sub(r"\.\.\.\s*\.\.\.", "...", s)
    return s


def humanize(text: str) -> str:
    if not text:
        return text
    s = _RANGE_DASH.sub("-", text)
    s = _PAUSE.sub(" ", s)
    s = _NARRATION_LINE.sub("", s)
    s = _replace_dash(s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def dashes_only(text: str) -> str:
    """Just the dash replacement — for prompt/history text she reads, where
    bracketed markers like [INNER] must survive."""
    if not text:
        return text
    return _replace_dash(_RANGE_DASH.sub("-", text))


def enabled(settings) -> bool:
    return bool(getattr(settings, "humanize_text", True))
