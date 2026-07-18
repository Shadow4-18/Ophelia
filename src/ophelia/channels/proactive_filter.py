"""Filter junk from proactive / spontaneous outreach (Discord DMs, Telegram, etc.).

Consciousness ticks, inner-mirror, and the send_message tool can emit status
placeholders like SKIP or "(no response)" that should stay internal — not ping
the owner every 90 seconds on every configured gateway.
"""

from __future__ import annotations

import json
import re

from ophelia.channels.message_split import split_messages

# Exact-match placeholders the runtime or models emit when there's nothing to say.
_JUNK_EXACT = frozenset(
    {
        "skip",
        "(no response)",
        "no response",
        "[no response]",
    }
)

# Channel-tagged lines copied from cross-channel memory — never user-facing outreach.
_CHANNEL_TAG = re.compile(
    r"^\[(?:consciousness|inner|cli|saw|spontaneous)\]", re.IGNORECASE
)

# System/meta diagnostics the model emits when confused by tick loops.
_META_KEYWORDS = re.compile(
    r"\b(?:duplicate(?:\s+\w+){0,3}\s+prompt|duplicate block|"
    r"cycling the same|looping without|no owner engagement|"
    r"holding stillness|holding still)\b",
    re.IGNORECASE,
)

# Silent-tick status fluff: models invent these when the pulse feels like a summons.
_TICK_STATUS_NOISE = re.compile(
    r"\b(?:"
    r"holding\s+still(?:ness)?|stillness|status\s+report|"
    r"nothing\s+(?:to\s+say|new|worth\s+saying)|"
    r"no\s+(?:change|update|new\s+thought)|"
    r"mid[- ]thought|same\s+as\s+(?:last|before)|"
    r"just\s+(?:quiet|still|waiting)|"
    r"pulse\s+(?:with\s+)?nothing|"
    r"awaiting\s+(?:input|something)"
    r")\b",
    re.IGNORECASE,
)

_STILLNESS_LABELS = frozenset(
    {
        "still",
        "stillness",
        "quiet",
        "silent",
        "idle",
        "waiting",
        "holding",
        "pause",
        "paused",
        "none",
        "n/a",
        "skip",
    }
)

# Consciousness tick schema keys — models echo these into chat after seeing
# raw tick JSON in cross-channel history.
_TICK_ACTION_VALUES = frozenset(
    {"silent", "message", "reflect", "act", "explore"}
)
_CREATIVE_INTENT = re.compile(
    r"(?ix)\b(?:"
    r"generate[_ ]?(?:image|video|art)|"
    r"text_to_speech|tts|"
    r"(?:draw|paint|render|create)\s+(?:an?\s+)?(?:image|picture|selfie|portrait)|"
    r"(?:make|send)\s+(?:an?\s+)?(?:image|picture|selfie|portrait|video)|"
    r"nsfw|pony\s*v?\d*|illustrious|sdxl|flux\b"
    r")\b"
)


def _object_is_tick(obj: object) -> bool:
    """True if a parsed JSON object looks like a consciousness tick payload."""
    if not isinstance(obj, dict):
        return False
    action = obj.get("action")
    if not isinstance(action, str) or action.lower() not in _TICK_ACTION_VALUES:
        return False
    # Require at least one other tick field so casual {"action":"message"}
    # dicts in normal chat aren't treated as ticks.
    return any(
        key in obj
        for key in ("internal_thought", "mood", "outward_message", "tool_intent", "feelings", "urges")
    )


def _iter_tick_json_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of consciousness-tick JSON objects in text."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start < 0:
            break
        depth = 0
        end = -1
        in_str = False
        escape = False
        for j in range(start, n):
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end < 0:
            break
        blob = text[start:end]
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            i = start + 1
            continue
        if _object_is_tick(obj):
            span_start = start
            # Swallow a leading "[consciousness]" tag on the prior line.
            prefix = text[:start]
            m = re.search(r"\[consciousness\]\s*$", prefix, re.IGNORECASE)
            if m:
                span_start = m.start()
            spans.append((span_start, end))
            i = end
        else:
            i = start + 1
    return spans


def is_consciousness_tick_payload(text: str) -> bool:
    """True if text is (or is mostly) a consciousness tick JSON blob."""
    t = (text or "").strip()
    if not t:
        return False
    # Drop an optional channel tag the model copies from history.
    bare = re.sub(r"^\[consciousness\]\s*", "", t, flags=re.IGNORECASE).strip()
    if bare.startswith("{"):
        try:
            return _object_is_tick(json.loads(bare))
        except json.JSONDecodeError:
            pass
    spans = _iter_tick_json_spans(t)
    if not spans:
        return False
    # If removing tick JSON leaves real prose, this is a mixed leak — not a
    # pure tick payload. Only treat as tick when nothing user-facing remains.
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(t[cursor:start])
        cursor = end
    parts.append(t[cursor:])
    remainder = re.sub(r"\s+", " ", "".join(parts)).strip()
    remainder = re.sub(
        r"^\[consciousness\]\s*|\s*\[consciousness\]\s*$",
        "",
        remainder,
        flags=re.IGNORECASE,
    ).strip(" \n\t:-")
    if not remainder:
        return True
    if len(remainder) < 12 and not re.search(r"[A-Za-z]{3,}", remainder):
        return True
    return False


def strip_consciousness_tick_leak(text: str) -> str:
    """Remove embedded consciousness-tick JSON from an otherwise normal message.

    Models that saw raw tick JSON in history often append a fake tick to a
    chat reply ("Sent. … {action: silent}"). Keep the human prose; drop the
    tick. Returns empty string when nothing user-facing remains.
    """
    t = (text or "").strip()
    if not t:
        return ""
    if is_consciousness_tick_payload(t):
        return ""
    spans = _iter_tick_json_spans(t)
    if not spans:
        return t
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(t[cursor:start])
        cursor = end
    parts.append(t[cursor:])
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    cleaned = re.sub(
        r"(?:^|\n)\s*\[consciousness\]\s*(?=\n|$)",
        "\n",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"^\[consciousness\]\s*", "", cleaned, flags=re.IGNORECASE).strip()
    if not cleaned or is_consciousness_tick_payload(cleaned):
        return ""
    # Avoid importing cycle issues: inline the cheap junk checks we need.
    if cleaned.lower() in _JUNK_EXACT or _CHANNEL_TAG.match(cleaned):
        return ""
    return cleaned


def has_creative_tool_intent(text: str) -> bool:
    """True if text declares image/video/voice creation intent."""
    return bool(_CREATIVE_INTENT.search(text or ""))


def is_outreach_junk(text: str) -> bool:
    """True if this text should not be pushed to the owner as outreach."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    if low in _JUNK_EXACT:
        return True
    if _CHANNEL_TAG.match(t):
        return True
    if is_consciousness_tick_payload(t):
        return True
    if _META_KEYWORDS.search(t):
        return True
    # Bare channel status: "[consciousness] (no response)" etc.
    if re.match(
        r"^\[(?:consciousness|inner)\]\s*(\(no response\)|no response)?\s*$",
        t,
        re.IGNORECASE,
    ):
        return True
    return False


def is_tick_status_noise(text: str) -> bool:
    """True if text is empty silence-label fluff from a quiet consciousness tick."""
    if is_outreach_junk(text):
        return True
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower().strip(" .!\"'")
    if low in _STILLNESS_LABELS:
        return True
    if _TICK_STATUS_NOISE.search(t):
        return True
    return False


def is_stillness_mood_label(label: str | None) -> bool:
    """True if a mood label is just a silence/status token."""
    low = (label or "").strip().lower().strip(" .!\"'")
    return not low or low in _STILLNESS_LABELS


def proactive_chunks(text: str, *, limit: int = 6) -> list[str]:
    """Split proactive text and drop junk/empty chunks."""
    out: list[str] = []
    for c in split_messages(text, limit=limit):
        cleaned = strip_consciousness_tick_leak(c)
        if cleaned and not is_outreach_junk(cleaned):
            out.append(cleaned)
    return out
