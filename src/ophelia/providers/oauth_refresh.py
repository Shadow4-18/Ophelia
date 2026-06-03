"""xAI SuperGrok OAuth refresh (compatible with Hermes ~/.hermes/auth.json)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

XAI_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
REFRESH_SKEW_SECONDS = 120
FALLBACK_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = (4 - len(parts[1]) % 4) % 4
        raw = base64.urlsafe_b64decode(parts[1] + "=" * pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jwt_exp(token: str) -> float | None:
    exp = _jwt_payload(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def _client_id_from_tokens(tokens: dict[str, Any]) -> str:
    for key in ("client_id",):
        val = tokens.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for tok_key in ("access_token", "id_token"):
        claims = _jwt_payload(str(tokens.get(tok_key) or ""))
        for claim in ("client_id", "aud"):
            val = claims.get(claim)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return FALLBACK_CLIENT_ID


def parse_xai_oauth_state(data: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize Hermes, Ophelia, or flat auth blobs."""
    if not data:
        return None

    providers = data.get("providers")
    if isinstance(providers, dict):
        entry = providers.get("xai-oauth")
        if isinstance(entry, dict):
            tokens = entry.get("tokens") or entry
            if isinstance(tokens, dict):
                discovery = entry.get("discovery") or {}
                return {
                    "access_token": str(tokens.get("access_token") or "").strip(),
                    "refresh_token": str(tokens.get("refresh_token") or "").strip(),
                    "client_id": _client_id_from_tokens(
                        {**tokens, "client_id": entry.get("client_id")}
                    ),
                    "token_endpoint": str(
                        discovery.get("token_endpoint") or XAI_TOKEN_ENDPOINT
                    ),
                }

    entry = data.get("xai-oauth")
    if isinstance(entry, dict):
        tokens = entry.get("tokens") or entry
        if isinstance(tokens, dict):
            return parse_xai_oauth_state({"providers": {"xai-oauth": entry}})

    access = str(data.get("access_token") or data.get("token") or "").strip()
    if access:
        return {
            "access_token": access,
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "client_id": _client_id_from_tokens(data),
            "token_endpoint": str(data.get("token_endpoint") or XAI_TOKEN_ENDPOINT),
        }
    return None


def load_oauth_state(path: Path) -> dict[str, Any] | None:
    return parse_xai_oauth_state(_read_json(path) or {})


def save_oauth_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_json(path) or {}
    providers = existing.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers["xai-oauth"] = {
        "tokens": {
            "access_token": state["access_token"],
            "refresh_token": state.get("refresh_token", ""),
            "token_type": state.get("token_type", "Bearer"),
        },
        "discovery": {"token_endpoint": state.get("token_endpoint", XAI_TOKEN_ENDPOINT)},
        "client_id": state.get("client_id", FALLBACK_CLIENT_ID),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    existing["providers"] = providers
    existing["active_provider"] = existing.get("active_provider") or "xai-oauth"
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def needs_refresh(access_token: str) -> bool:
    exp = _jwt_exp(access_token)
    if exp is None:
        return False
    return exp <= time.time() + REFRESH_SKEW_SECONDS


def refresh_tokens_sync(state: dict[str, Any]) -> dict[str, Any]:
    refresh_token = state.get("refresh_token", "").strip()
    client_id = state.get("client_id") or FALLBACK_CLIENT_ID
    endpoint = state.get("token_endpoint") or XAI_TOKEN_ENDPOINT
    if not refresh_token:
        raise RuntimeError("No refresh_token — re-login on old phone: hermes auth add xai-oauth")

    resp = httpx.post(
        endpoint,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=30.0,
    )
    if resp.status_code == 403:
        raise RuntimeError(
            "xAI OAuth tier denied (HTTP 403). Subscription may not include API access."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed HTTP {resp.status_code}")

    payload = resp.json()
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise RuntimeError("Refresh response missing access_token")
    return {
        **state,
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "token_type": str(payload.get("token_type") or "Bearer"),
    }


async def ensure_fresh_token(path: Path) -> str:
    state = load_oauth_state(path)
    if not state or not state.get("access_token"):
        raise RuntimeError(f"No OAuth state in {path}")

    if needs_refresh(state.get("access_token", "")):
        log.info("oauth.refreshing")
        state = await asyncio.to_thread(refresh_tokens_sync, state)
        save_oauth_state(path, state)

    return state["access_token"]
