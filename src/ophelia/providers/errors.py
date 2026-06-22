"""Helpers for extracting useful detail from LLM API errors."""

from __future__ import annotations


def api_error_detail(exc: BaseException) -> str:
    """Best-effort extraction of status + body from an openai/APIStatusError.

    openai.BadRequestError's str() is just "Error code: 400", which hides the
    actual reason (bad model name, unsupported tool schema, etc.). This pulls
    the response body out of the exception so it can be logged and shown.
    """
    # openai.APIStatusError exposes .response (httpx.Response) and .status_code
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    body = ""
    if response is not None:
        try:
            body = response.text
        except Exception:
            body = ""
    if not body:
        # Some exceptions carry a .body or .message attribute
        for attr in ("body", "message", "detail"):
            val = getattr(exc, attr, None)
            if isinstance(val, str) and val:
                body = val
                break
    parts = []
    if status:
        parts.append(f"HTTP {status}")
    if body:
        parts.append(body.strip()[:500])
    if not parts:
        parts.append(str(exc))
    return ": ".join(parts)
