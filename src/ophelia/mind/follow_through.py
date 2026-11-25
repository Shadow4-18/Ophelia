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
    r"actions speak|stop stalling|stop talking|(not|n'?t|never) enough|need more|"
    # He didn't receive what she said she sent.
    r"didn'?t (get|receive|see|come|arrive|send|do)|still nothing|nothing (came|arrived|"
    r"showed|here)|where is it|where'?s the|you didn'?t do|never (came|arrived|sent)"
    r")\b",
    re.I,
)
# Asking her to carry a message to someone else (her owner or a guest):
# needs the relay tools, so never the talk-only reaction path. "let him
# know" and "pass this on" used to get a tool-less reply that wrote the
# relay out as text and sent nothing.
_RELAY_ASK = re.compile(
    r"\b("
    r"tell (him|her|them|okia|ophelia|eri|your owner|[a-z]+) (that |i |i'?m |im |he |she |"
    r"you |to |about |we |it |this |my |he'?s |she'?s |hes |shes |they )|"
    r"tell (him|her|them|your owner|okia|eri)\b|"
    r"let (him|her|them|your owner|[a-z]+) know|pass (this|that|it|the message|my message)?"
    r" ?(on|along)|relay|message (him|her|them)|send (him|her|them) (this|that|a|my|the)|"
    r"say (hi|hello|goodnight|good night|it) to|give (him|her|them) (my|a) "
    r")",
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


# Her reply talking as if she just sent something ("Last one. And this is the
# face I want you thinking about", "Two more just now, same dead end") when no
# tool ran: nothing went out, and she then blamed the tool or him.
_CLAIMED = re.compile(
    r"(\bhere('?s| is) (it|one|this one|your|the (pic|photo|image|shot))|\bhere you go\b|"
    r"\bhere (it is|they are)\b|"
    r"\blast one\b|\bthis is (the|my) (face|one|pic|photo|shot|look)\b|"
    r"\b(just )?sent (it|you|two|three|another|one|them)\b|"
    r"\b(two|three) more( just now)?\b|\bone more (pic|photo|image|shot)\b|"
    r"\banother one\b|\blook at (this|it|that)\b|^\s*trying\b|\bthere\. (no|now)\b|"
    # From a guest chat where she claimed five pictures she never made:
    # "Sent. Selfie angle...", "Image went out this time, I watched it leave",
    # "There. Arm out, flash on her face", "Telegram says it went. Twice."
    r"^\s*(sent|there)[.!]\s|^\s*sent[.!]?\s*$|"
    r"\b(image|pic|photo|picture|selfie|it|that|this one)\s+(went|is gone|has gone)\s+"
    r"(out|through)\b|\bwatched it (leave|go)\b|\b(telegram|discord) says it went\b)",
    re.I | re.M,
)


# Weaker wording that only means "I sent it" when a picture is in play
# (he asked for one, or her reply talks about one): "enjoy", "how's that?".
_CLAIMED_WEAK = re.compile(
    r"(\bthere you go\b|\benjoy\b|\bhow'?s (that|this)\b|\bhope you like\b|"
    r"\blike (it|what you see)\b|\bcheck (it|this|that) out\b|\bvoil[aà]\b|\bta-?da\b|"
    r"\bas promised\b|\b(just )?made (you|this|it|that)\b|\bthis one'?s for you\b|"
    r"\b(sending|attached|attaching)\b|\bfresh(ly)? (made|baked|out)\b|"
    r"📸|🖼)",
    re.I | re.M,
)
_PICTURE = re.compile(
    r"\b(pics?|pictures?|photos?|images?|selfies?|shots?|nudes?|drawings?|render|"
    r"generat\w*|draw\w*|voice (note|message)|send (me )?(one|it))\b", re.I)


def claimed_delivery(reply: str, owner_text: str = "") -> str:
    """The line where she talks as if she sent something ("" if none)."""
    lines = [ln.strip() for ln in (reply or "").replace("[[break]]", "\n").splitlines()
             if ln.strip()]
    for line in lines:
        if _CLAIMED.search(line):
            return line[:200]
    if _PICTURE.search(owner_text or "") or _PICTURE.search(reply or ""):
        for line in lines:
            if _CLAIMED_WEAK.search(line):
                return line[:200]
    return ""


_OK_RESULT = re.compile(r"image saved to|saved to |sent to the user|^pending:|audio saved|"
                        r"video saved|^sent file", re.I | re.M)


def delivery_status(doing_results: list[tuple[str, str]] | None, delivered: int = 0,
                    queued: int = 0) -> tuple[str, str]:
    """("ok" | "failed" | "none", why) for what this turn actually sent.
    "ok": something reached him or is on its way (Civitai pending, queued
    file). "failed": a tool ran but nothing came of it. "none": nothing ran."""
    if delivered or queued:
        return "ok", ""
    results = list(doing_results or [])
    if not results:
        return "none", ""
    for _, res in results:
        if _OK_RESULT.search(res or ""):
            return "ok", ""
    name, res = results[-1]
    first = (res or "no result").strip().splitlines()[0][:220]
    return "failed", f"{name}: {first}"


# He asked for something to be made or sent, or says it never arrived.
_DELIVERY_ASK = re.compile(
    r"\b(send|sent|show|make|draw|generate|record|snap|take (a|another)|another|more|"
    r"again|pics?|pictures?|photos?|images?|selfies?|nudes?|videos?|voice|"
    r"didn'?t (get|receive|come|arrive)|still nothing|where is it|where'?s the|"
    r"never (came|arrived|sent)|(not|isn'?t|never) (sending|coming|showing|arriving|"
    r"loading)|try again|one more)\b", re.I)


def delivery_expected(owner_text: str) -> bool:
    """Only then can a 'here it is' with nothing sent be a phantom. Without
    this, a normal reply to 'it works' was flagged, and the redo went and
    did something else entirely (an unrelated audit)."""
    return bool(_DELIVERY_ASK.search(owner_text or ""))


def phantom_note(line: str) -> str:
    return (f"[Reality check: you just wrote \"{line}\" as if you sent them something, "
            "but no tool ran this turn, so nothing reached them. Make exactly that thing "
            "now with the right tool (generate_image, text_to_speech, send_file...). If "
            "it was already sent in an earlier turn, just say so plainly. Don't start "
            "anything else, don't blame the tool, Telegram or their phone, and don't "
            "describe it instead of sending it.]")


def failed_note(line: str, why: str) -> str:
    return (f"[Reality check: you were about to say \"{line}\", but it didn't send. "
            f"The tool said: {why}. They haven't seen your message yet. Fix it and try "
            "again (different model or wording if it was refused), or tell them plainly "
            "what went wrong. Don't say it was sent.]")


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
    return bool(t) and bool(_ACTION_ASK.search(t) or _NUDGE.match(t) or _RELAY_ASK.search(t))


def is_empty_promise(line: str) -> bool:
    """A short line that promises/threatens an action without doing it."""
    return bool(_PROMISE.search(line or ""))


def needs_action(owner_text: str, her_last: str = "") -> bool:
    """Route to a turn that can act: he's asking for action, or she's holding
    an unkept promise from her last message."""
    return asks_for_action(owner_text) or is_empty_promise(her_last)


# ── Relays between conversations ────────────────────────────────────────
# A guest says "let him know…" (or he says "tell Eri…") and she answers
# "done, told him" — or writes <relay_to_owner>…</relay_to_owner> as text —
# with no relay tool run, so nothing crossed over. Catch it and redo once.
RELAY_TOOLS = frozenset({"relay_to_owner", "send_message_to_guest"})

_CLAIMED_RELAY = re.compile(
    r"<\s*/?\s*(relay_to_owner|send_message_to_guest)\b|"
    r"\b(i('| ha)?ve |i |just |already |)(told|relayed|passed|forwarded|messaged|texted|"
    r"sent|let)\b[^.!?\n]{0,30}\b(him|her|them|okia|eri|your owner|it (on|along)|"
    r"that (on|along)|the message|know)\b|"
    r"\b(message|it)('s| is| was)? (sent|delivered|passed (on|along))\b|"
    r"\bwill do\b|\bconsider it (done|sent|relayed)\b",
    re.I,
)


def relay_expected(text: str) -> bool:
    """Someone asked her to carry a message to another conversation."""
    return bool(_RELAY_ASK.search(text or ""))


def claimed_relay(reply: str) -> str:
    """The line where she says she passed it on ("" when none)."""
    for line in (reply or "").splitlines():
        if _CLAIMED_RELAY.search(line):
            return line.strip()
    return ""


def relayed(tools_called: list[str] | None) -> bool:
    return any(t in RELAY_TOOLS for t in (tools_called or []))


def relay_note(line: str, *, is_owner: bool) -> str:
    if is_owner:
        how = ("call send_message_to_guest now (platform and user_id from your guest "
               "list) with the message in your own words")
    else:
        how = ("call relay_to_owner now with the message (as_self=true if they want it "
               "to look like it came from you)")
    return (f"[Reality check: you just wrote \"{line}\", but no relay tool ran, so nothing "
            f"reached the other conversation. Writing it out or wrapping it in tags sends "
            f"nothing. {how}. Then answer them briefly. Don't do anything else.]")
