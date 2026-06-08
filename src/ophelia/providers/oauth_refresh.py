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
XAI_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
REFRESH_SKEW_SECONDS = 120
FALLBACK_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"


class OAuthRefreshError(RuntimeError):
    def __init__(self, message: str, *, relogin: bool = False) -> None:
        super().__init__(message)
        self.relogin = relogin


def oauth_auth_paths(
    *,
    hermes_home: Path | None = None,
    hermes_auth_path: Path | None = None,
    oauth_path: Path | None = None,
) -> list[Path]:
    """Prefer live ~/.hermes/auth.json (Hermes) over Ophelia copies."""
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in (
        (hermes_home or Path.home() / ".hermes") / "auth.json",
        hermes_auth_path or Path.home() / ".ophelia" / "hermes_auth.json",
        oauth_path or Path.home() / ".ophelia" / "xai_oauth.json",
    ):
        p = raw.expanduser().resolve()
        if p in seen:
            continue
        seen.add(p)
        if p.is_file():
            out.append(p)
    return out


def resolve_oauth_auth_path(
    *,
    hermes_home: Path | None = None,
    hermes_auth_path: Path | None = None,
    oauth_path: Path | None = None,
) -> Path | None:
    paths = oauth_auth_paths(
        hermes_home=hermes_home,
        hermes_auth_path=hermes_auth_path,
        oauth_path=oauth_path,
    )
    return paths[0] if paths else None


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


def access_token_usable(access_token: str) -> bool:
    exp = _jwt_exp(access_token)
    if exp is None:
        return bool(access_token.strip())
    return exp > time.time()


def access_token_expiry_label(access_token: str) -> str:
    exp = _jwt_exp(access_token)
    if exp is None:
        return "unknown expiry"
    remaining = int(exp - time.time())
    if remaining <= 0:
        return f"expired {abs(remaining) // 60}m ago"
    return f"expires in {remaining // 60}m"


def format_refresh_error(status_code: int, body: str) -> str:
    detail = body.strip()[:300]
    try:
        err = json.loads(body)
        if isinstance(err, dict):
            code = str(err.get("error") or "").strip()
            desc = str(err.get("error_description") or err.get("message") or "").strip()
            if code == "invalid_grant":
                return (
                    "refresh token invalid or expired (invalid_grant) — "
                    "run: ophelia auth login"
                )
            if code and desc:
                detail = f"{code}: {desc}"
            elif code:
                detail = code
    except (json.JSONDecodeError, TypeError):
        pass
    msg = f"Token refresh failed HTTP {status_code}"
    if detail:
        msg += f": {detail}"
    if status_code in {400, 401, 403}:
        msg += " — run: ophelia auth login"
    return msg


def describe_oauth_paths(
    *,
    hermes_home: Path | None = None,
    hermes_auth_path: Path | None = None,
    oauth_path: Path | None = None,
) -> list[str]:
    lines: list[str] = []
    paths = oauth_auth_paths(
        hermes_home=hermes_home,
        hermes_auth_path=hermes_auth_path,
        oauth_path=oauth_path,
    )
    if not paths:
        lines.append("No OAuth auth files found.")
        return lines
    primary = paths[0]
    for i, path in enumerate(paths):
        state = load_oauth_state(path)
        prefix = "ACTIVE" if path == primary else "copy"
        if not state or not state.get("access_token"):
            lines.append(f"[{prefix}] {path} — no xai-oauth tokens")
            continue
        refresh = state.get("refresh_token", "").strip()
        refresh_note = f"refresh_token={'yes' if refresh else 'MISSING'}"
        exp_note = access_token_expiry_label(state["access_token"])
        lines.append(f"[{prefix}] {path}")
        lines.append(f"         access: {exp_note}, {refresh_note}")
    return lines


def _discover_token_endpoint(timeout: float = 15.0) -> str:
    try:
        resp = httpx.get(
            XAI_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            endpoint = str(resp.json().get("token_endpoint") or "").strip()
            if endpoint.startswith("https://") and ".x.ai" in endpoint:
                return endpoint
    except httpx.HTTPError as e:
        log.warning("oauth.discovery_failed", error=str(e))
    return XAI_TOKEN_ENDPOINT


def refresh_tokens_sync(state: dict[str, Any]) -> dict[str, Any]:
    refresh_token = state.get("refresh_token", "").strip()
    client_id = state.get("client_id") or FALLBACK_CLIENT_ID
    endpoint = str(state.get("token_endpoint") or "").strip() or _discover_token_endpoint()
    if not refresh_token:
        raise OAuthRefreshError(
            "No refresh_token — re-authenticate: ophelia auth login",
            relogin=True,
        )

    resp = httpx.post(
        endpoint,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=30.0,
    )
    if resp.status_code == 403:
        raise OAuthRefreshError(
            "xAI OAuth tier denied (HTTP 403). Subscription may not include API access.",
            relogin=True,
        )
    if resp.status_code != 200:
        relogin = resp.status_code in {400, 401, 403}
        raise OAuthRefreshError(
            format_refresh_error(resp.status_code, resp.text),
            relogin=relogin,
        )

    payload = resp.json()
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise OAuthRefreshError("Refresh response missing access_token", relogin=True)
    return {
        **state,
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "token_type": str(payload.get("token_type") or "Bearer"),
        "token_endpoint": endpoint,
    }


async def ensure_fresh_token(path: Path, *, force: bool = False) -> str:
    state = load_oauth_state(path)
    if not state or not state.get("access_token"):
        raise RuntimeError(f"No OAuth state in {path}")

    access = state["access_token"]
    if not force and not needs_refresh(access):
        return access

    try:
        log.info("oauth.refreshing", path=str(path))
        state = await asyncio.to_thread(refresh_tokens_sync, state)
        save_oauth_state(path, state)
        return state["access_token"]
    except OAuthRefreshError as e:
        if access_token_usable(access):
            log.warning("oauth.refresh_failed_using_cached", error=str(e), path=str(path))
            return access
        raise RuntimeError(str(e)) from e
