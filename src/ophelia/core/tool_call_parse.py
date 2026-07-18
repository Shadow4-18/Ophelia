"""Recover structured tool calls from assistant text content.

Some providers/models (notably smaller Ollama models) emit a prose preamble
and then either:
  - put a JSON / markup tool invocation in ``content`` with ``tool_calls`` empty, or
  - narrate the action ("let me generate that") and stop without any call.

The agent loop treats a text-only response as a finished turn. These helpers
let the loop salvage the tool intent instead of terminating early.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedToolCall:
    name: str
    arguments: str  # JSON object string


# Common shapes models dump into content when structured tool_calls is empty.
_JSON_FENCE = re.compile(
    r"```(?:json|tool|tool_call)?\s*(\{[\s\S]*?\})\s*```",
    re.IGNORECASE,
)
_XML_TOOL = re.compile(
    r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>",
    re.IGNORECASE,
)
_LLAMA_PYTHON_TAG = re.compile(
    r"<\|python_tag\|>\s*(\{[\s\S]*?\})",
    re.IGNORECASE,
)
_FUNCTION_EQ = re.compile(
    r"<function\s*=\s*([A-Za-z_][\w.]*)\s*>([\s\S]*?)</function>",
    re.IGNORECASE,
)

# Prose that claims a tool is about to run / already ran, without a real call.
_NARRATION_HINTS = re.compile(
    r"(?ix)"
    r"("
    r"\bi'?ll\s+(generate|make|create|search|send|call|fire|run|try|do)\b"
    r"|\blet\s+me\s+(try|check|search|generate|make|create|send|call|look|do)\b"
    r"|\bone\s+sec\b"
    r"|\bgive\s+me\s+(a\s+)?(sec|second|moment|minute)\b"
    r"|\btool\s+called\b"
    r"|\*fires?\s+(the\s+)?tool\*"
    r"|\bfiring\s+(the\s+)?tool\b"
    r"|\bcalling\s+(the\s+)?(tool|generate_|send_|search_)"
    r"|\b(generate_image|generate_video|send_message|web_search|text_to_speech)\b"
    # Claimed media delivery / backend narration without an actual tool call —
    # common on NSFW image retries ("Sent. This time I used Illustrious…").
    r"|\bi\s+(?:just\s+)?(?:sent|generated|rendered|drew|painted)\b"
    r"|\bsent\.?\s+(?:this\s+time|again|a\s+(?:new\s+)?(?:one|image|picture))\b"
    r"|\bthis\s+time\s+i\s+used\b"
    r"|\bused\s+(?:pony|illustrious|sdxl|flux|nsfw)\b"
    r"|\bnsfw\s+backend\b"
    r")"
)


def _as_args_json(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "{}"
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            return json.dumps({"raw": text})
    if isinstance(value, dict):
        return json.dumps(value)
    return json.dumps({"value": value})


def _from_obj(obj: Any, *, known_tools: set[str] | None) -> ParsedToolCall | None:
    if not isinstance(obj, dict):
        return None
    # {"name": "...", "parameters"|"arguments": {...}}
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    args = obj.get("arguments", obj.get("parameters", obj.get("args")))
    # {"function": {"name": "...", "arguments": ...}}
    if not name and isinstance(obj.get("function"), dict):
        fn = obj["function"]
        name = fn.get("name")
        args = fn.get("arguments", fn.get("parameters", args))
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if known_tools is not None and name not in known_tools:
        return None
    return ParsedToolCall(name=name, arguments=_as_args_json(args))


def _iter_json_candidates(text: str) -> list[str]:
    blobs: list[str] = []
    for rx in (_JSON_FENCE, _XML_TOOL, _LLAMA_PYTHON_TAG):
        blobs.extend(m.group(1) for m in rx.finditer(text))
    # Fallback: scan for balanced {...} objects that look like tool payloads.
    for m in re.finditer(r"\{", text):
        start = m.start()
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if '"name"' in candidate or '"function"' in candidate:
                        blobs.append(candidate)
                    break
    return blobs


def extract_tool_calls_from_content(
    content: str | None,
    *,
    known_tools: set[str] | None = None,
) -> tuple[list[ParsedToolCall], str]:
    """Parse tool invocations embedded in assistant text.

    Returns ``(calls, remaining_text)``. ``remaining_text`` is the prose with
    recognized tool markup stripped, suitable as a mid-turn preamble.
    """
    text = content or ""
    if not text.strip():
        return [], ""

    calls: list[ParsedToolCall] = []
    remaining = text

    for m in _FUNCTION_EQ.finditer(text):
        name = m.group(1).strip()
        if known_tools is not None and name not in known_tools:
            continue
        raw_args = (m.group(2) or "").strip()
        args_obj: Any = {}
        if raw_args:
            try:
                args_obj = json.loads(raw_args)
            except json.JSONDecodeError:
                args_obj = {"raw": raw_args}
        calls.append(ParsedToolCall(name=name, arguments=_as_args_json(args_obj)))
        remaining = remaining.replace(m.group(0), "\n")

    for blob in _iter_json_candidates(text):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        matched = False
        for item in items:
            parsed = _from_obj(item, known_tools=known_tools)
            if parsed is None:
                continue
            calls.append(parsed)
            matched = True
        if matched:
            remaining = remaining.replace(blob, "\n")
            # Also drop surrounding fences if still present.
            remaining = re.sub(
                r"```(?:json|tool|tool_call)?\s*```",
                "\n",
                remaining,
                flags=re.IGNORECASE,
            )

    # Deduplicate identical name+args while preserving order.
    seen: set[tuple[str, str]] = set()
    unique: list[ParsedToolCall] = []
    for c in calls:
        key = (c.name, c.arguments)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    cleaned = re.sub(r"\n{3,}", "\n\n", remaining).strip()
    return unique, cleaned


def looks_like_tool_narration(content: str | None) -> bool:
    """True when prose claims a tool action without structured tool_calls."""
    text = (content or "").strip()
    if not text:
        return False
    return bool(_NARRATION_HINTS.search(text))


def synthetic_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def parsed_to_openai_dicts(calls: list[ParsedToolCall]) -> list[dict[str, Any]]:
    """Shape parsed calls like OpenAI ``message.tool_calls`` entries."""
    out: list[dict[str, Any]] = []
    for c in calls:
        out.append(
            {
                "id": synthetic_tool_call_id(),
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
        )
    return out
