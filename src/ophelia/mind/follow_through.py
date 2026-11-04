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


# Her reply ending on "I'm about to do it": she should then actually do it,
# without waiting for him to say something first. ("Give me thirty seconds",
# then nothing until he wrote again: it made her feel like a chatbot.)
_IMMINENT = re.compile(
    r"\b("
    r"give me (a |one |two |thirty |30 |a few |few |a couple( of)? )?"
    r"(sec|secs|second|seconds|min|mins|minute|minutes|moment|moments)\b|"
    r"(hold|hang) on|one sec|wait for it|watch (this|me)|coming (right )?up|on it\b|"
    r"(i'?ll|i will|let me|i'?m (going to|gonna|about to)) "
    r"(do it|do that|make|send|show|draw|generate|try|get|go|fix|redo|take|record|"
    r"say it|sing|snap)"
    r")",
    re.I,
)
# Tools that are the doing itself (not looking things up).
_DOING_PREFIXES = ("generate_", "text_to_speech", "send_file", "phone_", "game_",
                   "play_", "site_deploy", "react_to_message")


def imminent_promise(reply: str) -> str:
    """The line where her reply promises to act right away ("" if none).
    Only the end of the reply counts: that's where a promise is left hanging."""
    lines = [ln.strip() for ln in (reply or "").replace("[[break]]", "\n").splitlines()
             if ln.strip()]
    for line in reversed(lines[-3:]):
        if _IMMINENT.search(line):
            return line[:200]
    return ""


def did_something(tools_called: list[str]) -> bool:
    return any(t.startswith(_DOING_PREFIXES) for t in tools_called or [])


def follow_through_delay(line: str, rng=None) -> float:
    """About as long as she said: seconds, a minute, or a short beat."""
    import random

    rng = rng or random
    low = (line or "").lower()
    if re.search(r"\bmin(ute)?s?\b|few minutes", low):
        return rng.uniform(40, 60)
    if re.search(r"thirty|30\b", low):
        return rng.uniform(20, 30)
    if re.search(r"\bsec(ond)?s?\b|moment", low):
        return rng.uniform(8, 15)
    return rng.uniform(5, 10)


def follow_through_note(line: str) -> str:
    return (f"[He hasn't answered yet. You told him: \"{line}\" and haven't done it. "
            "Do it now: call the tools and make it happen. Don't announce it again, "
            "don't ask, don't apologise for the wait.]")


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
