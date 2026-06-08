"""Telegram Bot API helpers (webhook vs polling)."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

log = structlog.get_logger()


async def fetch_webhook_info(token: str, *, timeout: float = 10.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or "getWebhookInfo failed"))
    result = data.get("result")
    return result if isinstance(result, dict) else {}


async def ensure_polling_mode(token: str, *, timeout: float = 10.0) -> str | None:
    """Delete stale webhook so Ophelia can poll (Hermes webhook mode blocks getUpdates)."""
    info = await fetch_webhook_info(token, timeout=timeout)
    url = str(info.get("url") or "").strip()
    if not url:
        return None
    log.warning("telegram.webhook_active", url=url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            params={"drop_pending_updates": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or "deleteWebhook failed"))
    log.info("telegram.webhook_cleared", previous_url=url)
    return url
