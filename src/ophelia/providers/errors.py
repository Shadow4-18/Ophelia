"""Helpers for extracting useful detail from LLM API errors."""

from __future__ import annotations

import json


def api_error_detail(exc: BaseException) -> str:
    """Best-effort extraction of status + body from an openai/APIStatusError.

    openai.BadRequestError's str() is just "Error code: 400", which hides the
    actual reason (bad model name, unsupported tool schema, etc.). This pulls
    the response body out of the exception so it can be logged and shown.

    Tries multiple sources because the openai SDK surfaces errors differently
    depending on version and transport:
      - .response.text (httpx.Response)
      - .body (parsed dict, may contain {"error": {"message": ...}})
      - .message (sometimes set directly)
      - str(exc) (fallback, usually "Error code: 400")
    """
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)

    body = ""
    # Source 1: raw response text
    if response is not None:
        try:
            body = response.text or ""
        except Exception:
            body = ""

    # Source 2: parsed body dict (openai sets .body on APIStatusError)
    parsed_body = getattr(exc, "body", None)
    if not body and parsed_body:
        if isinstance(parsed_body, dict):
            # Standard OpenAI/xAI error envelope: {"error": {"message": ..., "type": ...}}
            err_obj = parsed_body.get("error")
            if isinstance(err_obj, dict):
                msg = err_obj.get("message") or err_obj.get("type") or ""
                if msg:
                    body = msg
            elif isinstance(err_obj, str) and err_obj:
                body = err_obj
            else:
                body = json.dumps(parsed_body)
        elif isinstance(parsed_body, str):
            body = parsed_body

    # Source 3: .message attribute
    if not body:
        msg = getattr(exc, "message", None)
        if isinstance(msg, str) and msg:
            body = msg

    # If the body is JSON, try to dig the real message out of it.
    body = _extract_message_from_json(body) or body

    parts = []
    if status:
        parts.append(f"HTTP {status}")
    if body and body.strip():
        parts.append(body.strip()[:500])
    if not parts:
        parts.append(str(exc))
    return ": ".join(parts)


def _extract_message_from_json(text: str) -> str | None:
    """If `text` is a JSON error envelope, pull the human message out of it."""
    if not text:
        return None
    s = text.strip()
    if not s.startswith("{"):
        return None
    try:
        data = json.loads(s)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type") or ""
        detail = err.get("detail") or ""
        if isinstance(msg, str) and msg:
            return f"{msg}{f' ({detail})' if detail else ''}"
    if isinstance(err, str) and err:
        return err
    # Some providers put the message at the top level
    msg = data.get("message") or data.get("detail")
    if isinstance(msg, str) and msg:
        return msg
    return None
