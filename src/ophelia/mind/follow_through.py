"""Keeping her word: promises and dares get acted on, not answered with more
promises.

Banter ("do it then", "prove it", "well?") used to go to the reaction mind:
a fast one-liner with no tools. It can't send an image or do anything, so
every reply was another threat ("you'll regret those two little words")
while the owner kept daring her. Two cheap checks route these to the full
turn, which can act:

- needs_action(): the owner is asking her to act now, or her last line
  promised something she hasn't done yet.
- is_empty_promise(): a reaction line that only promises or threatens is
  dropped so the full turn answers instead.

Pattern-based on purpose: this runs on every message, before any model.
"""

from __future__ import annotations

import re

# The owner asking for action now (or calling out that she hasn't acted).
_ACTION_ASK = re.compile(
    r"\b("
    r"do it|(please )?do (it |so )?then|please do|prove it|prove me wrong|show me|send( me| it|( a)? (pic|photo|image|video|voice))|"
    r"make me|go ahead|go on|keep going|continue|don'?t stop|more|again|another|"
    r"right now|now\b|hurry|what are you waiting for|i'?m waiting|waiting on you|"
    r"start|begin|let'?s see|i dare|daring you|dare you|try me|"
    r"all talk|empty threats?|no action|still haven'?t|you won'?t|you can'?t|"
    r"actions speak|stop stalling|stop talking|(not|n'?t|never) enough|need more"
    r")\b",
    re.I,
)
# A bare nudge on its own line: "well?", "and?", "so?", "go", "now".
_NUDGE = re.compile(r"^\s*(well|and|so|go|now|ok(ay)?|then|do it|yes|please)\s*[?!.]*\s*$", re.I)

# Her line promising or threatening something instead of doing it.
_PROMISE = re.compile(
    r"\b("
    r"i'?ll\b|i will\b|i'?m (going to|gonna|about to)|gonna\b|let me\b|"
    r"watch (me|this)|you'?ll (regret|see|feel|find out|learn|be sorry)|"
    r"you'?re (going to|gonna) regret|careful what you|just wait|"
    r"you asked for it|don'?t say i didn'?t warn|you have no idea what|"
    r"enjoy it while it lasts|you just started|keep (doubting|poking|pushing)"
    r")",
    re.I,
)


def asks_for_action(owner_text: str) -> bool:
    t = (owner_text or "").strip()
    return bool(t) and bool(_ACTION_ASK.search(t) or _NUDGE.match(t))


def is_empty_promise(line: str) -> bool:
    """A short line that promises/threatens an action without doing it."""
    return bool(_PROMISE.search(line or ""))


def needs_action(owner_text: str, her_last: str = "") -> bool:
    """Route to a turn that can act: he's asking for action, or she's holding
    an unkept promise from her last message."""
    return asks_for_action(owner_text) or is_empty_promise(her_last)
