"""Copyable code blocks on Telegram.

Her replies go out as plain text, so a ``` block arrived as literal backticks
around the lyrics and he had to select around them by hand. Telegram has
real code blocks (monospace, tap to copy) but only when the message says
where they are. This turns ```...``` into a "pre" block and `...` into inline
code, as message entities rather than Markdown, so nothing else in her text
(a stray * or _) can ever be read as formatting.

Entity offsets are in UTF-16 code units, as the Bot API requires (an emoji
counts as two).
"""

from __future__ import annotations

import re
from typing import Any

# ```lang\n body ``` : the language word is optional; so is the newline.
_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+#.-]*)[ \t]*\n?(.*?)\n?[ \t]*```", re.S)
_INLINE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def to_entities(text: str) -> tuple[str, list[dict[str, Any]]]:
    """(text without the backtick markers, entity dicts for it)."""
    out: list[str] = []
    ents: list[dict[str, Any]] = []
    length = 0  # UTF-16 length of what's in ``out``

    def add_plain(chunk: str) -> None:
        nonlocal length
        pos = 0
        for m in _INLINE.finditer(chunk):
            before = chunk[pos:m.start()]
            out.append(before)
            length += _u16(before)
            code = m.group(1)
            ents.append({"type": "code", "offset": length, "length": _u16(code)})
            out.append(code)
            length += _u16(code)
            pos = m.end()
        rest = chunk[pos:]
        out.append(rest)
        length += _u16(rest)

    pos = 0
    for m in _FENCE.finditer(text):
        add_plain(text[pos:m.start()])
        lang, body = m.group(1), m.group(2)
        if body.strip():
            ent: dict[str, Any] = {"type": "pre", "offset": length, "length": _u16(body)}
            if lang:
                ent["language"] = lang
            ents.append(ent)
            out.append(body)
            length += _u16(body)
        pos = m.end()
    add_plain(text[pos:])
    return "".join(out), ents


def send_kwargs(text: str, limit: int = 4096) -> dict[str, Any]:
    """Keyword arguments for send_message / reply_text: the text with code
    blocks as entities (only when there are some)."""
    from telegram import MessageEntity

    plain, ents = to_entities(text)
    if not ents or _u16(plain) > limit:
        return {"text": text[:limit]}
    return {"text": plain, "entities": [MessageEntity(**e) for e in ents]}


async def send(call, text: str, **kw: Any):
    """``await call(**send_kwargs(text), **kw)``; if Telegram rejects the
    entities, send the plain text instead so the message still arrives."""
    from telegram.error import BadRequest

    args = send_kwargs(text)
    try:
        return await call(**args, **kw)
    except BadRequest:
        if "entities" not in args:
            raise
        return await call(text=text[:4096], **kw)
